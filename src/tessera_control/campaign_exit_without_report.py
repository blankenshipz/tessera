"""Closing an attempt whose container exited but whose executor never reported (ADR 0158).

ADR 0053 resolves an attempt on the executor's confirmed exit observation. When the executor
path is dead (its exit observation and run receipt never reach control) nothing on the control
side ever ended a ``running`` attempt: its grant held a worker slot until it expired. The stage
watcher (ADR 0087) still reports ``failed``, and since ADR 0158 a ``failed`` report about a
container the probe read as definitively exited carries the closed ``exit`` object
``{exit_code, oom_killed}``. ``assess`` decides, from control-plane records only, whether that
report may close the attempt. It is a pure read: the runner applies the result in one
transaction (``CampaignRunner._close_unreported_exit``).

What is trusted, and how far. The watch report is a worker assertion
(``trusted_worker_assertion``), the same trust domain as the executor's own observation, but it
alone never closes anything. It closes an attempt only when control's own records agree that
nothing is still running, and only after a bounded window in which the executor could still
report. Every guard must hold:

* the stage's provider is a non-billable worker-grant provider (the caller reads the
  descriptor), the attempt is ``admitted`` or ``running`` and unsettled, its grant is unresolved
  and has exactly one external operation, and that operation is the attempt's;
* the operation's latest watch event is a ``failed`` report carrying an ``exit`` with an integer
  code (never an unknown, absent or deadline ``failed``, never a report without ``exit``), for
  the operation's own grant, lease and epoch;
* no confirmed exit is on record (that is ADR 0053's case), no run receipt or output manifest
  exists (the normal flow is alive), and no observation from any source and no lease of the
  grant is later than the watch exit (a later ``running`` contradicts it);
* every lease of the grant has expired (the executor stopped renewing) and no output upload is
  still arriving;
* at least ``grace_seconds`` (default ``EXIT_REPORT_GRACE_SECONDS``, at most
  ``MAX_EXIT_REPORT_GRACE_SECONDS``) of control's clock have passed since control recorded the
  watch event.

The outcome is never a pass, never a measurement and never a permanent failure: every close is
``failed_transient`` with failure class ``exited_without_report``, settled at zero, and the stage's
retry policy (or the ADR 0058 infrastructure bound) decides when it ends. The watcher's exit code
only classifies the close for the receipt (``killed``, ``oom``, ``error``); it is kept in the
evidence and the detail and is never written as the attempt's own ``exit_code``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .artifact_limits import OUTPUT_REGISTRATION_GRACE_SECONDS
from .campaign_exited_resolve import (
    UNRESOLVED_GRANT,
    UploadProgress,
    hold_deadline,
    uploads_arriving,
)
from .external_operations import current_operation_reconciliation
from .stage_watch import EVENT_TYPE as WATCH_EVENT_TYPE
from .store import canonical_digest

EXITED_WITHOUT_REPORT = "exited_without_report"
EXIT_REPORT_GRACE_SECONDS = 300
MIN_EXIT_REPORT_GRACE_SECONDS = OUTPUT_REGISTRATION_GRACE_SECONDS
MAX_EXIT_REPORT_GRACE_SECONDS = 900
# SIGKILL (128+9) and SIGTERM (128+15): the container was ended from outside.
KILLED_EXIT_CODES = frozenset({137, 143})
EXIT_CLASSES = ("killed", "oom", "error")
CLOSABLE = ("admitted", "running")


@dataclass(frozen=True)
class Closure:
    """An attempt closed on a watcher-reported exit, and the evidence for it."""

    status: str
    exit_class: str
    exit_code: int
    detail: str
    cents: int
    basis: str
    evidence: dict[str, Any]

    failure_class: str = EXITED_WITHOUT_REPORT

    @property
    def digest(self) -> str:
        return canonical_digest(self.evidence)


def grace_seconds(value: object) -> int:
    """Validate a configured grace: an integer within the closed bounds, else ``ValueError``."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_EXIT_REPORT_GRACE_SECONDS <= value <= MAX_EXIT_REPORT_GRACE_SECONDS
    ):
        raise ValueError(
            "exit_report_grace_seconds must be an integer in "
            f"{MIN_EXIT_REPORT_GRACE_SECONDS}..{MAX_EXIT_REPORT_GRACE_SECONDS}"
        )
    return value


def exit_class(exit_code: int, oom_killed: bool) -> str | None:
    """``oom``, ``killed`` (137/143, not OOM) or ``error`` (another non-zero code); ``None`` for
    a clean zero exit, which the watcher never reports as ``failed`` and which closes nothing."""
    if oom_killed:
        return "oom"
    if exit_code in KILLED_EXIT_CODES:
        return "killed"
    if exit_code != 0:
        return "error"
    return None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _watch_exit(
    connection: sqlite3.Connection, project_id: str, operation_id: str
) -> sqlite3.Row | None:
    """The operation's LATEST watch event, if it is a ``failed`` report carrying an ``exit``."""
    row = connection.execute(
        """SELECT cursor, created_at, payload_json FROM events
        WHERE project_id=? AND event_type=? AND json_extract(payload_json,'$.operation_id')=?
        ORDER BY cursor DESC LIMIT 1""",
        (project_id, WATCH_EVENT_TYPE, operation_id),
    ).fetchone()
    return row


def _valid_exit(record: dict[str, Any]) -> tuple[int, bool] | None:
    exit_ = record.get("exit")
    if record.get("kind") != "failed" or not isinstance(exit_, dict):
        return None
    code, oom = exit_.get("exit_code"), exit_.get("oom_killed")
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255:
        return None
    if not isinstance(oom, bool):
        return None
    return code, oom


def _later_evidence(
    connection: sqlite3.Connection,
    grant_id: str,
    operation_id: str,
    exit_observed: datetime,
    exit_recorded: datetime,
) -> bool:
    """True when any observation (worker, provider receipt or recovery) or any lease of the grant
    is not earlier than the watch exit: something may have run after it."""
    for row in connection.execute(
        "SELECT observed_at, recorded_at FROM execution_observations WHERE operation_id=?",
        (operation_id,),
    ):
        if _parse(row["observed_at"]) >= exit_observed or _parse(row["recorded_at"]) >= (
            exit_recorded
        ):
            return True
    for row in connection.execute(
        "SELECT observed_at FROM external_operation_receipts WHERE operation_id=?",
        (operation_id,),
    ):
        if _parse(row["observed_at"]) >= exit_observed:
            return True
    for row in connection.execute(
        """SELECT o.observed_at FROM execution_recovery_observations o
        JOIN execution_recovery_reconciliations r ON r.id=o.recovery_id
        WHERE r.operation_id=?""",
        (operation_id,),
    ):
        if _parse(row["observed_at"]) >= exit_observed:
            return True
    for row in connection.execute(
        "SELECT issued_at FROM execution_leases WHERE grant_id=?", (grant_id,)
    ):
        if _parse(row["issued_at"]) >= exit_observed:
            return True
    return False


def _confirmed_exit_anywhere(connection: sqlite3.Connection, operation_id: str) -> bool:
    """True when any source (worker, provider receipt, recovery) recorded a confirmed exit."""
    return (
        connection.execute(
            """SELECT 1 FROM execution_observations WHERE operation_id=?
            AND observed_state='exited' AND termination_confirmed=1
            UNION ALL SELECT 1 FROM external_operation_receipts WHERE operation_id=?
            AND observed_state='exited' AND termination_confirmed=1
            UNION ALL SELECT 1 FROM execution_recovery_observations o
            JOIN execution_recovery_reconciliations r ON r.id=o.recovery_id
            WHERE r.operation_id=? AND o.observed_state='exited' AND o.termination_confirmed=1
            LIMIT 1""",
            (operation_id, operation_id, operation_id),
        ).fetchone()
        is not None
    )


def _live_lease(connection: sqlite3.Connection, grant_id: str, now: datetime) -> bool:
    return any(
        now < _parse(row["expires_at"])
        for row in connection.execute(
            "SELECT expires_at FROM execution_leases WHERE grant_id=?", (grant_id,)
        )
    )


def assess(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row | dict[str, Any],
    *,
    now: datetime,
    free_worker_grant: bool = False,
    transient_codes: frozenset[int] | set[int] | tuple[int, ...] = (),
    grace: int = EXIT_REPORT_GRACE_SECONDS,
    progress: UploadProgress | None = None,
    approval_expires: datetime | None = None,
) -> Closure | None:
    """A ``Closure`` when every guard holds, else ``None`` (and the attempt is untouched)."""
    grace = grace_seconds(grace)
    if (
        not free_worker_grant
        or attempt["status"] not in CLOSABLE
        or attempt["grant_id"] is None
        or attempt["operation_id"] is None
        or attempt["settled_cents"] is not None
    ):
        return None
    attempt_id, grant_id = attempt["id"], attempt["grant_id"]
    grant = connection.execute("SELECT * FROM execution_grants WHERE id=?", (grant_id,)).fetchone()
    if grant is None or grant["status"] not in UNRESOLVED_GRANT:
        return None
    operations = connection.execute(
        "SELECT * FROM external_operations WHERE grant_id=?", (grant_id,)
    ).fetchall()
    if len(operations) != 1 or operations[0]["operation_id"] != attempt["operation_id"]:
        return None
    operation = operations[0]
    operation_id = operation["operation_id"]
    watched = _watch_exit(connection, operation["project_id"], operation_id)
    if watched is None:
        return None
    record = json.loads(watched["payload_json"])
    reported = _valid_exit(record)
    if reported is None:
        return None  # unknown, absent, deadline, exited_unknown or a report without ``exit``
    code, oom = reported
    lease = connection.execute(
        "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
    ).fetchone()
    if (
        lease is None
        or record.get("grant_id") != grant_id
        or record.get("lease_id") != operation["lease_id"]
        or record.get("epoch") != operation["epoch"]
        or lease["epoch"] != operation["epoch"]
        or lease["grant_id"] != grant_id
    ):
        return None
    klass = exit_class(code, oom)
    if klass is None:
        return None
    seen = current_operation_reconciliation(connection, operation_id)
    if seen is not None and seen["observed_state"] == "exited" and seen["termination_confirmed"]:
        return None  # a confirmed exit is on record: ADR 0053 decides, never this path
    if _confirmed_exit_anywhere(connection, operation_id):
        return None  # even a contradicted one: the executor path spoke, a person or 0053 decides
    if (
        connection.execute(
            "SELECT 1 FROM execution_run_receipts WHERE operation_id=?", (operation_id,)
        ).fetchone()
        is not None
        or connection.execute(
            "SELECT 1 FROM artifact_manifests WHERE grant_id=?", (grant_id,)
        ).fetchone()
        is not None
    ):
        return None  # the executor is reporting: the normal flow collects it
    exit_observed = _parse(record["observed_at"])
    exit_recorded = _parse(watched["created_at"])
    if (now - exit_recorded).total_seconds() < grace:
        return None  # the executor may still report within the window
    if _later_evidence(connection, grant_id, operation_id, exit_observed, exit_recorded):
        return None  # something ran or renewed after the reported exit
    if _live_lease(connection, grant_id, now):
        return None  # a live lease: the executor may still be at it
    deadline = hold_deadline(grant, exit_recorded, approval_expires)
    if uploads_arriving(connection, grant_id, now, progress, deadline, exit_recorded):
        return None  # outputs are still reaching control
    # Recorded for the receipt only: a worker assertion never makes a failure permanent.
    declared = code in transient_codes
    status = "failed_transient"
    detail = (
        f"{EXITED_WITHOUT_REPORT}:{klass}; exit_code:{code} reported by the stage watcher; "
        f"no run report reached control within {grace} s"
    )
    evidence = {
        "reason": EXITED_WITHOUT_REPORT,
        "exit_class": klass,
        "exit_code": code,
        "oom_killed": oom,
        "declared_transient": declared,
        "attempt_id": attempt_id,
        "grant_id": grant_id,
        "operation_id": operation_id,
        "lease_id": operation["lease_id"],
        "epoch": operation["epoch"],
        "watch_event_cursor": watched["cursor"],
        "watch_seq": record.get("seq"),
        "watch_observed_at": record["observed_at"],
        "watch_recorded_at": watched["created_at"],
        "observation_proof": record.get("observation_proof"),
        "grace_seconds": grace,
        "hold_cents": int(grant["max_exposure_cents"]),
        "cents": 0,
        "was_status": attempt["status"],
    }
    return Closure(status, klass, code, detail[:500], 0, "local_zero", evidence)
