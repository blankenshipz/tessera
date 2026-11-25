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
WRONG_PROVIDER = "wrong_provider"
WRONG_PROFILE = "wrong_profile"

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


def _stage_exposure(
    connection: sqlite3.Connection,
    campaign: Any,
    stage_key: str,
    provider_id: str | None = None,
) -> int | None:
    """The cent hold of the stage on the provider the attempt was placed on (ADR 0066).

    ``None`` (fail closed to the human path) for a provider the approved spec does not name for
    this stage, one that is not registered, or one that is not a Salad route: a pre-approval
    never reaches an unregistered or unapproved destination."""
    from .campaign_delegation import _normal_spec
    from .campaign_models import stage_on_candidate, stage_provider

    try:
        stage = _normal_spec(campaign).stage(stage_key)
        provider_id = provider_id or stage.provider
        if provider_id != stage.provider:
            stage = stage_on_candidate(stage, provider_id)
        # The same re-check for every provider, the stage's own included (ADR 0066 section 3).
        if stage_provider(provider_id).descriptor.kind != "salad_route":
            return None
        return int(stage.max_exposure_cents)
    except (StopIteration, ValueError, KeyError):
        return None


def covered_provider(
    connection: sqlite3.Connection, pre: dict[str, Any], stage_provider_id: str | None
) -> str | None:
    """The provider this pre-approval names, as recorded on the approve event (ADR 0066).

    An entry approved before alternates could be named (no ``provider_id`` recorded) covers the
    stage's own provider only."""
    row = connection.execute(
        """SELECT json_extract(p.value, '$.provider_id') AS provider_id
        FROM events e JOIN campaigns c ON c.id=?, json_each(e.payload_json, '$.preapprovals') p
        WHERE e.event_type='campaign.approved' AND e.org_id=c.org_id
        AND e.project_id=c.project_id AND json_extract(e.payload_json, '$.campaign_id')=c.id
        AND json_extract(p.value, '$.id')=? LIMIT 1""",
        (pre["campaign_id"], pre["id"]),
    ).fetchone()
    return (row["provider_id"] if row is not None else None) or stage_provider_id


def bound_profiles(placed_stage: Any, entry: Any) -> list[dict[str, str]] | None:
    """The profiles one approve-body entry covers, each with its bound plan digest (ADR 0151).

    ``placed_stage`` is the stage on the candidate the entry covers (``stage_on_candidate``);
    ``entry`` is its ``StagePreapproval``. ``None`` when that candidate lists no alternate
    profiles: the entry then covers its own profile only, exactly as before ADR 0151. Otherwise
    the entry must name every listed alternate once with its plan digest (``plan_digest`` stays
    the candidate's own profile's); a profile the approved spec does not list is refused and so
    can never be bound. Source assessment and source use do not depend on the profile, so the
    entry's digests of those cover every listed profile. Raises ValueError naming the profile."""
    from .salad_alternate_profiles import covered_profiles

    key = entry.stage_key
    order = covered_profiles(placed_stage)
    named = {item.profile: item.plan_digest for item in entry.alternate_profiles or ()}
    for name in named:
        if name == order[0]:
            raise ValueError(
                f"Pre-approval of stage {key!r} lists profile {name!r}, which is the stage's own "
                "profile: its plan digest is the entry's plan_digest."
            )
        if name not in order:
            raise ValueError(
                f"Pre-approval of stage {key!r} names profile {name!r}, which the approved spec "
                "does not list for that stage; a profile not in the spec is never bound."
            )
    missing = [name for name in order[1:] if name not in named]
    if missing:
        raise ValueError(
            f"Pre-approval of stage {key!r} does not cover its alternate profile "
            f"{missing[0]!r}: add an alternate_profiles entry with that profile's plan digest "
            "(the pre-approval preview lists it), or approve without pre-approving the stage."
        )
    if len(order) == 1:
        return None
    return [
        {"profile": name, "plan_digest": entry.plan_digest if index == 0 else named[name]}
        for index, name in enumerate(order)
    ]


def recorded_profiles(connection: sqlite3.Connection, pre: dict[str, Any]) -> list[dict] | None:
    """The profiles this pre-approval covers, as recorded on the approve event (ADR 0151).

    ``None`` for an entry whose candidate listed no alternate profiles (or one approved before
    ADR 0151): it covers its own profile with its ``plan_digest`` only."""
    row = connection.execute(
        """SELECT json_extract(p.value, '$.profiles') AS profiles
        FROM events e JOIN campaigns c ON c.id=?, json_each(e.payload_json, '$.preapprovals') p
        WHERE e.event_type='campaign.approved' AND e.org_id=c.org_id
        AND e.project_id=c.project_id AND json_extract(e.payload_json, '$.campaign_id')=c.id
        AND json_extract(p.value, '$.id')=? LIMIT 1""",
        (pre["campaign_id"], pre["id"]),
    ).fetchone()
    if row is None or not row["profiles"]:
        return None
    return list(json.loads(row["profiles"]))


def _profile_plan_digest(
    connection: sqlite3.Connection,
    campaign: Any,
    pre: dict[str, Any],
    stage_key: str,
    provider_id: str,
    attempt_id: str,
) -> str | None:
    """The plan digest this pre-approval binds for the profile the attempt runs on, or None
    when the attempt's profile is not one it covers (ADR 0151, fail closed to a person).

    A candidate without alternate profiles keeps the pre-ADR-0151 rule unchanged: the entry's
    ``plan_digest`` (its provider was already checked)."""
    from .campaign_delegation import _normal_spec
    from .campaign_models import stage_on_candidate
    from .salad_alternate_profiles import attempt_profile, covered_profiles

    try:
        placed = stage_on_candidate(_normal_spec(campaign).stage(stage_key), provider_id)
        order = covered_profiles(placed)
    except (StopIteration, ValueError, KeyError):
        return None
    if len(order) == 1:
        return pre["plan_digest"]
    recorded = recorded_profiles(connection, pre)
    if recorded is None:
        return None  # a candidate with alternates is covered only by a per-profile entry
    bound = {item["profile"]: item["plan_digest"] for item in recorded}
    running = attempt_profile(connection, attempt_id)
    if running not in order:
        return None
    return bound.get(running)


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
    if _delegation_of(approval, connection) is not None:
        # ADR 0150: a pre-approval recorded under a delegation is only as good as the
        # delegation is now: re-checked at every use, in the resolving transaction too.
        refused = delegated_review_reasons(connection, campaign, approval, stage_key, kind, now)
        if refused:
            return Verdict(",".join(refused), pre)
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
        """SELECT a.attempt AS attempt, a.provider_id AS provider_id, a.id AS attempt_id
        FROM campaign_salad_attempts sa
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
    from .campaign_delegation import _normal_spec

    try:
        own = _normal_spec(campaign).stage(stage_key).provider
    except (StopIteration, ValueError, KeyError):
        return Verdict(UNBOUND_SCOPE, pre)
    if (attempt["provider_id"] or own) != covered_provider(connection, pre, own):
        # An entry covers only the candidate it names: another one waits for a person.
        return Verdict(WRONG_PROVIDER, pre, attempt=attempt["attempt"])
    # ADR 0151: an entry covers only the profiles it names; another one waits for a person.
    plan_digest = _profile_plan_digest(
        connection, campaign, pre, stage_key, attempt["provider_id"] or own, attempt["attempt_id"]
    )
    if plan_digest is None:
        return Verdict(WRONG_PROFILE, pre, attempt=attempt["attempt"])
    try:
        scope = json.loads(decision["scope_json"])
    except ValueError:
        return Verdict(UNBOUND_SCOPE, pre)
    if not _bound(scope, kind, salad):
        return Verdict(UNBOUND_SCOPE, pre)
    digest = review_core_digest(scope)
    expected = plan_digest if kind == KIND_PLAN else pre["source_use_digest"]
    if digest != expected:
        return Verdict(DIGEST_MISMATCH, pre, digest, attempt=attempt["attempt"])
    cents = 0
    if kind == KIND_PLAN:
        cents = _stage_exposure(connection, campaign, stage_key, attempt["provider_id"]) or 0
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


# -- review pre-approval under an owner-signed delegation (ADR 0150) -------------------

# Bound in place of the digest of a review kind the delegation does not cover. Its preimage is
# not a review core, so no review ever digests to it: an uncovered kind can never match.
UNCOVERED_DIGEST = canonical_digest({"type": "delegated_review_kind_not_covered", "version": 1})
NOT_SALAD_STAGE = "stage_provider_not_salad"


REVIEW_DELEGATION_EVENT = "campaign.review_delegation_bound"


def _delegation_of(approval: Any, connection: sqlite3.Connection | None = None) -> str | None:
    """The owner-signed delegation this approval's per-run reviews are resolved under, if any:
    the delegation an agent approved the campaign under (ADR 0150), or the one the approving
    owner bound in a direct approval (``review_delegation_id``, recorded as a
    ``campaign.review_delegation_bound`` event in the approval's own transaction). Without a
    connection only the first is visible."""
    try:
        found = approval["delegation_id"]
    except (KeyError, IndexError):
        found = None
    if found is not None or connection is None:
        return found
    return bound_review_delegation(connection, approval)


def bound_review_delegation(connection: sqlite3.Connection, approval: Any) -> str | None:
    """The delegation the approving owner bound to this approval's reviews, or None."""
    try:
        approval_id, campaign_id = approval["id"], approval["campaign_id"]
    except (KeyError, IndexError):
        return None
    row = connection.execute(
        """SELECT e.payload_json FROM events e JOIN campaigns c ON c.project_id = e.project_id
        WHERE c.id=? AND e.event_type=? AND json_extract(e.payload_json,'$.campaign_id')=?
        AND json_extract(e.payload_json,'$.approval_id')=? ORDER BY e.cursor LIMIT 1""",
        (campaign_id, REVIEW_DELEGATION_EVENT, campaign_id, approval_id),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0]).get("delegation_id")
    return value if isinstance(value, str) else None


def delegated_review_reasons(
    connection: sqlite3.Connection,
    campaign: Any,
    approval: Any,
    stage_key: str,
    kind: str,
    now: Any,
) -> list[str]:
    """Why the delegation an approval was made under does not (any longer) cover this review
    of this stage on its own provider; empty when it does. Fail closed."""
    from .campaign_delegation import _normal_spec
    from .delegation_rules import evaluate_review

    delegation = connection.execute(
        "SELECT * FROM approval_delegations WHERE id=? AND org_id=?",
        (_delegation_of(approval, connection), campaign["org_id"]),
    ).fetchone()
    if delegation is None:
        return ["delegation_missing"]
    try:
        stage = _normal_spec(campaign).stage(stage_key)
    except (StopIteration, ValueError, KeyError):
        return [UNBOUND_SCOPE]
    if _stage_exposure(connection, campaign, stage_key) is None:
        return [NOT_SALAD_STAGE]
    return list(evaluate_review(connection, delegation, campaign, stage, kind, now=now).reasons)


def record_delegated_preapproval(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    approval_id: str,
    stage_key: str,
    digests: dict[str, str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Inside the runner's write transaction, at a stage's first review stop: record the review
    pre-approval the delegation the campaign was approved under writes for this stage, or the
    reasons it does not (nothing is written then).

    The row is the same one an approve body records (ADR 0044) and is enforced by the same
    exact-digest ``evaluate``: the digests are the ones the runner itself derives from the
    approved spec and profile, a kind the rule does not cover binds ``UNCOVERED_DIGEST``, the
    attempt cap is the stage's own hold (never the rule's larger cap), the total is the
    smaller of the campaign's cap and the delegation's per-run cap, and it expires with the
    approval or the delegation, whichever is first. Returns (row, []) or (None, reasons)."""
    from .delegation_rules import parse_terms

    campaign = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    approval = connection.execute(
        "SELECT * FROM campaign_approvals WHERE id=? AND campaign_id=?", (approval_id, campaign_id)
    ).fetchone()
    if campaign is None or approval is None or approval["status"] != "active":
        return None, [APPROVAL_INACTIVE]
    delegation_id = _delegation_of(approval, connection)
    if delegation_id is None:
        return None, [NO_PREAPPROVAL]
    existing = stage_preapproval(connection, approval_id, stage_key)
    if existing is not None:
        return existing, []
    now = _parse_utc(utc_now())
    covered = []
    reasons: list[str] = []
    for kind in KINDS:
        found = delegated_review_reasons(connection, campaign, approval, stage_key, kind, now)
        if not found:
            covered.append(kind)
        reasons += [code for code in found if code not in reasons]
    if not covered:
        return None, reasons
    delegation = connection.execute(
        "SELECT * FROM approval_delegations WHERE id=? AND org_id=?",
        (delegation_id, campaign["org_id"]),
    ).fetchone()
    if delegation is None:
        return None, ["delegation_missing"]
    terms = parse_terms(delegation)
    exposure = _stage_exposure(connection, campaign, stage_key)
    if terms is None or exposure is None:
        return None, ["delegation_terms_invalid"]
    total = min(int(campaign["budget_cap_cents"]), terms.per_run_cap_cents)
    if exposure < 1 or total < exposure:
        return None, ["review_cap_exceeded"]
    expires = min(
        (approval["expires_at"], delegation["expires_at"]), key=lambda value: _parse_utc(value)
    )
    bound = {
        KIND_SOURCE: digests["source_assessment_digest"],
        KIND_PLAN: digests["plan_digest"],
        KIND_SOURCE_USE: digests["source_use_digest"],
    }
    row = {
        "id": "preapproval_" + uuid.uuid4().hex,
        "campaign_id": campaign_id,
        "approval_id": approval_id,
        "stage_key": stage_key,
        "source_digest": bound[KIND_SOURCE] if KIND_SOURCE in covered else UNCOVERED_DIGEST,
        "plan_digest": bound[KIND_PLAN] if KIND_PLAN in covered else UNCOVERED_DIGEST,
        "source_use_digest": (
            bound[KIND_SOURCE_USE] if KIND_SOURCE_USE in covered else UNCOVERED_DIGEST
        ),
        "max_attempt_cents": exposure,
        "max_total_cents": total,
        "expires_at": _utc(expires),
        "created_at": utc_now(),
    }
    connection.execute(
        """INSERT INTO campaign_review_preapprovals (id, campaign_id, approval_id,
        stage_key, source_digest, plan_digest, source_use_digest, max_attempt_cents,
        max_total_cents, expires_at, created_at) VALUES (:id,:campaign_id,:approval_id,
        :stage_key,:source_digest,:plan_digest,:source_use_digest,:max_attempt_cents,
        :max_total_cents,:expires_at,:created_at)""",
        row,
    )
    row["covered_kinds"] = covered
    row["delegation_id"] = delegation["id"]
    row["terms_digest"] = delegation["terms_digest"]
    return row, []


def _utc(value: str) -> str:
    from .store import _utc_string

    return _utc_string(_parse_utc(value))


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
        # ADR 0150: a pre-approval the platform recorded under an owner-signed delegation names
        # that delegation and its terms digest (never the person who signed it).
        approval = connection.execute(
            "SELECT * FROM campaign_approvals WHERE id=?", (row["approval_id"],)
        ).fetchone()
        delegation_id = None if approval is None else _delegation_of(approval, connection)
        delegation = (
            None
            if delegation_id is None
            else connection.execute(
                "SELECT id, terms_digest FROM approval_delegations WHERE id=?", (delegation_id,)
            ).fetchone()
        )
        result.append(
            {
                "id": row["id"],
                "approval_id": row["approval_id"],
                **(
                    {
                        "delegation": {
                            "id": delegation["id"],
                            "terms_digest": delegation["terms_digest"],
                        }
                    }
                    if delegation is not None
                    else {}
                ),
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
    stages (ADR 0044), with each alternate profile's plan digest (ADR 0151). Grants nothing;
    nothing is written."""
    from pathlib import Path

    from .campaign_models import normalize, parse_spec_json, stage_on_candidate, stage_provider
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
        # The stage's own provider, then each Salad alternate it names (ADR 0066): a human
        # binds the digests of the candidate they are willing to have placed.
        for index, candidate in enumerate(
            (stage.provider, *(a.provider for a in stage.alternates))
        ):
            try:
                salad = stage_provider(candidate).descriptor.kind == "salad_route"
            except KeyError:
                salad = False
            if not salad:
                continue
            placed = stage_on_candidate(stage, candidate)
            result.append(
                {
                    **driver.preview_review_digests(
                        ctx, stage.key, None if index == 0 else candidate
                    ),
                    "max_attempt_cents": placed.max_exposure_cents,
                    "max_total_cents": spec.budget_cap_cents,
                    **alternate_profile_digests(driver, campaign, spec, placed),
                }
            )
    return result


def alternate_profile_digests(
    driver: Any, campaign: dict[str, Any], spec: Any, placed: Any
) -> dict[str, list[dict[str, str]]]:
    """``{"alternate_profiles": [{profile, plan_digest}, ...]}`` for a placed Salad candidate
    that lists alternate profiles, else ``{}`` (ADR 0151). Read-only.

    Each plan digest is computed by the driver's own preview, on the stage as it runs on that
    profile (``stage_on_profile``), so the entry a human binds is exactly what ``evaluate``
    compares for an attempt on that profile."""
    import dataclasses

    from .campaign_runner import _Ctx
    from .salad_alternate_profiles import covered_profiles, stage_on_profile

    found = []
    for name in covered_profiles(placed)[1:]:
        moved = stage_on_profile(placed, name)
        shifted = dataclasses.replace(
            spec, stages=tuple(moved if item.key == placed.key else item for item in spec.stages)
        )
        digests = driver.preview_review_digests(_Ctx(campaign, shifted, None, 0), placed.key, None)
        found.append({"profile": name, "plan_digest": digests["plan_digest"]})
    return {"alternate_profiles": found} if found else {}
