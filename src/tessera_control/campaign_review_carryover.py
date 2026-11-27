"""An approved Salad review carried over to the attempt requeued after a pre-route lapse.

A Salad attempt is packaged under its execution lease before its plan and source-use reviews
are proposed, because those scopes bind the run-minted operation. The lease's hard expiry is
fixed when it is issued (the grant, the worker credential and the task's duration bound), so
a review slower than that bound always lapses it, and the attempt is requeued as not started
(ADR 0046 amendment). Without a carryover the requeued attempt proposes fresh reviews and stops
for a person again, every time: the review loops.

The requeued attempt's pending review is resolved by the campaign runner only when the person's
approval of the same review on the immediately preceding attempt covers it exactly:

* that attempt was taken off its route for a pre-route lapse (``REQUEUE_EVENT`` with a lapse
  reason) and closed ``failed_transient``; nothing was sent to the provider;
* its review of the same kind is ``approved`` and the reviewer is the same principal;
* both scopes bind this stage (``_bound``), and their cores, the scope without the ids minted at
  run time, digest identically (never approve more than was reviewed);
* the stage runs on the same provider, and the person's approval is still valid: traced back
  through any chain of carryovers to the root decision a person resolved (the reviewer), it was
  given before that review's pending deadline and is less than ``APPROVAL_HOURS`` past its
  proposal. The earliest end along the chain governs, so a carryover never extends the window.
  The pending deadline itself (``expires_at``, which the campaign grant may shorten and which
  can fall exactly at the lease's lapse) bounds when the person may answer, not how long the
  answer holds.

Anything else leaves the new review pending for a person. The check runs twice: on the runner
(whether to try) and again inside the resolving transaction (``authorize``), which records
``campaign.review_approval_carried_over`` there, so the receipt shows every carryover.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .campaign_salad_lease_lapse import (
    LEASE_LAPSED_BEFORE_ROUTE,
    LEASE_WINDOW_SHORT_BEFORE_ROUTE,
    REQUEUE_EVENT,
    WORKER_UNAVAILABLE_BEFORE_ROUTE,
)
from .store import Forbidden, _parse_utc

CARRYOVER_EVENT = "campaign.review_approval_carried_over"
DECLINED_EVENT = "campaign.review_carryover_declined"
LAPSE_REASONS = frozenset(
    {LEASE_LAPSED_BEFORE_ROUTE, WORKER_UNAVAILABLE_BEFORE_ROUTE, LEASE_WINDOW_SHORT_BEFORE_ROUTE}
)
_COLUMNS = {"plan": "plan_decision_id", "source_use": "source_decision_id"}

NOT_PENDING = "review_not_pending"
NO_PRIOR = "no_prior_attempt"
PRIOR_NOT_LAPSED = "prior_not_lapsed"
PRIOR_NOT_APPROVED = "prior_not_approved"
OTHER_REVIEWER = "other_reviewer"
OTHER_PROVIDER = "other_provider"
UNBOUND = "unbound_scope"
DIGEST_MISMATCH = "digest_mismatch"
EXPIRED = "prior_review_expired"
OTHER_RESOLVER = "other_resolver"
BROKEN_CHAIN = "carryover_chain_broken"
APPROVAL_HOURS = 23  # a person's approval holds this long past the proposal (DECISION_HOURS)
MAX_CHAIN = 16
_DENIED = "Campaign review carryover does not authorize this review."


@dataclass(frozen=True)
class Carryover:
    reason: str | None  # None: covered
    prior_decision_id: str | None = None
    prior_attempt: int | None = None
    attempt: int | None = None
    digest: str | None = None
    root_decision_id: str | None = None


def _scope(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        scope = json.loads(row["scope_json"])
    except (TypeError, ValueError):
        return None
    return scope if isinstance(scope, dict) else None


def _root_approval(
    connection: sqlite3.Connection, decision_id: str, now: datetime
) -> tuple[sqlite3.Row | None, str | None]:
    """Follow carryover events back to the decision a person resolved; (root, None) when every
    link was approved inside its pending deadline and is within the approval window, else
    (None, reason). Fail closed on a missing, cyclic or over-long chain."""
    seen: set[str] = set()
    current = decision_id
    while True:
        if current in seen or len(seen) >= MAX_CHAIN:
            return None, BROKEN_CHAIN
        seen.add(current)
        row = connection.execute(
            "SELECT * FROM decision_requests WHERE id=?", (current,)
        ).fetchone()
        if row is None or row["status"] != "approved":
            return None, PRIOR_NOT_APPROVED
        try:
            resolved = _parse_utc(row["resolved_at"])
            if resolved >= _parse_utc(row["expires_at"]) or now >= _parse_utc(
                row["created_at"]
            ) + timedelta(hours=APPROVAL_HOURS):
                return None, EXPIRED
        except (TypeError, ValueError):
            return None, EXPIRED
        link = connection.execute(
            """SELECT json_extract(payload_json,'$.carried_from_decision_id') AS source
            FROM events WHERE event_type=?
            AND json_extract(payload_json,'$.decision_id')=? LIMIT 1""",
            (CARRYOVER_EVENT, current),
        ).fetchone()
        if link is None:
            return row, None
        if not link["source"]:
            return None, BROKEN_CHAIN
        current = link["source"]


def evaluate(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    stage_key: str,
    kind: str,
    subject_id: str,
    now: datetime | None = None,
) -> Carryover:
    """Pure read: would the preceding attempt's approval cover resolving ``subject_id``."""
    from .campaign_preapproval import _bound, review_core_digest

    column = _COLUMNS.get(kind)
    if column is None:
        return Carryover(UNBOUND)
    current = connection.execute(
        f"""SELECT a.id, a.attempt, a.provider_id FROM campaign_salad_attempts sa
        JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
        WHERE a.campaign_id=? AND a.stage_key=? AND sa.{column}=?""",
        (campaign_id, stage_key, subject_id),
    ).fetchone()
    decision = connection.execute(
        "SELECT * FROM decision_requests WHERE id=?", (subject_id,)
    ).fetchone()
    if current is None or decision is None:
        return Carryover(UNBOUND)
    if decision["status"] != "pending":
        return Carryover(NOT_PENDING)
    prior = connection.execute(
        f"""SELECT a.id, a.attempt, a.provider_id, a.status, sa.{column} AS decision_id
        FROM campaign_stage_attempts a
        JOIN campaign_salad_attempts sa ON sa.attempt_id = a.id
        WHERE a.campaign_id=? AND a.stage_key=? AND a.attempt<?
        ORDER BY a.attempt DESC LIMIT 1""",
        (campaign_id, stage_key, current["attempt"]),
    ).fetchone()
    if prior is None or prior["decision_id"] is None:
        return Carryover(NO_PRIOR, attempt=current["attempt"])
    lapsed = connection.execute(
        """SELECT json_extract(payload_json,'$.reason') AS reason FROM events
        WHERE event_type=? AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
        (REQUEUE_EVENT, campaign_id, prior["id"]),
    ).fetchone()
    if (
        prior["status"] != "failed_transient"
        or lapsed is None
        or lapsed["reason"] not in LAPSE_REASONS
    ):
        return Carryover(PRIOR_NOT_LAPSED, attempt=current["attempt"])
    approved = connection.execute(
        "SELECT * FROM decision_requests WHERE id=?", (prior["decision_id"],)
    ).fetchone()
    if approved is None or approved["status"] != "approved":
        return Carryover(PRIOR_NOT_APPROVED, prior["decision_id"], prior["attempt"])
    base = Carryover(None, prior["decision_id"], prior["attempt"], current["attempt"])
    if (
        approved["reviewer_principal_id"] != decision["reviewer_principal_id"]
        or approved["kind"] != decision["kind"]
        or approved["project_id"] != decision["project_id"]
    ):
        return Carryover(OTHER_REVIEWER, base.prior_decision_id, base.prior_attempt)
    if prior["provider_id"] != current["provider_id"]:
        return Carryover(OTHER_PROVIDER, base.prior_decision_id, base.prior_attempt)
    root, problem = _root_approval(connection, prior["decision_id"], now or datetime.now(UTC))
    if root is None:
        return Carryover(problem or EXPIRED, base.prior_decision_id, base.prior_attempt)
    if (
        root["resolved_by_principal_id"] != decision["reviewer_principal_id"]
        or root["kind"] != decision["kind"]
        or root["project_id"] != decision["project_id"]
    ):
        return Carryover(OTHER_RESOLVER, base.prior_decision_id, base.prior_attempt)
    salad = connection.execute(
        "SELECT * FROM campaign_salad_stages WHERE campaign_id=? AND stage_key=?",
        (campaign_id, stage_key),
    ).fetchone()
    new_scope, old_scope = _scope(decision), _scope(approved)
    if (
        salad is None
        or new_scope is None
        or old_scope is None
        or new_scope.get("action") != kind
        or old_scope.get("action") != kind
        or not _bound(new_scope, kind, salad)
        or not _bound(old_scope, kind, salad)
    ):
        return Carryover(UNBOUND, base.prior_decision_id, base.prior_attempt)
    digest = review_core_digest(new_scope)
    if digest != review_core_digest(old_scope):
        return Carryover(DIGEST_MISMATCH, base.prior_decision_id, base.prior_attempt)
    root_scope = _scope(root)
    if root_scope is None or review_core_digest(root_scope) != digest:
        return Carryover(DIGEST_MISMATCH, base.prior_decision_id, base.prior_attempt)
    return Carryover(
        None, base.prior_decision_id, base.prior_attempt, base.attempt, digest, root["id"]
    )


def available(
    connection: sqlite3.Connection, campaign_id: str, stage_key: str, attempt: int
) -> bool:
    """True when the attempt before ``attempt`` was requeued for a pre-route lapse with both of
    its reviews approved: its replacement's reviews will be offered for carryover."""
    prior = connection.execute(
        """SELECT a.id, a.status, sa.plan_decision_id, sa.source_decision_id
        FROM campaign_stage_attempts a
        JOIN campaign_salad_attempts sa ON sa.attempt_id = a.id
        WHERE a.campaign_id=? AND a.stage_key=? AND a.attempt<?
        ORDER BY a.attempt DESC LIMIT 1""",
        (campaign_id, stage_key, attempt),
    ).fetchone()
    if prior is None or prior["status"] != "failed_transient":
        return False
    lapsed = connection.execute(
        """SELECT json_extract(payload_json,'$.reason') AS reason FROM events
        WHERE event_type=? AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
        (REQUEUE_EVENT, campaign_id, prior["id"]),
    ).fetchone()
    if lapsed is None or lapsed["reason"] not in LAPSE_REASONS:
        return False
    for decision_id in (prior["plan_decision_id"], prior["source_decision_id"]):
        row = connection.execute(
            "SELECT status FROM decision_requests WHERE id=?", (decision_id,)
        ).fetchone()
        if row is None or row["status"] != "approved":
            return False
    return True


def authorize(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    intent: sqlite3.Row,
    *,
    kind: str,
    subject_id: str,
    prior_id: str,
) -> None:
    """Inside the resolving transaction: re-check the carryover and record it, or refuse."""
    from .store import Store

    verdict = evaluate(
        connection,
        campaign_id=intent["campaign_id"],
        stage_key=intent["stage_key"],
        kind=kind,
        subject_id=subject_id,
    )
    if verdict.reason is not None:
        raise Forbidden(f"{_DENIED} ({verdict.reason})")
    if verdict.prior_decision_id != prior_id or verdict.attempt != intent["attempt"]:
        raise Forbidden(_DENIED)
    decision = connection.execute(
        "SELECT project_id, task_id, revision FROM decision_requests WHERE id=?", (subject_id,)
    ).fetchone()
    Store._event(
        connection,
        live,
        decision["project_id"],
        decision["task_id"],
        CARRYOVER_EVENT,
        decision["revision"] + 1,
        {
            "campaign_id": intent["campaign_id"],
            "stage_key": intent["stage_key"],
            "kind": kind,
            "decision_id": subject_id,
            "attempt": verdict.attempt,
            "carried_from_decision_id": prior_id,
            "carried_from_attempt": verdict.prior_attempt,
            "review_digest": verdict.digest,
            "root_decision_id": verdict.root_decision_id,
        },
    )


def summary(connection: sqlite3.Connection, campaign_id: str) -> list[dict[str, Any]]:
    """Every carryover of the campaign, for its receipt (empty when none)."""
    rows = connection.execute(
        """SELECT payload_json FROM events WHERE event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY created_at, rowid""",
        (CARRYOVER_EVENT, campaign_id),
    ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        result.append(
            {
                name: payload.get(name)
                for name in (
                    "stage_key",
                    "kind",
                    "decision_id",
                    "attempt",
                    "carried_from_decision_id",
                    "carried_from_attempt",
                    "review_digest",
                    "root_decision_id",
                )
            }
        )
    return result
