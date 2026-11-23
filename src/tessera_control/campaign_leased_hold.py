"""A stuck campaign's leased grants: freed on evidence, otherwise held and said so (ADR 0153).

A grant a worker leased (or that has an external operation) may have run. While its campaign
is ``blocked`` or ``paused_delegation_lapsed`` the runner drives such an attempt only through
the evidence paths a running campaign already uses, read from control-plane records alone:

* once the grant has expired, the attempt is classified exactly as the running flow's
  observation would classify it (``admitted``/``running`` to ``unknown`` on an expired grant
  with no confirmed exit, a missing receipt or a missing manifest); nothing is started,
  cancelled or re-run, and no lease is issued;
* a confirmed exit is resolved by the ADR 0053 route and an attempt that provably never
  started by the ADR 0046 route, each with its own accounting; the freed project slot is
  recorded with ``campaign.slot_freed`` (reason ``confirmed_exit`` or ``not_started_evidence``).

A live (unexpired) grant is never touched: the run may still be going. An expired one that
no evidence settles keeps its project slot and hold, and gets one ``campaign.slot_held``
event per attempt per spell out of ``running`` naming what was seen and what a person must
do. This module is the read side; the runner writes in one transaction under its lease.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .campaign_slot_reclaim import EventWriter, unrecorded_sql
from .external_operations import current_operation_reconciliation
from .store import _parse_utc

SLOT_HELD_EVENT = "campaign.slot_held"
CONFIRMED_EXIT = "confirmed_exit"
LEASED_GRANT_UNCONFIRMED = "leased_grant_unconfirmed"
EXITED_AWAITING_COLLECTION = "exited_awaiting_collection"
# Attempts the running flow's grant observation drives (worker-grant providers).
OBSERVED = ("admitted", "running")
# Attempts whose unresolved grant counts against the project limit until a route settles it.
HELD = ("granted", "admitted", "running", "unknown")

_ACTIONS = {
    LEASED_GRANT_UNCONFIRMED: (
        "confirm whether the run stopped, then reconcile the grant through an evidence route "
        "(POST /execution-grants/{grant_id}/reconcile) or cancel the campaign"
    ),
    EXITED_AWAITING_COLLECTION: (
        "the run exited with a run receipt; resume the campaign so it collects the outputs, "
        "or cancel it"
    ),
}


def expired(grant: Mapping[str, Any], now: datetime) -> bool:
    """True only for a parseable ``expires_at`` at or before ``now`` (fail closed: live)."""
    try:
        return _parse_utc(grant["expires_at"]) <= now
    except (TypeError, ValueError):
        return False


def expired_leased(
    connection: sqlite3.Connection,
    campaign_id: str,
    now: datetime,
    statuses: tuple[str, ...],
    *,
    unheld: bool = False,
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """(attempt, grant) pairs of a campaign whose grant was leased or has an operation, is
    unresolved and has expired; with ``unheld``, only those with no ``slot_held`` event in
    the current spell."""
    marks = ",".join("?" for _ in statuses)
    spell = f"AND {unrecorded_sql(SLOT_HELD_EVENT)}" if unheld else ""
    rows = connection.execute(
        f"""SELECT a.id AS attempt_id, g.id AS grant_id
        FROM campaign_stage_attempts a JOIN campaigns c ON c.id=a.campaign_id
        JOIN execution_grants g ON g.id=a.grant_id
        WHERE a.campaign_id=? AND a.status IN ({marks}) AND a.settled_cents IS NULL
        AND g.status IN ('active','cancel_requested')
        AND (EXISTS (SELECT 1 FROM execution_leases l WHERE l.grant_id=g.id)
             OR EXISTS (SELECT 1 FROM external_operations o WHERE o.grant_id=g.id))
        {spell} ORDER BY a.stage_key, a.attempt""",
        (campaign_id, *statuses),
    ).fetchall()
    pairs = []
    for row in rows:
        grant = connection.execute(
            "SELECT * FROM execution_grants WHERE id=?", (row["grant_id"],)
        ).fetchone()
        if expired(grant, now):
            attempt = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (row["attempt_id"],)
            ).fetchone()
            pairs.append((attempt, grant))
    return pairs


def held_evidence(
    connection: sqlite3.Connection, grant: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    """What control holds about a leased grant: ids, states and times only."""
    leases = connection.execute(
        "SELECT hard_expires_at FROM execution_leases WHERE grant_id=?", (grant["id"],)
    ).fetchall()
    live = 0
    for lease in leases:
        try:
            live += _parse_utc(lease["hard_expires_at"]) > now
        except (TypeError, ValueError):
            live += 1  # unreadable: counted as live
    operations = connection.execute(
        "SELECT operation_id FROM external_operations WHERE grant_id=? ORDER BY operation_id",
        (grant["id"],),
    ).fetchall()
    seen = receipt = None
    if len(operations) == 1:
        operation_id = operations[0]["operation_id"]
        seen = current_operation_reconciliation(connection, operation_id)
        receipt = connection.execute(
            "SELECT 1 FROM execution_run_receipts WHERE operation_id=?", (operation_id,)
        ).fetchone()
    return {
        "grant_expires_at": grant["expires_at"],
        "lease_count": len(leases),
        "live_lease_count": live,
        "operation_ids": [row["operation_id"] for row in operations],
        "observed_state": None if seen is None else seen["observed_state"],
        "termination_confirmed": bool(seen and seen["termination_confirmed"]),
        "contradiction_state": None if seen is None else seen["contradiction_state"],
        "run_receipt": receipt is not None,
    }


def held_reason(evidence: Mapping[str, Any]) -> str:
    if (
        evidence["observed_state"] == "exited"
        and evidence["termination_confirmed"]
        and evidence["contradiction_state"] == "none"
        and evidence["run_receipt"]
    ):
        return EXITED_AWAITING_COLLECTION
    return LEASED_GRANT_UNCONFIRMED


def record_slot_held(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    attempt: Mapping[str, Any],
    grant: Mapping[str, Any],
    evidence: dict[str, Any],
    *,
    record_event: EventWriter,
) -> dict[str, Any]:
    """Write one ``campaign.slot_held`` event in the caller's transaction."""
    reason = held_reason(evidence)
    item = {
        "stage_key": attempt["stage_key"],
        "attempt": attempt["attempt"],
        "attempt_id": attempt["id"],
        "attempt_status": attempt["status"],
        "provider_id": attempt["provider_id"],
        "grant_id": grant["id"],
        "reason": reason,
        "campaign_status": campaign["status"],
        "trigger": "stuck_campaign_reconcile",
        "evidence": evidence,
        "action": _ACTIONS[reason].replace("{grant_id}", grant["id"]),
        "automatic": True,
    }
    record_event(connection, dict(campaign), SLOT_HELD_EVENT, item)
    return {"campaign_id": campaign["id"], **item}
