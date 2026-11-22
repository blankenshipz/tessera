"""Record a worker's stage watch report as one append-only event (ADR 0087 sections 3, 7, 8).

A stage watch report is an unverified, operational worker assertion: a heartbeat, a
progress tick, or a stuck or failed signal with a few bounded integer counters. It is
recorded as one ``execution.watch_reported`` event and nothing else. It never writes
execution observations, external operations, budget, hold or settlement, custody or
attempt state, so it cannot affect money, custody or whether a stage succeeded. No table
is added: the record is an event, and the bounds below are read back from the events.

Bounds, all per operation and failing closed (numbers in ``stage_watch_limits``, shared
with the worker's stage contract):

Every refusal raised here carries a closed ``reason`` code (``stage_watch_limits.REASON_STATUS``
gives its HTTP status) so the worker can tell a permanent refusal from a transient one.

* ``seq`` must increase; a report at or below the last recorded ``seq`` is refused
  (an exact idempotent replay returns its stored response instead).
* ``observed_at`` may not be earlier than the lease's ``issued_at``, may not be earlier than
  the last recorded report, and may not be later than the control-plane clock plus the
  observation future skew. There is no lease-expiry bound: a report about a stage that ran
  past its lease is still an operational record.
* A routine report (``heartbeat`` or ``progress``) is recorded at most once per
  ``MIN_ROUTINE_INTERVAL_SECONDS`` of ``observed_at`` per kind; a faster one is acknowledged
  as coalesced without an event. Because ``observed_at`` cannot run ahead of the
  control-plane clock, this bounds routine reports by real time. ``stuck`` and ``failed``
  are never coalesced.
* At most ``MAX_REPORTS_PER_OPERATION`` reports are recorded. Routine reports are accepted
  only while fewer than ``MAX_REPORTS_PER_OPERATION - TERMINAL_RESERVE`` are recorded, so
  the reserve is kept for ``stuck`` and ``failed``: routine reports can never use up the
  budget a later ``failed`` needs. A ``stuck`` or ``failed`` arriving once the total cap is
  reached is refused (409, budget exhausted).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from .execution import (
    MAX_OBSERVATION_FUTURE_SKEW,
    UNRESOLVED_GRANT_STATUSES,
    ExecutionStore,
)
from .stage_watch_limits import (
    MAX_DECISIONS_PER_OPERATION,
    MAX_REPORTS_PER_OPERATION,
    MIN_ROUTINE_INTERVAL_SECONDS,
    REASON_STATUS,
    ROUTINE_KINDS,
    TERMINAL_RESERVE,
)
from .stage_watch_models import WATCH_KINDS, ReportStageWatch
from .store import Conflict, NotFound, StoreError, _parse_utc, _utc_string

EVENT_TYPE = "execution.watch_reported"
OBSERVATION_PROOF = "trusted_worker_assertion"
ROUTINE_INTERVAL = timedelta(seconds=MIN_ROUTINE_INTERVAL_SECONDS)
ROUTINE_BUDGET = MAX_REPORTS_PER_OPERATION - TERMINAL_RESERVE


MISMATCH_DETAIL = "Stage watch report does not match the external operation."
TOO_EARLY_DETAIL = "observed_at is ahead of the control clock; retry"


KEY_CONFLICT_DETAIL = "Idempotency key was already used with a different request."


class WatchRefused(StoreError):
    """A refused watch report with a closed ``reason`` code; the route answers
    ``REASON_STATUS[reason]`` with ``{"detail", "reason"}``.

    Raised inside ``_mutate``, so the transaction rolls back with no event and no idempotency
    record. Only ``too_early`` is transient: the identical body under the same key is accepted
    once the control clock catches up.
    """

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in REASON_STATUS:
            raise ValueError(f"unknown watch refusal reason {reason!r}")
        super().__init__(detail)
        self.reason = reason


def _watch_events(
    connection: sqlite3.Connection, project_id: str, operation_id: str
) -> list[dict[str, Any]]:
    # ``project_id`` first so the lookup walks only this project's events through
    # ``events_by_project_cursor`` instead of scanning the whole events table.
    rows = connection.execute(
        """SELECT payload_json FROM events
        WHERE project_id = ? AND event_type = ?
        AND json_extract(payload_json, '$.operation_id') = ?
        ORDER BY cursor""",
        (project_id, EVENT_TYPE, operation_id),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


class StageWatchStore(ExecutionStore):
    def report_stage_watch(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReportStageWatch, payload)
        counters = {name: value for name, value in payload["counters"].items() if value is not None}
        path = f"/worker/operations/{operation_id}/watch-reports"

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
                self._fence_recovered_operation(connection, operation_id)
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
                raise WatchRefused("stale_epoch", "Stage watch report epoch is no longer current.")
            try:
                grant = self._grant(connection, worker, operation["grant_id"])
            except NotFound as error:
                # The worker's credential no longer covers the grant's project.
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
            observed_at = _parse_utc(payload["observed_at"])
            if observed_at < _parse_utc(lease["issued_at"]):
                raise WatchRefused(
                    "before_lease", "Stage watch report timestamp is outside the accepted window."
                )
            if observed_at > self._now() + MAX_OBSERVATION_FUTURE_SKEW:
                # Transient: the same body is accepted once the control clock catches up.
                raise WatchRefused("too_early", TOO_EARLY_DETAIL)
            recorded = _watch_events(connection, operation["project_id"], operation_id)
            if recorded and (
                payload["seq"] <= recorded[-1]["seq"]
                or observed_at < _parse_utc(recorded[-1]["observed_at"])
            ):
                raise WatchRefused(
                    "out_of_order", "Stage watch reports cannot be reported out of order."
                )
            kind = payload["kind"]
            if kind in ROUTINE_KINDS:
                if len(recorded) >= ROUTINE_BUDGET:
                    raise WatchRefused(
                        "routine_budget_exhausted", "Routine watch report budget exhausted."
                    )
            elif len(recorded) >= MAX_REPORTS_PER_OPERATION:
                raise WatchRefused("budget_exhausted", "Stage watch report budget exhausted.")
            base = {"seq": payload["seq"], "kind": kind, "operation_id": operation_id}
            if kind in ROUTINE_KINDS:
                same = [item for item in recorded if item["kind"] == kind]
                if same and observed_at - _parse_utc(same[-1]["observed_at"]) < ROUTINE_INTERVAL:
                    return 200, base | {"recorded": False, "reason": "coalesced"}
            event = {
                "grant_id": grant["id"],
                "lease_id": lease["id"],
                "operation_id": operation_id,
                "epoch": lease["epoch"],
                "kind": payload["kind"],
                "seq": payload["seq"],
                "observed_at": _utc_string(observed_at),
                "counters": counters,
                "kill_switch_engaged": payload["kill_switch_engaged"],
                "contract_digest": payload["contract_digest"],
                "operational_only": True,
                "observation_proof": OBSERVATION_PROOF,
            }
            self._event(
                connection,
                worker,
                operation["project_id"],
                operation["task_id"],
                EVENT_TYPE,
                lease["epoch"],
                event,
            )
            return 201, base | {"recorded": True}

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")


def watch_receipt_section(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """Return the optional ``stage_watch`` receipt section, or ``{}`` when none was reported.

    Every field here is an unverified worker claim (``trusted_worker_assertion``). It is
    shown for operators only and never decides cost, success or custody: settlement,
    stage outcome and custody are derived elsewhere from recorded control-plane evidence.
    A campaign without watch reports gets no section, so its receipt keeps its old shape
    and digest. ``last_counters_seen`` is the counters of the latest recorded report; its
    ``refused_reports`` and ``dropped_reports`` are the worker's own count of reports control
    refused or that it dropped, so gaps the worker knows about are visible here too.

    Only events of the campaign's project are read (every watch event is written under its
    operation's project, which is the grant's project), through ``events_by_project_cursor``.

    An attempt with recorded watch decision requests (ADR 0111) also gets a ``decisions`` list
    (at most ``MAX_DECISIONS_PER_OPERATION``); every one is ``admission: "not_admitted"``, an
    advisory record of what the agent asked. An attempt with decisions but no reports gets an
    entry with zero counts and no ``first_seen``/``last_seen``. Without decisions every entry
    keeps its earlier shape.
    """
    from .stage_watch_decisions import DECISION_EVENT_TYPE, decision_receipt_entry

    rows = connection.execute(
        """SELECT a.stage_key, a.attempt, a.operation_id, e.event_type, e.payload_json
        FROM campaigns c
        JOIN campaign_stage_attempts a ON a.campaign_id = c.id
        JOIN events e
        ON e.project_id = c.project_id AND e.event_type IN (?, ?)
        AND json_extract(e.payload_json, '$.operation_id') = a.operation_id
        AND json_extract(e.payload_json, '$.grant_id') = a.grant_id
        WHERE c.id = ? AND a.operation_id IS NOT NULL
        ORDER BY a.stage_key, a.attempt, e.cursor""",
        (EVENT_TYPE, DECISION_EVENT_TYPE, campaign_id),
    ).fetchall()
    if not rows:
        return {}
    attempts: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        record = json.loads(row["payload_json"])
        entry = attempts.setdefault(
            (row["stage_key"], row["attempt"]),
            {
                "stage_key": row["stage_key"],
                "attempt": row["attempt"],
                "operation_id": row["operation_id"],
                "counts_by_kind": dict.fromkeys(WATCH_KINDS, 0),
                "first_seen": None,
                "last_seen": None,
                "last_stuck_counters": None,
                "last_failed_counters": None,
                "last_counters_seen": None,
                "kill_switch_engaged_seen": False,
                "reports_recorded": 0,
            },
        )
        if row["event_type"] == DECISION_EVENT_TYPE:
            decisions = entry.setdefault("decisions", [])
            if len(decisions) < MAX_DECISIONS_PER_OPERATION:
                decisions.append(decision_receipt_entry(record))
            continue
        report = record
        if entry["first_seen"] is None:
            entry["first_seen"] = report["observed_at"]
        entry["counts_by_kind"][report["kind"]] += 1
        entry["last_seen"] = report["observed_at"]
        if report["kind"] in ("stuck", "failed"):
            entry[f"last_{report['kind']}_counters"] = report["counters"]
        entry["last_counters_seen"] = report["counters"]
        entry["kill_switch_engaged_seen"] |= bool(report["kill_switch_engaged"])
        entry["reports_recorded"] += 1
    return {
        "stage_watch": {
            "attempts": list(attempts.values()),
            "proof": OBSERVATION_PROOF,
            "claims_unverified": True,
        }
    }
