"""Free a finished campaign's input releases so a fresh attempt can use the same snapshot.

A campaign's runner holds one active ``snapshot_releases`` row per (snapshot, worker) for
its attempts, and the unique active index means a second campaign on the same snapshot is
refused while that row is active. A campaign that is finished or can no longer run
anything (terminal, blocked or paused with every attempt settled) has no use for the
release, so it is revoked in the same transaction as the state change that made it idle,
with a ``campaign.release_freed`` event saying the system released it, why, for which
campaign and which snapshot digest.

Fail closed: a release is freed only when no attempt of ANY campaign that uses it is still
live (in flight, ``unknown``, or with a grant whose cost is not settled) and no execution
grant issued against it is unresolved. A release no campaign attempt used (a person's own
release) is never touched here. A release that stays held is refused to other campaigns as
before; the refusal names the holding campaign and the release expiry only to the same
organization.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

# Campaign statuses in which the runner will not use a release again without first
# creating a new one: terminal, blocked (only a person or settlement moves it on) and paused
# (a resumed campaign issues a fresh release for any new attempt).
IDLE_CAMPAIGN = (
    "succeeded",
    "stopped",
    "failed",
    "cancelled",
    "blocked",
    "paused_delegation_lapsed",
)
# Attempt statuses whose run can no longer use its input. ``unknown`` is not one of them: the
# run may still be live.
SETTLED_ATTEMPT = (
    "succeeded",
    "failed_transient",
    "failed_permanent",
    "refused_budget",
    "cancelled",
)
SWEEP_LIMIT = 50

EventWriter = Callable[[sqlite3.Connection, Mapping[str, Any], str, dict[str, Any]], None]


def _in(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def release_still_needed(connection: sqlite3.Connection, release_id: str) -> bool:
    """True while any attempt (any campaign) or unresolved grant may still use the release."""
    live_attempt = connection.execute(
        f"""SELECT 1 FROM campaign_stage_attempts WHERE release_id=? AND
        (status NOT IN ({_in(SETTLED_ATTEMPT)})
         OR (grant_id IS NOT NULL AND settled_cents IS NULL)) LIMIT 1""",
        (release_id,),
    ).fetchone()
    if live_attempt is not None:
        return True
    live_grant = connection.execute(
        """SELECT 1 FROM execution_grants WHERE snapshot_release_id=?
        AND status IN ('active','cancel_requested') LIMIT 1""",
        (release_id,),
    ).fetchone()
    return live_grant is not None


def free_idle_campaign_releases(
    connection: sqlite3.Connection,
    campaign_id: str,
    *,
    trigger: str,
    now: str,
    record_event: EventWriter,
    steering_paused: bool = False,
) -> list[dict[str, Any]]:
    """Revoke this campaign's releases nothing still needs, in the caller's transaction.

    ``steering_paused`` lets a ``running`` campaign whose steering pause is set (ADR 0061)
    count as idle (ADR 0167); the reason recorded is then ``paused``.

    ``trigger`` names what freed it (the campaign's own transition or the reconcile sweep);
    the reason recorded is the campaign's status as of this transaction."""
    campaign = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return []
    if campaign["status"] not in IDLE_CAMPAIGN and not (
        steering_paused and campaign["status"] == "running"
    ):
        return []
    reason = campaign["status"] if campaign["status"] in IDLE_CAMPAIGN else "paused"
    releases = connection.execute(
        """SELECT r.* FROM snapshot_releases r WHERE r.status='active' AND EXISTS
        (SELECT 1 FROM campaign_stage_attempts a WHERE a.campaign_id=? AND a.release_id=r.id)
        ORDER BY r.id""",
        (campaign_id,),
    ).fetchall()
    freed: list[dict[str, Any]] = []
    for release in releases:
        if release_still_needed(connection, release["id"]):
            continue
        updated = connection.execute(
            """UPDATE snapshot_releases SET status='revoked', revision=revision+1, revoked_at=?
            WHERE id=? AND status='active' AND revision=?""",
            (now, release["id"], release["revision"]),
        ).rowcount
        if not updated:
            continue
        item = {
            "release_id": release["id"],
            "snapshot_id": release["snapshot_id"],
            "snapshot_digest": release["digest"],
            "released_by": "system",
            "trigger": trigger,
            "reason": reason,
            "release_expires_at": release["expires_at"],
            "automatic": True,
            "credited_to": "campaign_runner",
        }
        record_event(connection, dict(campaign), "campaign.release_freed", item)
        freed.append({"campaign_id": campaign_id, **item})
    return freed


def idle_holders(
    connection: sqlite3.Connection, *, after: str | None = None, limit: int = SWEEP_LIMIT
) -> tuple[list[str], str | None]:
    """Idle campaigns that still hold an active release (the reconcile sweep's work list).

    Returns at most ``limit`` campaign ids after ``after``, in id order, and the cursor for
    the next pass: the last id of a full window, or ``None`` once the window comes up short,
    so the sweep wraps around and every holder is visited within a bounded number of passes."""
    ids = [
        row["campaign_id"]
        for row in connection.execute(
            f"""SELECT DISTINCT a.campaign_id FROM snapshot_releases r
            JOIN campaign_stage_attempts a ON a.release_id=r.id
            JOIN campaigns c ON c.id=a.campaign_id
            WHERE r.status='active' AND c.status IN ({_in(IDLE_CAMPAIGN)})
            AND a.campaign_id > ? ORDER BY a.campaign_id LIMIT ?""",
            (after or "", limit),
        )
    ]
    return ids, (ids[-1] if ids and len(ids) >= limit else None)


def holder_refusal(
    connection: sqlite3.Connection, release: Mapping[str, Any], requester_org_id: str
) -> str:
    """The refusal for a release another campaign (or a person) holds.

    Names the holding campaign only within the same organization; otherwise only that the
    snapshot is held and until when."""
    holder = connection.execute(
        """SELECT c.id, c.org_id FROM campaign_stage_attempts a
        JOIN campaigns c ON c.id=a.campaign_id WHERE a.release_id=?
        ORDER BY a.created_at, a.id LIMIT 1""",
        (release["id"],),
    ).fetchone()
    until = release["expires_at"]
    if holder is None:
        return f"a human release of this snapshot is active until {until}"
    if holder["org_id"] != requester_org_id:
        return f"the input snapshot is held until {until}"
    return f"the input snapshot is held by campaign {holder['id']} until {until}"


RELEASE_WAITING_EVENT = "campaign.stage_waiting_release"
# Triggers of a ``campaign.release_freed`` written because the campaign was paused.
PAUSE_RELEASE_TRIGGERS = ("campaign_paused", "paused_campaign_release")


def freed_by_pause(connection: sqlite3.Connection, campaign: Mapping[str, Any], snapshot_id: str):
    """True when this campaign's release of ``snapshot_id`` was given back because it was
    paused (ADR 0167): its next attempt then waits for the snapshot instead of failing."""
    marks = ",".join("?" for _ in PAUSE_RELEASE_TRIGGERS)
    return (
        connection.execute(
            f"""SELECT 1 FROM events WHERE project_id=? AND event_type='campaign.release_freed'
            AND json_extract(payload_json,'$.campaign_id')=?
            AND json_extract(payload_json,'$.snapshot_id')=?
            AND json_extract(payload_json,'$.trigger') IN ({marks}) LIMIT 1""",
            (campaign["project_id"], campaign["id"], snapshot_id, *PAUSE_RELEASE_TRIGGERS),
        ).fetchone()
        is not None
    )


def release_waiters_ahead(
    connection: sqlite3.Connection, project_id: str, snapshot_id: str, attempt: Mapping[str, Any]
) -> int:
    """Attempts queued for ``snapshot_id`` before ``attempt`` (fair FIFO, like grant slots).

    A waiter is a reserved attempt of a running, unpaused campaign on the same snapshot that
    was refused the release (``campaign.stage_waiting_release`` carries its ``attempt_id``)."""
    row = connection.execute(
        """SELECT count(*) AS n FROM campaign_stage_attempts w
        JOIN campaigns wc ON wc.id=w.campaign_id
        JOIN campaign_stages ws ON ws.campaign_id=w.campaign_id AND ws.stage_key=w.stage_key
        WHERE wc.project_id=? AND ws.input_snapshot_id=? AND wc.status IN ('running','stopping')
        AND w.id<>? AND w.status='reserved' AND w.release_id IS NULL
        AND (w.created_at < ? OR (w.created_at = ? AND w.id < ?))
        AND NOT EXISTS (SELECT 1 FROM campaign_steering s
            WHERE s.campaign_id=w.campaign_id AND s.paused=1)
        AND EXISTS (SELECT 1 FROM events e WHERE e.project_id=wc.project_id
            AND e.event_type='campaign.stage_waiting_release'
            AND json_extract(e.payload_json, '$.attempt_id')=w.id)""",
        (
            project_id,
            snapshot_id,
            attempt["id"],
            attempt["created_at"],
            attempt["created_at"],
            attempt["id"],
        ),
    ).fetchone()
    return int(row["n"])
