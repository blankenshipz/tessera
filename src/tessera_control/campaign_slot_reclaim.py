"""A campaign that is not running stops holding capacity it provably is not using (ADR 0153).

Three counters can keep a stuck (blocked or paused) campaign's capacity taken:

* the project concurrency limit counts every unresolved grant, expired or not. An expired
  grant that no worker ever leased and that has no operation can never run (the store
  refuses a lease on an expired grant), so the runner reconciles it at zero for a stuck
  campaign with the same evidence and accounting as the expired-unattempted reconcile route
  (``unclaimed_expired_grant``). An ``unknown`` attempt with the existing not-started
  evidence (ADR 0046) is auto-reconciled for a paused campaign as for a blocked one. A grant
  that was leased and has no confirmed exit stays counted: a person reconciles it;
* the placement queue (``campaign_placement.placed_not_granted``, multi-candidate stages
  only) counts attempts placed but not yet granted. An attempt of a campaign in an
  ``IDLE_CAMPAIGN`` status that never wrote an ``issue_grant`` intent stops counting
  (``FREED_SQL``), a computed rule with no state change; it counts again on resume, and its
  grant then passes the provider's own fresh slot check;
* the provider's own count of live grants needs nothing: an expired grant already frees it
  (ADR 0048), and a live one keeps it.

Each freed counter is recorded with a ``campaign.slot_freed`` event (reason
``expired_unclaimed_grant``, ``not_started_evidence`` or ``placed_not_granted``); the queue
event is written once per spell out of ``running``, at the transition or by the runner's
reconcile sweep for a campaign that went idle earlier.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .campaign_release_scope import IDLE_CAMPAIGN, SWEEP_LIMIT
from .store import _parse_utc

SLOT_FREED_EVENT = "campaign.slot_freed"
PLACED_NOT_GRANTED = "placed_not_granted"
EXPIRED_UNCLAIMED_GRANT = "expired_unclaimed_grant"
NOT_STARTED_EVIDENCE = "not_started_evidence"
PAUSED_UNLEASED_GRANT = "paused_unleased_grant"
BLOCKED_UNLEASED_GRANT = "blocked_unleased_grant"
SLOT_REACQUIRED_EVENT = "campaign.slot_reacquired"
# Marks an attempt closed by the paused-campaign release; the re-acquire event looks for it.
PAUSED_RELEASE_DETAIL = "grant released while the campaign was paused (paused_slot_release)"
BLOCKED_RELEASE_DETAIL = "grant released while the campaign was blocked (blocked_slot_release)"
# Every detail an unleased-grant release writes (paused or blocked).
UNLEASED_RELEASE_DETAILS = (PAUSED_RELEASE_DETAIL, BLOCKED_RELEASE_DETAIL)
# Provider kinds whose never-leased grants the release may give back: a worker grant and a
# Salad route grant before its executor's lease (ADR 0167, ADR 0208 addendum). Any other kind
# keeps its grant (fail closed).
UNLEASED_RELEASE_KINDS = ("worker_grant", "salad_route")
# Non-terminal statuses in which the runner issues no grant and drives no attempt forward.
STUCK_CAMPAIGN = ("blocked", "paused_delegation_lapsed")
# Events after which a campaign may be running again; a later freeing is a new spell.
_RESUME_EVENTS = ("campaign.unblocked", "campaign.approved", "campaign.cancel_requested")

EventWriter = Callable[[sqlite3.Connection, Mapping[str, Any], str, dict[str, Any]], None]


def _in(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


# True for an attempt (alias ``a``) of a campaign (alias ``c``) that holds no provider slot:
# placed without a grant, the campaign idle, and no grant request ever written for it.
FREED_SQL = f"""(c.status IN ({_in(IDLE_CAMPAIGN)})
    AND a.status='reserved' AND a.grant_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM campaign_step_intents i WHERE i.campaign_id=a.campaign_id
        AND i.stage_key=a.stage_key AND i.attempt=a.attempt AND i.step='issue_grant'))"""


def unrecorded_sql(event_type: str) -> str:
    """True while the attempt (alias ``a``) of campaign ``c`` has no ``event_type`` event since
    the campaign last may have gone back to running (the current spell)."""
    return f"""NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id=c.project_id
    AND e.event_type='{event_type}'
    AND json_extract(e.payload_json, '$.attempt_id')=a.id
    AND e.cursor > COALESCE((SELECT max(r.cursor) FROM events r WHERE r.project_id=c.project_id
        AND r.event_type IN ({_in(_RESUME_EVENTS)})
        AND json_extract(r.payload_json, '$.campaign_id')=c.id
        AND COALESCE(json_extract(r.payload_json, '$.resulting_status'), '')
            NOT IN ({_in(IDLE_CAMPAIGN)})), 0))"""


# True while the attempt has no ``slot_freed`` event in the current spell.
UNRECORDED_SQL = unrecorded_sql(SLOT_FREED_EVENT)


def record_freed_slots(
    connection: sqlite3.Connection,
    campaign_id: str,
    *,
    trigger: str,
    record_event: EventWriter,
) -> list[dict[str, Any]]:
    """Write, in the caller's transaction, one ``campaign.slot_freed`` per newly freed attempt.

    Idempotent: an attempt already recorded since the campaign last left ``running`` is
    skipped. Nothing else is written; the slot is free by ``FREED_SQL`` itself."""
    campaign = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None or campaign["status"] not in IDLE_CAMPAIGN:
        return []
    recorded: list[dict[str, Any]] = []
    attempts = connection.execute(
        f"""SELECT a.* FROM campaign_stage_attempts a JOIN campaigns c ON c.id=a.campaign_id
        WHERE a.campaign_id=? AND a.provider_id IS NOT NULL AND {FREED_SQL}
        AND {UNRECORDED_SQL} ORDER BY a.stage_key, a.attempt""",
        (campaign_id,),
    ).fetchall()
    for attempt in attempts:
        recorded.append(
            record_slot_freed(
                connection,
                campaign,
                attempt,
                reason=PLACED_NOT_GRANTED,
                trigger=trigger,
                record_event=record_event,
            )
        )
    return recorded


def record_slot_freed(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    attempt: Mapping[str, Any],
    *,
    reason: str,
    trigger: str,
    record_event: EventWriter,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """Write one ``campaign.slot_freed`` event in the caller's transaction."""
    item = {
        "stage_key": attempt["stage_key"],
        "attempt": attempt["attempt"],
        "attempt_id": attempt["id"],
        "provider_id": attempt["provider_id"],
        "reason": reason,
        "campaign_status": campaign["status"],
        "trigger": trigger,
        "released_by": "system",
        "automatic": True,
        **({"grant_id": grant_id} if grant_id else {}),
    }
    record_event(connection, dict(campaign), SLOT_FREED_EVENT, item)
    return {"campaign_id": campaign["id"], **item}


def unclaimed_expired_grant(
    connection: sqlite3.Connection, attempt_id: str, now: datetime
) -> sqlite3.Row | None:
    """The attempt's grant when it provably never ran, else None.

    Evidence (the same as the expired-unattempted reconcile route): the attempt is granted
    or admitted and unsettled; its grant is unresolved and expired; no worker ever took a
    lease on it and no operation was ever recorded for it. A lease is the only way a worker
    gets authority to run, and the store refuses a lease on an expired grant, so such a
    grant can never run."""
    attempt = connection.execute(
        """SELECT * FROM campaign_stage_attempts WHERE id=? AND grant_id IS NOT NULL
        AND status IN ('granted','admitted') AND settled_cents IS NULL""",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        return None
    grant = connection.execute(
        """SELECT * FROM execution_grants g WHERE g.id=?
        AND g.status IN ('active','cancel_requested')
        AND NOT EXISTS (SELECT 1 FROM execution_leases l WHERE l.grant_id=g.id)
        AND NOT EXISTS (SELECT 1 FROM external_operations o WHERE o.grant_id=g.id)""",
        (attempt["grant_id"],),
    ).fetchone()
    if grant is None:
        return None
    try:
        if _parse_utc(grant["expires_at"]) > now:
            return None
    except (TypeError, ValueError):
        return None
    return grant


def unclaimed_candidates(connection: sqlite3.Connection, campaign_id: str) -> list[str]:
    """Attempt ids of a campaign that may hold an expired, never-claimed grant."""
    return [
        row["id"]
        for row in connection.execute(
            """SELECT id FROM campaign_stage_attempts WHERE campaign_id=?
            AND grant_id IS NOT NULL AND status IN ('granted','admitted')
            AND settled_cents IS NULL ORDER BY stage_key, attempt""",
            (campaign_id,),
        )
    ]


def idle_slot_holders(
    connection: sqlite3.Connection, *, after: str | None = None, limit: int = SWEEP_LIMIT
) -> tuple[list[str], str | None]:
    """Idle campaigns with a freed attempt not yet recorded (the reconcile sweep's work list).

    Paged like ``idle_holders``: at most ``limit`` ids after ``after`` and the next cursor
    (``None`` once the window comes up short, so the sweep wraps around)."""
    ids = [
        row["id"]
        for row in connection.execute(
            f"""SELECT DISTINCT c.id FROM campaign_stage_attempts a
            JOIN campaigns c ON c.id=a.campaign_id
            WHERE a.provider_id IS NOT NULL AND {FREED_SQL} AND {UNRECORDED_SQL}
            AND c.id > ? ORDER BY c.id LIMIT ?""",
            (after or "", limit),
        )
    ]
    return ids, (ids[-1] if ids and len(ids) >= limit else None)


def unleased_release_reason(status: str, steering_paused: bool) -> tuple[str, str] | None:
    """``(slot_freed reason, failure detail)`` when a campaign in this state gives back each
    grant no worker took, else None (it may still run them).

    The one condition both the runner's pass and its in-transaction re-read use: paused
    (``paused_delegation_lapsed``, or ``running`` with a steering pause; ADR 0167) or
    ``blocked`` (ADR 0208 addendum). A blocked campaign starts nothing until a person or the
    evidence path unblocks it, so a grant nobody leased is as unusable as a paused one's and,
    under a recorded route width, would hold a route slot for every campaign until it expires."""
    if status == "paused_delegation_lapsed" or (status == "running" and steering_paused):
        return PAUSED_UNLEASED_GRANT, PAUSED_RELEASE_DETAIL
    if status == "blocked":
        return BLOCKED_UNLEASED_GRANT, BLOCKED_RELEASE_DETAIL
    return None


def releasable_unleased_grant(
    connection: sqlite3.Connection, attempt_id: str
) -> sqlite3.Row | None:
    """The attempt's grant when a paused or blocked campaign may give it back, else None
    (fail closed).

    Unlike ``unclaimed_expired_grant`` the grant may still be live: such a campaign starts
    nothing, so a grant nobody ever took cannot be used by the campaign. The evidence is that
    no worker ever took a lease on it, no external operation exists for it and the attempt
    has recorded no operation of its own; a Salad attempt is also still ``awaiting_lease``
    with no lease or operation on its Salad row. The store refuses a lease on a reconciled
    grant, and the caller re-reads all of this inside the write transaction. A grant in
    ``cancel_requested``, with any lease row (even an expired one) or with any operation is
    never released here."""
    attempt = connection.execute(
        """SELECT * FROM campaign_stage_attempts a WHERE a.id=? AND a.grant_id IS NOT NULL
        AND a.status IN ('granted','admitted') AND a.settled_cents IS NULL
        AND a.operation_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM campaign_salad_attempts sa WHERE sa.attempt_id=a.id
            AND (sa.phase<>'awaiting_lease' OR sa.lease_id IS NOT NULL
                OR sa.operation_id IS NOT NULL))""",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        return None
    return connection.execute(
        """SELECT * FROM execution_grants g WHERE g.id=? AND g.status='active'
        AND NOT EXISTS (SELECT 1 FROM execution_leases l WHERE l.grant_id=g.id)
        AND NOT EXISTS (SELECT 1 FROM external_operations o WHERE o.grant_id=g.id)""",
        (attempt["grant_id"],),
    ).fetchone()


def waiters_ahead(
    connection: sqlite3.Connection, project_id: str, attempt: Mapping[str, Any]
) -> int:
    """Attempts of the project queued for a grant before ``attempt`` (fair FIFO).

    A waiter is a reserved attempt with no grant and no grant request, of a campaign the
    runner is driving and that is not paused, that was refused for the project limits
    (``campaign.stage_waiting_limits`` carries its ``attempt_id``). Order is the attempt's
    creation time, then id: a resumed campaign's new attempt joins the back of the line."""
    row = connection.execute(
        """SELECT count(*) AS n FROM campaign_stage_attempts w
        JOIN campaigns wc ON wc.id=w.campaign_id
        WHERE wc.project_id=? AND wc.status IN ('running','stopping')
        AND w.status='reserved' AND w.grant_id IS NULL AND w.id<>?
        AND (w.created_at < ? OR (w.created_at = ? AND w.id < ?))
        AND NOT EXISTS (SELECT 1 FROM campaign_steering s
            WHERE s.campaign_id=w.campaign_id AND s.paused=1)
        AND NOT EXISTS (SELECT 1 FROM campaign_step_intents i WHERE i.campaign_id=w.campaign_id
            AND i.stage_key=w.stage_key AND i.attempt=w.attempt AND i.step='issue_grant')
        AND EXISTS (SELECT 1 FROM events e WHERE e.project_id=wc.project_id
            AND e.event_type='campaign.stage_waiting_limits'
            AND json_extract(e.payload_json, '$.attempt_id')=w.id)""",
        (project_id, attempt["id"], attempt["created_at"], attempt["created_at"], attempt["id"]),
    ).fetchone()
    return int(row["n"])
