"""Automatic reconciliation of campaign attempts that provably never started (ADR 0046).

A launch that the worker itself reports as refused (for example a container network that
cannot be created) leaves a campaign attempt ``unknown`` although nothing ever ran: no
container, no provider resource, no money held or spent. ``assess`` decides, from
control-plane records only, whether an unknown attempt is such an orphan. It is a pure read:
the runner applies the result in one transaction (``CampaignRunner._auto_reconcile``).

A host reboot is deliberately not a trigger. Control cannot see whether a container was
created and started before it lost contact (the worker may be on another host, or may have
started the container before reporting it), so an unreported attempt is an unknown effect
and keeps the human path.

The rule is deliberately narrow and fail-closed. Every guard below must hold, and the
trigger is required on top of them:

* guards: the attempt is unknown and unsettled with a zero hold; its grant is unresolved,
  expired, with only expired leases; the one operation (if any) has no provider identity;
  no worker observation ever showed a container or a non-unknown state; and there is no
  run receipt, provider receipt, output manifest, recovery record, dispatch, workspace
  handoff or Salad record for the grant or attempt;
* trigger: the worker reported a launch error (a definite refusal before any container
  existed). A launch error is a trusted worker assertion; the worker never starts an
  operation after reporting one (its journal holds the operation ``uncertain``).

Anything else keeps the existing human path. ``not_started`` is never evidence: it carries
no exit code, receipt, manifest or measurement, so no gate can read it as a pass or fail.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .store import canonical_digest

NOT_STARTED = "not_started"
# Identical not-started attempts of one stage that may be re-queued automatically; the next
# identical one stays ``unknown`` for a person, with the repeat recorded as the reason.
MAX_AUTO_REQUEUES = 2
REASON_LAUNCH_PREFIX = "launch_error:"


@dataclass(frozen=True)
class NotStarted:
    """An unknown attempt shown to have never started, and the evidence for it."""

    reason: str
    evidence: dict[str, Any]

    @property
    def digest(self) -> str:
        return canonical_digest(self.evidence)

    @property
    def detail(self) -> str:
        return f"auto-reconciled as not started ({self.reason}); no evidence recorded"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _none(connection: sqlite3.Connection, sql: str, *params: Any) -> bool:
    return connection.execute(sql, params).fetchone() is None


def assess(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row | dict[str, Any],
    *,
    now: datetime,
) -> NotStarted | None:
    """``NotStarted`` when every guard holds and a trigger applies, else ``None``."""
    if (
        attempt["status"] != "unknown"
        or attempt["grant_id"] is None
        or attempt["settled_cents"] is not None
        or attempt["hold_cents"] != 0
        or attempt["exit_code"] is not None
        or attempt["run_receipt_digest"] is not None
        or attempt["manifest_id"] is not None
        or attempt["salad_evidence_digest"] is not None
    ):
        return None
    attempt_id, grant_id = attempt["id"], attempt["grant_id"]
    grant = connection.execute("SELECT * FROM execution_grants WHERE id=?", (grant_id,)).fetchone()
    if (
        grant is None
        or grant["status"] not in ("active", "cancel_requested")
        or grant["max_exposure_cents"] != 0
        or now < _parse(grant["expires_at"])
    ):
        return None
    leases = connection.execute(
        "SELECT issued_at, hard_expires_at FROM execution_leases WHERE grant_id=?", (grant_id,)
    ).fetchall()
    if any(now < _parse(lease["hard_expires_at"]) for lease in leases):
        return None
    operations = connection.execute(
        "SELECT operation_id, provider_id FROM external_operations WHERE grant_id=?", (grant_id,)
    ).fetchall()
    if len(operations) > 1 or any(op["provider_id"] is not None for op in operations):
        return None
    operation_ids = [op["operation_id"] for op in operations]
    if not _none(
        connection,
        """SELECT 1 FROM execution_observations WHERE grant_id=?
        AND (provider_id IS NOT NULL OR observed_state!='unknown' OR termination_confirmed=1)""",
        grant_id,
    ):
        return None
    for table in (
        "artifact_manifests",
        "kubernetes_dispatches",
        "workspace_handoffs",
    ):
        if not _none(connection, f"SELECT 1 FROM {table} WHERE grant_id=?", grant_id):
            return None
    for table in (
        "external_operation_receipts",
        "execution_run_receipts",
        "execution_recovery_reconciliations",
    ):
        for operation_id in operation_ids:
            if not _none(connection, f"SELECT 1 FROM {table} WHERE operation_id=?", operation_id):
                return None
    if not _none(
        connection, "SELECT 1 FROM campaign_salad_attempts WHERE attempt_id=?", attempt_id
    ):
        return None

    base = {
        "attempt_id": attempt_id,
        "grant_id": grant_id,
        "operation_ids": operation_ids,
        "hold_cents": 0,
    }
    launch = connection.execute(
        """SELECT json_extract(payload_json,'$.launch_error') AS error, cursor FROM events
        WHERE project_id=? AND event_type='execution.observation_reported'
        AND json_extract(payload_json,'$.grant_id')=?
        AND json_extract(payload_json,'$.launch_error') IS NOT NULL
        ORDER BY cursor DESC LIMIT 1""",
        (grant["project_id"], grant_id),
    ).fetchone()
    if launch is not None:
        return NotStarted(
            REASON_LAUNCH_PREFIX + launch["error"],
            {**base, "trigger": "launch_error", "launch_error": launch["error"]},
        )
    return None


def repeat_count(attempts: list[dict[str, Any]], reason: str) -> int:
    """Consecutive trailing not-started attempts of a stage with the same reason."""
    count = 0
    for attempt in reversed(attempts):
        if attempt["failure_class"] != NOT_STARTED or f"({reason})" not in (
            attempt["failure_detail"] or ""
        ):
            break
        count += 1
    return count


def not_started_count(attempts: list[dict[str, Any]]) -> int:
    """Attempts that never started: they use an attempt number but no scientific retry."""
    return sum(1 for attempt in attempts if attempt["failure_class"] == NOT_STARTED)
