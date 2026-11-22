"""Release the slot of an expired campaign grant whose run is already over (ADR 0048).

An execution grant keeps holding a project concurrency slot (and its exposure hold) until
it is reconciled. A run that has finished and been confirmed terminal, on a grant that has
since expired, needs no further authority, yet nothing reconciled it unless a person did.
This module closes that gap for grants a campaign issued, with the same accounting as the
human reconcile route (``reconcile_grant_row``), and only on evidence:

* the grant is unresolved and past its expiry;
* its one external operation's current reconciliation is a confirmed exit with no
  contradicting observation (``exited``, ``termination_confirmed``, ``none``);
* the cost is recorded: ``0`` for a zero-exposure grant, or the settled cents of the
  campaign attempt that owns the grant; anything else is left alone;
* the owning campaign attempt is finished (not in flight and not ``unknown``: the
  campaign's own flow, or a person, settles those), so the campaign never sees its grant
  reconciled from under it.

A grant no campaign issued is never released here: single runs outside campaigns are
reconciled by a person. It never infers termination: an unconfirmed, running, unknown or
contradicted operation, a grant with no operation, and a nonzero cost with no recorded
figure all stay unresolved.

The release is credited to the campaign's acting principal (its approver, else its
submitter), exactly as the runner's own events and ADR 0046's reconciliation are, and the
event says it was automatic; it is never credited to whoever created the grant.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .execution import reconcile_grant_row
from .external_operations import current_operation_reconciliation
from .store import Store, _parse_utc, _utc_string

# Campaign attempts whose grant a campaign (or a person) still owns.
_OWNED_ATTEMPT_STATUSES = (
    "reserved",
    "granted",
    "admitted",
    "running",
    "exited",
    "collected",
    "resolved",
    "measured",
    "reconciled",
    "unknown",
)
MAX_RELEASES_PER_PASS = 50
# Expired grants examined per pass. A cursor carries on from the last one examined, so
# grants that can never qualify (they wait for a person) cannot starve the ones behind them.
SCAN_WINDOW = 500

Cursor = tuple[str, str]


def release_expired_terminal_grants(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    after: Cursor | None = None,
    limit: int = MAX_RELEASES_PER_PASS,
) -> tuple[list[dict[str, Any]], Cursor | None]:
    """Reconcile every eligible grant in one window, in the caller's transaction.

    Returns the releases and the cursor for the next pass (``None`` after the last
    window, so the scan wraps around)."""
    stamp = _utc_string(now)
    start = after or ("", "")
    window = connection.execute(
        """SELECT * FROM execution_grants WHERE status IN ('active','cancel_requested')
        AND expires_at <= ? AND (expires_at, id) > (?, ?)
        ORDER BY expires_at, id LIMIT ?""",
        (stamp, start[0], start[1], SCAN_WINDOW),
    ).fetchall()
    released: list[dict[str, Any]] = []
    previous = after
    for grant in window:
        if len(released) >= limit:
            return released, previous
        item = _release_one(connection, grant, now, stamp)
        previous = (grant["expires_at"], grant["id"])
        if item is not None:
            released.append(item)
    if len(window) < SCAN_WINDOW:
        return released, None
    last = window[-1]
    return released, (last["expires_at"], last["id"])


def _release_one(
    connection: sqlite3.Connection, grant: sqlite3.Row, now: datetime, stamp: str
) -> dict[str, Any] | None:
    try:
        if _parse_utc(grant["expires_at"]) > now:
            return None
    except (TypeError, ValueError):
        return None
    owner = connection.execute(
        """SELECT a.status, a.settled_cents, a.campaign_id, c.org_id
        FROM campaign_stage_attempts a JOIN campaigns c ON c.id=a.campaign_id
        WHERE a.grant_id=?""",
        (grant["id"],),
    ).fetchone()
    cents = _recorded_cost(grant, owner)
    if cents is None:
        return None
    operations = connection.execute(
        "SELECT operation_id FROM external_operations WHERE grant_id=?", (grant["id"],)
    ).fetchall()
    if len(operations) != 1:
        return None
    reconciliation = current_operation_reconciliation(connection, operations[0]["operation_id"])
    if (
        reconciliation is None
        or reconciliation["observed_state"] != "exited"
        or not reconciliation["termination_confirmed"]
        or reconciliation["contradiction_state"] != "none"
    ):
        return None
    actor = campaign_actor(connection, owner["campaign_id"])
    if actor is None:
        return None
    reconciled = reconcile_grant_row(
        connection, grant, settlement_cents=cents, principal_id=actor, now=stamp
    )
    if reconciled is None:
        return None
    Store._event(
        connection,
        {"id": actor, "org_id": owner["org_id"]},
        grant["project_id"],
        grant["task_id"],
        "execution.expired_terminal_grant_reconciled",
        reconciled,
        {
            "grant_id": grant["id"],
            "campaign_id": owner["campaign_id"],
            "operation_id": operations[0]["operation_id"],
            "settlement_cents": cents,
            "basis": "expired_with_confirmed_exit",
            "observation_cursor": reconciliation["cursor"],
            "automatic": True,
            "credited_to": "campaign_runner",
            "provider_termination_inferred": False,
            "operational_only": True,
        },
    )
    return {"grant_id": grant["id"], "settlement_cents": cents}


def campaign_actor(connection: sqlite3.Connection, campaign_id: str) -> str | None:
    """The principal a campaign's runner acts as: its approver, else its submitter."""
    approval = connection.execute(
        """SELECT approving_principal_id FROM campaign_approvals WHERE campaign_id=?
        ORDER BY (status='active') DESC, approved_at DESC, id DESC LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    if approval is not None:
        return approval["approving_principal_id"]
    campaign = connection.execute(
        "SELECT submitted_by_principal_id FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    return None if campaign is None else campaign["submitted_by_principal_id"]


def _recorded_cost(grant: sqlite3.Row, owner: sqlite3.Row | None) -> int | None:
    """The cost the records already show, or ``None`` when it would have to be guessed."""
    if owner is None or owner["status"] in _OWNED_ATTEMPT_STATUSES:
        return None
    if grant["max_exposure_cents"] == 0:
        return 0
    if owner["settled_cents"] is not None:
        cents = int(owner["settled_cents"])
        return cents if cents <= grant["max_exposure_cents"] else None
    return None
