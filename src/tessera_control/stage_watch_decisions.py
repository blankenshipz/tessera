"""Record a worker's watch decision request as one advisory event (ADR 0087 s4, ADR 0111).

A decision request is what the stage watcher would ask the runner to consider: a retry or a
resize of the stage's next attempt. The agent never performs one, and in this slice nothing
admits one either: the route records exactly one ``execution.watch_decision_recorded`` event
with ``admission: "not_admitted"`` and changes nothing else. It never creates an attempt and
never writes grants, holds, budget, custody, observations or attempt rows. Admission by the
runner moves spend and is a later, separately reviewed slice.

``bounds`` is control's own reading of the request against the *approved* spec of the
campaign stage that owns the operation (never the worker's envelope), and it is conservative:
anything control cannot show inside the approved policy is ``within_envelope: false`` with a
``blocked_by`` code. A request outside the envelope is still recorded, as a receipt of what the
agent asked.

The fences are those of the report route (``stage_watch``): the calling worker's operation,
its lease and epoch, an unresolved grant, the ``observed_at`` window (``too_early`` 425 is
the one transient refusal), a strictly increasing ``decision_seq``, at most
``MAX_DECISIONS_PER_OPERATION`` recorded decisions, and the closed kind-to-reason pairing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .campaign_autoreconcile import not_started_count
from .campaign_infra_retry import infra_attempts
from .campaign_models import normalize, parse_spec_json
from .execution import MAX_OBSERVATION_FUTURE_SKEW, UNRESOLVED_GRANT_STATUSES, ExecutionStore
from .stage_watch import OBSERVATION_PROOF, WatchRefused
from .stage_watch_decisions_models import RecordWatchDecision
from .stage_watch_limits import DECISION_KIND_REASONS, MAX_DECISIONS_PER_OPERATION
from .store import Conflict, NotFound, _parse_utc, _utc_string

DECISION_EVENT_TYPE = "execution.watch_decision_recorded"
ADMISSION = "not_admitted"
# Campaign attempt numbers are 1..5 (``campaign_stage_attempts.attempt``), the runner's
# ``MAX_ATTEMPT_NUMBERS``.
MAX_ATTEMPT_NUMBER = 5

MISMATCH_DETAIL = "Watch decision request does not match the external operation."


def _decision_events(
    connection: sqlite3.Connection, project_id: str, operation_id: str
) -> list[dict[str, Any]]:
    # Same project-first lookup as the report route (``events_by_project_cursor``).
    rows = connection.execute(
        """SELECT payload_json FROM events
        WHERE project_id = ? AND event_type = ?
        AND json_extract(payload_json, '$.operation_id') = ?
        ORDER BY cursor""",
        (project_id, DECISION_EVENT_TYPE, operation_id),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def _blocked(code: str, **extra: Any) -> dict[str, Any]:
    return {"within_envelope": False, "blocked_by": code, **extra}


def _owning_stage(
    connection: sqlite3.Connection, operation_id: str, grant_id: str
) -> tuple[sqlite3.Row, Any] | dict[str, Any]:
    """The campaign attempt owning the operation and its approved stage, or blocked bounds."""
    attempt = connection.execute(
        "SELECT * FROM campaign_stage_attempts WHERE operation_id=? AND grant_id=?",
        (operation_id, grant_id),
    ).fetchone()
    if attempt is None:
        return _blocked("no_campaign_stage")
    campaign = connection.execute(
        "SELECT * FROM campaigns WHERE id=?", (attempt["campaign_id"],)
    ).fetchone()
    approved = connection.execute(
        """SELECT 1 FROM campaign_approvals
        WHERE campaign_id=? AND status='active' AND spec_digest=?""",
        (campaign["id"], campaign["spec_digest"]),
    ).fetchone()
    if approved is None:
        return _blocked("spec_not_approved")
    try:
        stage = normalize(parse_spec_json(campaign["spec_json"])).stage(attempt["stage_key"])
    except (ValueError, StopIteration):
        return _blocked("spec_unreadable")
    return attempt, stage


def _retry_bounds(
    connection: sqlite3.Connection, operation_id: str, grant_id: str, reason: str
) -> dict[str, Any]:
    """Whether the approved stage still allows another attempt of this request's class.

    Mirrors the runner's own bounds without deciding anything: attempt numbers left, the
    combined ``max_attempts + infra_retries`` worst case the approval counted, and the class
    bound. ``transient_exit`` needs declared ``transient_exit_codes`` and a scientific retry
    left (control does not have the exit code, so only the class and the count are checked).
    ``probe_lost`` is never inside: an effect whose outcome is unknown is not retried.
    """
    owned = _owning_stage(connection, operation_id, grant_id)
    if isinstance(owned, dict):
        return owned
    attempt, stage = owned
    retry = stage.retry
    campaign = connection.execute(
        "SELECT status FROM campaigns WHERE id=?", (attempt["campaign_id"],)
    ).fetchone()
    stage_row = connection.execute(
        "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
        (attempt["campaign_id"], attempt["stage_key"]),
    ).fetchone()
    attempts = connection.execute(
        """SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=?
        ORDER BY attempt""",
        (attempt["campaign_id"], attempt["stage_key"]),
    ).fetchall()
    used = stage_row["attempts_used"]
    infra_used = infra_attempts(attempts)
    scientific_used = used - not_started_count(attempts) - infra_used
    facts = {
        "stage_key": attempt["stage_key"],
        "attempt": attempt["attempt"],
        "attempts_used": used,
        "max_attempts": retry.max_attempts,
        "infra_retries": retry.infra_retries,
        "scientific_attempts_used": scientific_used,
        "infra_retries_used": infra_used,
    }
    if reason == "probe_lost":
        blocked = "outcome_unknown"
    elif campaign["status"] != "running":
        blocked = "campaign_not_running"
    elif attempts[-1]["id"] != attempt["id"]:
        blocked = "not_latest_attempt"
    elif used >= MAX_ATTEMPT_NUMBER:
        blocked = "attempt_numbers_exhausted"
    elif used >= retry.max_attempts + retry.infra_retries:
        blocked = "retry_policy_exhausted"
    elif not retry.transient_exit_codes:
        blocked = "transient_exit_not_declared"
    elif scientific_used >= retry.max_attempts:
        blocked = "scientific_retries_exhausted"
    else:
        return {"within_envelope": True, "blocked_by": None, **facts}
    return _blocked(blocked, **facts)


def _resize_bounds(
    connection: sqlite3.Connection, operation_id: str, grant_id: str
) -> dict[str, Any]:
    """A resize is inside only below a memory cap the approved spec states.

    No spec version declares a stage memory size today (a Salad profile's memory is operator
    configuration, not part of the approved spec), so the cap is never determinable here and
    every resize is ``within_envelope: false`` with ``cap_unknown: true``.
    """
    owned = _owning_stage(connection, operation_id, grant_id)
    if isinstance(owned, dict):
        return owned | {"cap_unknown": True, "memory_cap_bytes": None}
    return _blocked("cap_unknown", cap_unknown=True, memory_cap_bytes=None)


def record_watch_decision(
    store: ExecutionStore,
    principal: dict[str, Any],
    operation_id: str,
    key: str,
    digest: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    payload = store._body(RecordWatchDecision, payload)
    path = f"/worker/operations/{operation_id}/watch-decisions"

    def command(
        connection: sqlite3.Connection, worker: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        operation = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if (
            operation is None
            or operation["worker_principal_id"] != worker["id"]
            or operation["org_id"] != worker["org_id"]
        ):
            # Same code and 404 for another worker's operation: no existence oracle.
            raise WatchRefused("unknown_operation", "External operation was not found.")
        try:
            store._fence_recovered_operation(connection, operation_id)
        except Conflict as error:
            raise WatchRefused("worker_fenced", str(error)) from None
        if operation["lease_id"] != payload["lease_id"]:
            raise WatchRefused("lease_mismatch", MISMATCH_DETAIL)
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
        ).fetchone()
        if (
            lease is None
            or lease["epoch"] != payload["expected_epoch"]
            or operation["epoch"] != lease["epoch"]
        ):
            raise WatchRefused("stale_epoch", "Watch decision request epoch is no longer current.")
        try:
            grant = store._grant(connection, worker, operation["grant_id"])
        except NotFound as error:
            raise WatchRefused("wrong_worker", str(error)) from None
        if (
            grant["worker_principal_id"] != worker["id"]
            or grant["project_id"] != operation["project_id"]
            or lease["grant_id"] != grant["id"]
        ):
            raise WatchRefused("grant_mismatch", MISMATCH_DETAIL)
        if grant["status"] not in UNRESOLVED_GRANT_STATUSES:
            raise WatchRefused(
                "grant_reconciled", "Execution authority has already been reconciled."
            )
        kind, reason = payload["kind"], payload["reason"]
        if reason not in DECISION_KIND_REASONS[kind]:
            raise WatchRefused(
                "decision_not_allowed", f"A {kind} may not give the reason {reason}."
            )
        observed_at = _parse_utc(payload["observed_at"])
        if observed_at < _parse_utc(lease["issued_at"]):
            raise WatchRefused(
                "before_lease", "Watch decision timestamp is outside the accepted window."
            )
        if observed_at > store._now() + MAX_OBSERVATION_FUTURE_SKEW:
            raise WatchRefused("too_early", "observed_at is ahead of the control clock; retry")
        recorded = _decision_events(connection, operation["project_id"], operation_id)
        if recorded and (
            payload["decision_seq"] <= recorded[-1]["decision_seq"]
            or observed_at < _parse_utc(recorded[-1]["observed_at"])
        ):
            raise WatchRefused(
                "out_of_order", "Watch decision requests cannot be recorded out of order."
            )
        if len(recorded) >= MAX_DECISIONS_PER_OPERATION:
            raise WatchRefused(
                "decision_budget_exhausted", "Watch decision request budget exhausted."
            )
        if kind == "retry_request":
            bounds = _retry_bounds(connection, operation_id, grant["id"], reason)
        else:
            bounds = _resize_bounds(connection, operation_id, grant["id"])
        event = {
            "grant_id": grant["id"],
            "lease_id": lease["id"],
            "operation_id": operation_id,
            "epoch": lease["epoch"],
            "kind": kind,
            "reason": reason,
            "decision_seq": payload["decision_seq"],
            "requested_memory_bytes": payload["requested_memory_bytes"],
            "contract_digest": payload["contract_digest"],
            "observed_at": _utc_string(observed_at),
            "operational_only": True,
            "observation_proof": OBSERVATION_PROOF,
            "admission": ADMISSION,
            "bounds": bounds,
        }
        store._event(
            connection,
            worker,
            operation["project_id"],
            operation["task_id"],
            DECISION_EVENT_TYPE,
            lease["epoch"],
            event,
        )
        return 201, {
            "recorded": True,
            "admitted": False,
            "within_envelope": bounds["within_envelope"],
            "kind": kind,
            "decision_seq": payload["decision_seq"],
            "operation_id": operation_id,
        }

    return store._mutate(principal, key, "POST", path, digest, command, audience="worker")


def decision_receipt_entry(event: dict[str, Any]) -> dict[str, Any]:
    """One recorded decision as the receipt's ``stage_watch`` attempt entry lists it."""
    return {
        "kind": event["kind"],
        "reason": event["reason"],
        "decision_seq": event["decision_seq"],
        "requested_memory_bytes": event["requested_memory_bytes"],
        "within_envelope": event["bounds"]["within_envelope"],
        "admission": event["admission"],
        "observed_at": event["observed_at"],
    }
