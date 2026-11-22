"""Salad review pre-approval (ADR 0044, amending ADR 0038).

A human may, in the approve body only, pre-approve a Salad stage's source assessment and
its per-attempt plan and source-use reviews by binding the digest of each review's *core*:
the content the approved spec, profile and snapshot determine, with run-minted ids removed.
The runner then resolves exactly those reviews as ``campaign_delegated``, and only when the
core recomputed from the *stored* row inside the resolving transaction equals the bound
digest, the pre-approval is unexpired and the cent ceilings hold. Anything else is a typed
decline and the review stays with a human (fail closed). Pre-approval never covers gate go,
conclusions, exports, publication, data classification or any other step.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .approval_trust import DELEGATED_ACTOR_KEY
from .store import Forbidden, _parse_utc, canonical_digest, utc_now

KIND_SOURCE = "source_assessment"
KIND_PLAN = "plan"
KIND_SOURCE_USE = "source_use"
KINDS = (KIND_SOURCE, KIND_PLAN, KIND_SOURCE_USE)
# step name -> the only review kinds it may resolve
STEP_KINDS = {
    "assess_source": frozenset({KIND_SOURCE}),
    "resolve_decision": frozenset({KIND_PLAN, KIND_SOURCE_USE}),
}
PREAPPROVAL_STEPS = frozenset(STEP_KINDS)

# Typed decline codes (fail closed to the human path).
NO_PREAPPROVAL = "no_preapproval"
EXPIRED = "preapproval_expired"
APPROVAL_INACTIVE = "approval_inactive"
DIGEST_MISMATCH = "digest_mismatch"
UNBOUND_SCOPE = "wrong_stage"
NOT_PENDING = "review_not_pending"
ATTEMPT_CAP = "attempt_cap"
TOTAL_CAP = "total_cap"
NOT_APPROVE = "not_approve"

_DENIED = "Campaign pre-approval does not authorize this review."


def create_preapproval_schema(connection: sqlite3.Connection) -> None:
    """Idempotent; shared by fresh databases and the migration."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS campaign_review_preapprovals (
        id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        approval_id TEXT NOT NULL REFERENCES campaign_approvals(id),
        stage_key TEXT NOT NULL,
        source_digest TEXT NOT NULL CHECK(length(source_digest)=64
            AND source_digest NOT GLOB '*[^0-9a-f]*'),
        plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64
            AND plan_digest NOT GLOB '*[^0-9a-f]*'),
        source_use_digest TEXT NOT NULL CHECK(length(source_use_digest)=64
            AND source_use_digest NOT GLOB '*[^0-9a-f]*'),
        max_attempt_cents INTEGER NOT NULL CHECK(max_attempt_cents >= 1),
        max_total_cents INTEGER NOT NULL CHECK(max_total_cents >= max_attempt_cents),
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(approval_id, stage_key))"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS campaign_review_preapproval_uses (
        id TEXT PRIMARY KEY,
        preapproval_id TEXT NOT NULL REFERENCES campaign_review_preapprovals(id),
        campaign_id TEXT NOT NULL, stage_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('source_assessment','plan','source_use')),
        subject_id TEXT NOT NULL, attempt INTEGER, review_digest TEXT NOT NULL,
        cents INTEGER NOT NULL CHECK(cents >= 0), intent_id TEXT,
        decision_class TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(preapproval_id, kind, subject_id))"""
    )
    for table in ("campaign_review_preapprovals", "campaign_review_preapproval_uses"):
        for event in ("UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()}
                BEFORE {event} ON {table}
                BEGIN SELECT RAISE(ABORT,'campaign pre-approvals are append-only'); END"""
            )


# -- review cores -------------------------------------------------------------------


def source_core_digest(source: Any) -> str:
    """Digest of a Salad stage's source assessment: the spec-fixed source content, judged
    ``supported`` (no other verdict is ever pre-approvable)."""
    core = {
        "uri": source["uri"],
        "quoted_passage": source["quoted_passage"],
        "content_digest": source["content_digest"],
        "locator": source["locator"],
    }
    return canonical_digest(
        {"type": "salad_source_assessment", "version": 1, "source": core, "verdict": "supported"}
    )


_VOLATILE_TOP = ("study_id", "basis_digest", "supersedes_id", "source_ids")


def scope_core(scope: dict[str, Any]) -> dict[str, Any]:
    """A program-review scope without the ids minted at run time.

    Removed: the study, the evidence-basis digest and superseded head, the source ids, the
    study variant ids and the split lock id and digest. The structural binding of those to
    the stage is checked separately (``_bound``); everything else (outcome, action, reason,
    brief, compute bounds, gate, identity digests, intended use) stays and is digested.
    """
    core = copy.deepcopy(scope)
    for name in _VOLATILE_TOP:
        core.pop(name, None)
    brief = core.get("brief")
    if isinstance(brief, dict):
        brief.pop("data_source_ids", None)
    for condition in (core.get("conditions") or {}).values():
        if isinstance(condition, dict):
            condition.pop("variant_ids", None)
    protocol = core.get("evaluation_protocol")
    if isinstance(protocol, dict):
        for gate in protocol.get("gates") or []:
            for name in ("variant_id", "experiment_variant_id", "split_lock_id", "split_digest"):
                gate.pop(name, None)
    return core


def review_core_digest(scope: dict[str, Any]) -> str:
    """Digest of the scope's core after the same ProgramReview normalization a stored
    decision went through, so a proposed scope and its stored row compare equal."""
    from .program_models import ProgramReview

    try:
        scope = ProgramReview.model_validate(scope).model_dump(mode="json")
    except ValueError:
        return "invalid-scope"  # not a digest, so it can never equal a bound one
    return canonical_digest({"type": "salad_review_core", "version": 1, "core": scope_core(scope)})


# -- evaluation ---------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    reason: str | None  # None: authorized
    preapproval: dict[str, Any] | None = None
    digest: str | None = None
    cents: int = 0
    attempt: int | None = None


def stage_preapproval(
    connection: sqlite3.Connection, approval_id: str, stage_key: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM campaign_review_preapprovals WHERE approval_id=? AND stage_key=?",
        (approval_id, stage_key),
    ).fetchone()
    return None if row is None else dict(row)


def _stage_exposure(connection: sqlite3.Connection, campaign: Any, stage_key: str) -> int | None:
    from .campaign_delegation import _normal_spec

    try:
        return int(_normal_spec(campaign).stage(stage_key).max_exposure_cents)
    except (StopIteration, ValueError, KeyError):
        return None


def _bound(scope: dict[str, Any], kind: str, salad: Any) -> bool:
    """The volatile ids the core drops must still be this stage's own."""
    if scope.get("type") != "research_program" or scope.get("study_id") != salad["study_id"]:
        return False
    sources = (
        (scope.get("brief") or {}).get("data_source_ids")
        if kind == KIND_PLAN
        else scope.get("source_ids")
    )
    return sources == [salad["source_id"]] and scope.get("action") == (
        "plan" if kind == KIND_PLAN else "source_use"
    )


def evaluate(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    approval_id: str,
    stage_key: str,
    kind: str,
    subject_id: str,
) -> Verdict:
    """Pure read: would this pre-approval authorize resolving ``subject_id`` as ``kind``."""
    campaign = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    approval = connection.execute(
        "SELECT * FROM campaign_approvals WHERE id=? AND campaign_id=?",
        (approval_id, campaign_id),
    ).fetchone()
    if campaign is None or approval is None or approval["status"] != "active":
        return Verdict(APPROVAL_INACTIVE)
    pre = stage_preapproval(connection, approval_id, stage_key)
    if pre is None or kind not in KINDS:
        return Verdict(NO_PREAPPROVAL)
    now = _parse_utc(utc_now())
    if now >= min(_parse_utc(pre["expires_at"]), _parse_utc(approval["expires_at"])):
        return Verdict(EXPIRED, pre)
    salad = connection.execute(
        "SELECT * FROM campaign_salad_stages WHERE campaign_id=? AND stage_key=?",
        (campaign_id, stage_key),
    ).fetchone()
    if salad is None or salad["source_id"] is None:
        return Verdict(UNBOUND_SCOPE, pre)
    if kind == KIND_SOURCE:
        if subject_id != salad["source_id"]:
            return Verdict(UNBOUND_SCOPE, pre)
        source = connection.execute(
            "SELECT * FROM research_sources WHERE id=?", (subject_id,)
        ).fetchone()
        if source is None or source["verdict"] != "unverified" or source["superseded_at"]:
            return Verdict(NOT_PENDING, pre)
        digest = source_core_digest(source)
        if digest != pre["source_digest"]:
            return Verdict(DIGEST_MISMATCH, pre, digest)
        return Verdict(None, pre, digest)
    attempt = connection.execute(
        """SELECT a.attempt AS attempt FROM campaign_salad_attempts sa
        JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
        WHERE a.campaign_id=? AND a.stage_key=? AND
        ((? = 'plan' AND sa.plan_decision_id=?)
        OR (? = 'source_use' AND sa.source_decision_id=?))""",
        (campaign_id, stage_key, kind, subject_id, kind, subject_id),
    ).fetchone()
    decision = connection.execute(
        "SELECT * FROM decision_requests WHERE id=?", (subject_id,)
    ).fetchone()
    if attempt is None or decision is None:
        return Verdict(UNBOUND_SCOPE, pre)
    if decision["status"] != "pending":
        return Verdict(NOT_PENDING, pre)
    try:
        scope = json.loads(decision["scope_json"])
    except ValueError:
        return Verdict(UNBOUND_SCOPE, pre)
    if not _bound(scope, kind, salad):
        return Verdict(UNBOUND_SCOPE, pre)
    digest = review_core_digest(scope)
    expected = pre["plan_digest"] if kind == KIND_PLAN else pre["source_use_digest"]
    if digest != expected:
        return Verdict(DIGEST_MISMATCH, pre, digest, attempt=attempt["attempt"])
    cents = 0
    if kind == KIND_PLAN:
        cents = _stage_exposure(connection, campaign, stage_key) or 0
        if cents < 1 or cents > pre["max_attempt_cents"]:
            return Verdict(ATTEMPT_CAP, pre, digest, cents, attempt["attempt"])
        used = connection.execute(
            "SELECT COALESCE(SUM(cents),0) AS c FROM campaign_review_preapproval_uses "
            "WHERE preapproval_id=? AND kind='plan'",
            (pre["id"],),
        ).fetchone()["c"]
        if used + cents > pre["max_total_cents"]:
            return Verdict(TOTAL_CAP, pre, digest, cents, attempt["attempt"])
    return Verdict(None, pre, digest, cents, attempt["attempt"])


def authorize_delegated(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    *,
    step: str,
    kind: str,
    subject_id: str,
    approve: bool,
) -> None:
    """Inside the resolving command's transaction. A no-op for a non-delegated actor.

    Any delegated actor reaching a review command must be the matching pre-approval step
    with an exact-digest match; the use is recorded in this same transaction."""
    delegated = live.get(DELEGATED_ACTOR_KEY)
    if not delegated:
        return
    if delegated.get("step") != step or kind not in STEP_KINDS.get(step, frozenset()):
        raise Forbidden(_DENIED)
    if not approve:
        raise Forbidden(_DENIED)  # only an approval is delegated; a human rejects
    intent = connection.execute(
        "SELECT * FROM campaign_step_intents WHERE id=?", (delegated["intent_id"],)
    ).fetchone()
    resources = json.loads(intent["resources_json"]) if intent else {}
    pre = connection.execute(
        "SELECT * FROM campaign_review_preapprovals WHERE id=?", (resources.get("preapproval_id"),)
    ).fetchone()
    if (
        intent is None
        or pre is None
        or pre["approval_id"] != delegated["approval_id"]
        or pre["campaign_id"] != delegated["campaign_id"]
        or pre["stage_key"] != intent["stage_key"]
    ):
        raise Forbidden(_DENIED)
    verdict = evaluate(
        connection,
        campaign_id=delegated["campaign_id"],
        approval_id=delegated["approval_id"],
        stage_key=pre["stage_key"],
        kind=kind,
        subject_id=subject_id,
    )
    if verdict.reason is not None:
        raise Forbidden(f"{_DENIED} ({verdict.reason})")
    connection.execute(
        """INSERT INTO campaign_review_preapproval_uses (id, preapproval_id, campaign_id, stage_key,
        kind, subject_id, attempt, review_digest, cents, intent_id, decision_class, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "preuse_" + uuid.uuid4().hex,
            pre["id"],
            pre["campaign_id"],
            pre["stage_key"],
            kind,
            subject_id,
            verdict.attempt,
            verdict.digest,
            verdict.cents,
            intent["id"],
            "campaign_delegated",
            utc_now(),
        ),
    )


# -- reads for receipts and the approve event ---------------------------------------


def preapproval_summary(connection: sqlite3.Connection, campaign_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM campaign_review_preapprovals WHERE campaign_id=? ORDER BY created_at, id",
        (campaign_id,),
    ).fetchall()
    result = []
    for row in rows:
        uses = connection.execute(
            """SELECT kind, subject_id, attempt, review_digest, cents, decision_class
            FROM campaign_review_preapproval_uses WHERE preapproval_id=? ORDER BY created_at, id""",
            (row["id"],),
        ).fetchall()
        result.append(
            {
                "id": row["id"],
                "approval_id": row["approval_id"],
                "stage_key": row["stage_key"],
                "source_digest": row["source_digest"],
                "plan_digest": row["plan_digest"],
                "source_use_digest": row["source_use_digest"],
                "max_attempt_cents": row["max_attempt_cents"],
                "max_total_cents": row["max_total_cents"],
                "expires_at": row["expires_at"],
                "resolutions": [dict(u) for u in uses],
            }
        )
    return result


def preview_for_campaign(
    state_dir: Any, profiles_file: Any, campaign_id: str, stage_key: str | None = None
) -> list[dict[str, str]]:
    """Read-only: the digests a human would bind in the approve body for a campaign's Salad
    stages (ADR 0044). Grants nothing; nothing is written."""
    from pathlib import Path

    from .campaign_models import normalize, parse_spec_json, stage_provider
    from .campaign_provider_salad import SaladRouteProvider
    from .campaign_runner import CampaignRunner, _Ctx
    from .salad_campaign_profiles import load_profiles

    runner = CampaignRunner(Path(state_dir))
    with runner.store.runner_connection() as connection:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise ValueError("campaign not found")
    campaign = dict(row)
    spec = normalize(parse_spec_json(campaign["spec_json"]))
    driver = SaladRouteProvider(load_profiles(Path(profiles_file))).driver(runner)
    ctx = _Ctx(campaign, spec, None, 0)
    result = []
    for stage in spec.stages:
        if stage_key is not None and stage.key != stage_key:
            continue
        try:
            salad = stage_provider(stage.provider).descriptor.kind == "salad_route"
        except KeyError:
            salad = False
        if salad:
            result.append(
                {
                    **driver.preview_review_digests(ctx, stage.key),
                    "max_attempt_cents": stage.max_exposure_cents,
                    "max_total_cents": spec.budget_cap_cents,
                }
            )
    return result
