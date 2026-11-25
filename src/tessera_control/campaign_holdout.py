"""One-time holdout claims for evaluation stages (ADR 0068).

A v2 stage may declare ``evaluation: {final: true, holdout: {input, partition_digest,
claim_scope}}``. The platform then records at most one claim per (project, partition digest,
claim scope): the runner opens it, in one transaction with its event, before the stage's input
is released to any worker, and only once every stage it depends on has succeeded. A second
campaign (or a resubmission of the same one) that names the same partition and scope is refused
at submit, at approval and, if two approved campaigns race, at the claim itself (a unique key).

A claim is never reopened or deleted (triggers refuse it). It ends ``scored`` with the metric
values of the stage's one succeeded attempt, or ``spent`` when the campaign finished without
them; either way the partition stays claimed for that scope. The claim and its scored values
are on the campaign receipt and readable through ``tessera_mcp``.

Rows exist only for specs that declare a holdout, so every other campaign keeps its receipt
shape and digest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from .campaign_models import NormalSpec, normalize
from .campaign_read_scope import own_clause

CLAIM_STATUSES = ("opened", "scored", "spent")
HOLDOUT_ALREADY_CLAIMED = "holdout_already_claimed"
SEALED_PARTITION = "sealed_partition"


def create_holdout_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge. Additive, idempotent."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS campaign_holdout_claims (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        partition_digest TEXT NOT NULL CHECK(length(partition_digest) = 64),
        claim_scope TEXT NOT NULL CHECK(length(claim_scope) BETWEEN 1 AND 128),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        stage_key TEXT NOT NULL,
        input_name TEXT NOT NULL,
        approval_id TEXT NOT NULL REFERENCES campaign_approvals(id),
        spec_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('opened','scored','spent')),
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        close_reason TEXT,
        scored_attempt INTEGER,
        metrics_json TEXT,
        UNIQUE(project_id, partition_digest, claim_scope),
        UNIQUE(campaign_id, stage_key))""",
        # Durable: a claim is never deleted, and a closed claim never changes again.
        """CREATE TRIGGER IF NOT EXISTS campaign_holdout_claims_no_delete
        BEFORE DELETE ON campaign_holdout_claims
        BEGIN SELECT RAISE(ABORT, 'holdout claims are permanent'); END""",
        """CREATE TRIGGER IF NOT EXISTS campaign_holdout_claims_closed
        BEFORE UPDATE ON campaign_holdout_claims WHEN OLD.status <> 'opened'
        BEGIN SELECT RAISE(ABORT, 'a closed holdout claim cannot change'); END""",
        """CREATE TRIGGER IF NOT EXISTS campaign_holdout_claims_identity
        BEFORE UPDATE ON campaign_holdout_claims WHEN
            NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id
            OR NEW.partition_digest IS NOT OLD.partition_digest
            OR NEW.claim_scope IS NOT OLD.claim_scope OR NEW.campaign_id IS NOT OLD.campaign_id
            OR NEW.stage_key IS NOT OLD.stage_key OR NEW.opened_at IS NOT OLD.opened_at
        BEGIN SELECT RAISE(ABORT, 'a holdout claim keeps its identity'); END""",
    ):
        connection.execute(statement)


def _claim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": row["id"],
        "partition_digest": row["partition_digest"],
        "claim_scope": row["claim_scope"],
        "campaign_id": row["campaign_id"],
        "stage_key": row["stage_key"],
        "input_name": row["input_name"],
        "approval_id": row["approval_id"],
        "spec_digest": row["spec_digest"],
        "status": row["status"],
        "opened_at": row["opened_at"],
        "closed_at": row["closed_at"],
        "close_reason": row["close_reason"],
        "scored_attempt": row["scored_attempt"],
        "metrics": None if row["metrics_json"] is None else json.loads(row["metrics_json"]),
    }


def existing_claim(
    connection: sqlite3.Connection, project_id: str, partition_digest: str, claim_scope: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT * FROM campaign_holdout_claims WHERE project_id=? AND partition_digest=?
        AND claim_scope=?""",
        (project_id, partition_digest, claim_scope),
    ).fetchone()
    return None if row is None else _claim(row)


def sealed_input_refusal(connection: sqlite3.Connection, project_id: str, stage: Any) -> str | None:
    """Why this stage may not receive one of its inputs, or ``None`` (ADR 0068).

    * A sealed partition (``sealed_digests``: claimed, or declared by an approved campaign) is
      read only by a holdout stage that names it as its holdout input.
    * An ``artifact`` input may not come from another campaign's holdout stage: a scorer's
      outputs (per-example predictions, for example) must not feed back into selection.

    Used at submit and approval, and again by the runner before each input release, so a
    campaign approved before the partition was sealed is still refused once it is."""
    claimed = sealed_digests(connection, project_id)
    holdout = stage.holdout
    for item in stage.inputs:
        if item.kind == "public_artifact":
            continue  # public data fetched on the provider (ADR 0097) is never a sealed partition
        digest = item.digest if item.kind == "snapshot" else item.sha256
        if digest in claimed and (holdout is None or holdout.input != item.name):
            return (
                f"Stage {stage.key}: input {item.name!r} is a sealed partition (claimed, or "
                "declared by an approved holdout stage); "
                "only a final holdout evaluation stage may read it"
            )
        if item.kind == "artifact" and _holdout_stage(
            connection, project_id, item.campaign_id, item.stage
        ):
            return (
                f"Stage {stage.key}: input {item.name!r} is an output of holdout stage "
                f"{item.stage} of campaign {item.campaign_id}; a held-out score never feeds "
                "another campaign"
            )
    return None


def sealed_digests(connection: sqlite3.Connection, project_id: str) -> set[str]:
    """Partitions sealed in the project: every claimed digest, and every holdout digest an
    approved campaign declares (ADR 0068). A partition is sealed from the first human approval
    of a spec that names it as a holdout, before any claim exists, so a selection stage in
    another campaign cannot read it in that window. Specs never approved seal nothing, so an
    unapproved submission cannot block anyone's data."""
    sealed = {
        row["partition_digest"]
        for row in connection.execute(
            "SELECT DISTINCT partition_digest FROM campaign_holdout_claims WHERE project_id=?",
            (project_id,),
        )
    }
    for row in connection.execute(
        """SELECT c.id, c.spec_json FROM campaigns c WHERE c.project_id=?
        AND c.spec_json LIKE '%"holdout"%'
        AND EXISTS (SELECT 1 FROM campaign_approvals a WHERE a.campaign_id=c.id)""",
        (project_id,),
    ):
        sealed.update(_declared_holdouts(row["id"], row["spec_json"]))
    return sealed


# Declared holdout digests per campaign, so a release does not parse every approved holdout
# spec in the project again. Keyed by campaign id and checked against the stored spec text, so
# a changed spec is parsed afresh. Guarded by a lock because the runner and API may share a
# process; bounded, evicting the oldest entry when full.
_DECLARED_LOCK = threading.Lock()
_DECLARED: dict[str, tuple[str, frozenset[str]]] = {}
_DECLARED_LIMIT = 4096


def _declared_holdouts(campaign_id: str, spec_json: str) -> frozenset[str]:
    with _DECLARED_LOCK:
        cached = _DECLARED.get(campaign_id)
    if cached is not None and cached[0] == spec_json:
        return cached[1]
    from .campaign_models import parse_spec_json

    try:
        stages = normalize(parse_spec_json(spec_json)).stages
    except ValueError:
        declared: frozenset[str] = frozenset()  # an unparseable stored spec seals nothing
    else:
        declared = frozenset(s.holdout.partition_digest for s in stages if s.holdout is not None)
    with _DECLARED_LOCK:
        _DECLARED.pop(campaign_id, None)
        while len(_DECLARED) >= _DECLARED_LIMIT:
            del _DECLARED[next(iter(_DECLARED))]  # dicts keep insertion order: drop the oldest
        _DECLARED[campaign_id] = (spec_json, declared)
    return declared


def holdout_output_source(
    connection: sqlite3.Connection, project_id: str, manifest_id: str
) -> tuple[str, str] | None:
    """``(campaign_id, stage_key)`` when the manifest holds a holdout stage's outputs."""
    row = connection.execute(
        """SELECT campaign_id, stage_key FROM campaign_stage_attempts
        WHERE manifest_id=? LIMIT 1""",
        (manifest_id,),
    ).fetchone()
    if row is None or not _holdout_stage(
        connection, project_id, row["campaign_id"], row["stage_key"]
    ):
        return None
    return row["campaign_id"], row["stage_key"]


def _holdout_stage(
    connection: sqlite3.Connection, project_id: str, campaign_id: str | None, stage_key: str | None
) -> bool:
    row = connection.execute(
        "SELECT spec_json FROM campaigns WHERE id=? AND project_id=?", (campaign_id, project_id)
    ).fetchone()
    if row is None:
        return False  # resolve_artifact refuses an unknown or foreign source on its own
    from .campaign_models import parse_spec_json

    try:
        stages = normalize(parse_spec_json(row["spec_json"])).stages
    except ValueError:
        return False
    return any(item.key == stage_key and item.holdout is not None for item in stages)


def check_unclaimed(
    connection: sqlite3.Connection, project_id: str, spec: Any, campaign_id: str | None = None
) -> None:
    """Submit and approval: refuse a spec whose holdout is already claimed in the project,
    and any stage ``sealed_input_refusal`` refuses. ``campaign_id`` is the campaign being
    re-approved: its own claim never counts against it. Raises ValueError."""
    normal: NormalSpec = normalize(spec)
    for stage in normal.stages:
        holdout = stage.holdout
        if holdout is not None:
            found = existing_claim(
                connection, project_id, holdout.partition_digest, holdout.claim_scope
            )
            if found is not None and found["campaign_id"] != campaign_id:
                raise ValueError(
                    f"Stage {stage.key}: sealed partition {holdout.partition_digest[:12]} was "
                    f"already claimed for scope {holdout.claim_scope!r} by campaign "
                    f"{found['campaign_id']} ({found['status']}); a holdout opens once"
                )
        reason = sealed_input_refusal(connection, project_id, stage)
        if reason is not None:
            raise ValueError(reason)


def open_claim(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    approval: Mapping[str, Any],
    stage: Any,
    now: str,
) -> tuple[bool, dict[str, Any]]:
    """Open (or find this stage's own) claim inside the caller's transaction.

    Returns ``(True, claim)`` when the stage holds the claim, ``(False, other)`` when another
    stage or campaign already holds it. Every stage the claiming stage depends on must have
    succeeded (the runner only gets here then; this re-checks it in the same transaction)."""
    holdout = stage.holdout
    assert holdout is not None
    own = connection.execute(
        "SELECT * FROM campaign_holdout_claims WHERE campaign_id=? AND stage_key=?",
        (campaign["id"], stage.key),
    ).fetchone()
    if own is not None:
        return True, _claim(own)
    upstream = sorted({item.stage for item in stage.inputs if item.kind == "stage"})
    for key in upstream:
        row = connection.execute(
            "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            (campaign["id"], key),
        ).fetchone()
        if row is None or row["status"] != "succeeded":
            raise RuntimeError(f"holdout stage {stage.key} opened before {key} succeeded")
    found = existing_claim(
        connection, campaign["project_id"], holdout.partition_digest, holdout.claim_scope
    )
    if found is not None:
        return False, found
    claim_id = "holdout_claim_" + uuid.uuid4().hex
    try:
        _insert_claim(connection, claim_id, campaign, approval, stage, now)
    except sqlite3.IntegrityError:
        # The unique keys are the final arbiter: if another writer got there between the
        # lookup and the insert (only possible if the caller's transaction were weakened), the
        # statement is rolled back and the caller gets the same clean refusal as above.
        own = connection.execute(
            "SELECT * FROM campaign_holdout_claims WHERE campaign_id=? AND stage_key=?",
            (campaign["id"], stage.key),
        ).fetchone()
        if own is not None:
            return True, _claim(own)
        found = existing_claim(
            connection, campaign["project_id"], holdout.partition_digest, holdout.claim_scope
        )
        if found is None:
            raise
        return False, found
    row = connection.execute(
        "SELECT * FROM campaign_holdout_claims WHERE id=?", (claim_id,)
    ).fetchone()
    return True, _claim(row)


def _insert_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    campaign: Mapping[str, Any],
    approval: Mapping[str, Any],
    stage: Any,
    now: str,
) -> None:
    holdout = stage.holdout
    connection.execute(
        """INSERT INTO campaign_holdout_claims (id, org_id, project_id, partition_digest,
        claim_scope, campaign_id, stage_key, input_name, approval_id, spec_digest, status,
        opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,'opened',?)""",
        (
            claim_id,
            campaign["org_id"],
            campaign["project_id"],
            holdout.partition_digest,
            holdout.claim_scope,
            campaign["id"],
            stage.key,
            holdout.input,
            approval["id"],
            campaign["spec_digest"],
            now,
        ),
    )


def score_claim(
    connection: sqlite3.Connection,
    campaign_id: str,
    stage_key: str,
    attempt: Mapping[str, Any],
    now: str,
) -> None:
    """Record the succeeded attempt's measured values on its open claim (same transaction)."""
    metrics = {
        item["name"]: item.get("value")
        for item in json.loads(attempt["measurements_json"] or "[]")
        if not str(item.get("name", "")).startswith("evidence_rows.")
    }
    connection.execute(
        """UPDATE campaign_holdout_claims SET status='scored', closed_at=?,
        close_reason='stage_succeeded', scored_attempt=?, metrics_json=?
        WHERE campaign_id=? AND stage_key=? AND status='opened'""",
        (
            now,
            attempt["attempt"],
            json.dumps(metrics, sort_keys=True),
            campaign_id,
            stage_key,
        ),
    )


def spend_open_claims(connection: sqlite3.Connection, campaign_id: str, now: str) -> None:
    """A campaign that finishes with a claim still open spends it: the partition stays
    claimed, with the stage's final status as the reason and no scored values."""
    for row in connection.execute(
        """SELECT c.id, s.status FROM campaign_holdout_claims c LEFT JOIN campaign_stages s
        ON s.campaign_id=c.campaign_id AND s.stage_key=c.stage_key
        WHERE c.campaign_id=? AND c.status='opened'""",
        (campaign_id,),
    ).fetchall():
        connection.execute(
            """UPDATE campaign_holdout_claims SET status='spent', closed_at=?, close_reason=?
            WHERE id=? AND status='opened'""",
            (now, f"stage_{row['status'] or 'unknown'}", row["id"]),
        )


def campaign_claims(
    connection: sqlite3.Connection, campaign_id: str, limit: int = -1
) -> list[dict[str, Any]]:
    """A campaign's claims, oldest first; ``limit`` caps the rows (-1: no cap)."""
    return [
        _claim(row)
        for row in connection.execute(
            """SELECT * FROM campaign_holdout_claims WHERE campaign_id=?
            ORDER BY opened_at, id LIMIT ?""",
            (campaign_id, limit),
        )
    ]


def receipt_section(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """Absent for a campaign without a claim, so existing receipts keep their shape."""
    claims = campaign_claims(connection, campaign_id)
    return {"holdout_claims": claims} if claims else {}


def project_claims(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    partition_digest: str | None = None,
    limit: int = 200,
    *,
    reader: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Tenant-scoped: the caller has already checked the project; the org is re-applied.

    ``reader`` (an agent, audit O-05) keeps only claims of campaigns that reader may read."""
    where = "h.org_id=? AND h.project_id=?"
    params: list[Any] = [org_id, project_id]
    if partition_digest is not None:
        where += " AND h.partition_digest=?"
        params.append(partition_digest)
    if reader is not None:
        clause, own_args = own_clause(reader, "h.campaign_id")
        where += clause
        params.extend(own_args)
    rows = connection.execute(
        f"SELECT h.* FROM campaign_holdout_claims h WHERE {where} "
        "ORDER BY h.opened_at, h.id LIMIT ?",
        (*params, limit),
    )
    return [_claim(row) for row in rows]
