"""Where each stage of a campaign landed and why it is not progressing (read-only).

One bounded record per stage from rows the platform already keeps: the chosen provider and
profile, the recorded placement decision (candidates and skip reason codes), the waiting events a
stage has produced, the latest attempt with its grant, its executor lease and the executor's
heartbeat age, and failure counts by class. It returns codes, ages and counts only. It never
returns free-text failure detail, log text, principal or credential ids, worker names, hosts or
addresses; a free-text waiting reason is withheld unless it is a short code.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .campaign_placement import placements
from .stage_rejections import EVENT as REJECTION_EVENT
from .store import _parse_utc
from .workers import HEARTBEAT_MAX_AGE

_CODE = re.compile(r"[a-z0-9_.:-]{1,64}")
_WAIT_PREFIX = "campaign.stage_waiting_"
_PER_STAGE_CAP = 20


def _age(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    try:
        return max(0, int((now - _parse_utc(value)).total_seconds()))
    except ValueError:
        return None


def _code(value: str | None) -> str | None:
    """A recorded reason only when it is a short code; free text is reported as withheld."""
    if not value:
        return None
    return value if _CODE.fullmatch(value) else "detail_withheld"


def _lease(connection: sqlite3.Connection, grant_id: str | None, now: datetime):
    if grant_id is None:
        return None, None
    row = connection.execute(
        """SELECT epoch, renewed_at, expires_at, hard_expires_at, worker_principal_id
        FROM execution_leases WHERE grant_id=? ORDER BY renewed_at DESC, id DESC LIMIT 1""",
        (grant_id,),
    ).fetchone()
    if row is None:
        return {"held": False, "reason": "no_lease_recorded"}, None
    try:
        expired = _parse_utc(row["expires_at"]) <= now or _parse_utc(row["hard_expires_at"]) <= now
        remaining = int((_parse_utc(row["expires_at"]) - now).total_seconds())
    except ValueError:
        expired, remaining = True, None
    lease = {
        "held": not expired,
        "epoch": row["epoch"],
        "renewed_age_seconds": _age(row["renewed_at"], now),
        "expires_in_seconds": remaining,
        "expired": expired,
    }
    worker = connection.execute(
        "SELECT lifecycle_state, heartbeat_at FROM worker_registry WHERE worker_principal_id=?",
        (row["worker_principal_id"],),
    ).fetchone()
    if worker is None:
        return lease, {"registered": False}
    age = _age(worker["heartbeat_at"], now)
    return lease, {
        "registered": True,
        "lifecycle_state": worker["lifecycle_state"],
        "heartbeat_age_seconds": age,
        "heartbeat_stale": age is None or age > HEARTBEAT_MAX_AGE.total_seconds(),
    }


def _attempt_view(connection: sqlite3.Connection, row: Mapping[str, Any], now: datetime):
    grant = None
    if row["grant_id"]:
        g = connection.execute(
            """SELECT status, max_exposure_cents, expires_at, settlement_cents
            FROM execution_grants WHERE id=?""",
            (row["grant_id"],),
        ).fetchone()
        grant = None if g is None else dict(g)
    lease, executor = _lease(connection, row["grant_id"], now)
    return {
        "attempt": row["attempt"],
        "status": row["status"],
        "phase": row["phase"],
        "failure_class": row["failure_class"],
        "exit_code": row["exit_code"],
        "hold_cents": row["hold_cents"],
        "settled_cents": row["settled_cents"],
        "updated_at": row["updated_at"],
        "age_seconds": _age(row["updated_at"], now),
        "waiting_reason": _code(row["waiting_reason"]),
        "grant": grant,
        "lease": lease,
        "executor": executor,
    }


def stage_status(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    cid = campaign["id"]
    recorded = placements(connection, campaign)
    steering = connection.execute(
        "SELECT paused, max_concurrent_stages FROM campaign_steering WHERE campaign_id=?", (cid,)
    ).fetchone()
    stages = []
    rows = connection.execute(
        """SELECT s.stage_key, s.status, s.attempts_used, s.provider_id, z.profile
        FROM campaign_stages s LEFT JOIN campaign_salad_stages z
        ON z.campaign_id = s.campaign_id AND z.stage_key = s.stage_key
        WHERE s.campaign_id=? ORDER BY s.topo_index""",
        (cid,),
    ).fetchall()
    for stage in rows:
        key = stage["stage_key"]
        mine = [p for p in recorded if p.get("stage_key") == key]
        placement = None
        if mine:
            last = mine[-1]
            placement = {
                "attempt": last.get("attempt"),
                "chosen_provider": last.get("provider_id"),
                "candidates": list(last.get("candidates") or []),
                "skipped": [
                    {"provider_id": s.get("provider_id"), "reason": _code(s.get("reason"))}
                    for s in (last.get("skipped") or [])[:_PER_STAGE_CAP]
                ],
                "recorded_at": last.get("recorded_at"),
            }
        waits = connection.execute(
            """SELECT event_type, count(*) AS n, max(created_at) AS last_at FROM events
            WHERE project_id=? AND event_type LIKE ?
            AND json_extract(payload_json,'$.campaign_id')=?
            AND json_extract(payload_json,'$.stage_key')=? GROUP BY event_type""",
            (campaign["project_id"], _WAIT_PREFIX + "%", cid, key),
        ).fetchall()
        rejected = connection.execute(
            """SELECT json_extract(payload_json,'$.attempt') AS attempt,
            json_extract(payload_json,'$.step') AS step,
            json_extract(payload_json,'$.code') AS code, created_at
            FROM events WHERE project_id=? AND event_type=?
            AND json_extract(payload_json,'$.campaign_id')=?
            AND json_extract(payload_json,'$.stage_key')=? ORDER BY cursor""",
            (campaign["project_id"], REJECTION_EVENT, cid, key),
        ).fetchall()
        counts: dict[str, int] = {}
        for item in rejected:
            counts[item["code"]] = counts.get(item["code"], 0) + 1
        latest_rejection = None
        if rejected:
            last = rejected[-1]
            latest_rejection = {
                "attempt": last["attempt"],
                "step": last["step"],
                "code": last["code"],
                "at": last["created_at"],
                "age_seconds": _age(last["created_at"], now),
            }
        latest = connection.execute(
            """SELECT a.attempt, a.status, a.exit_code, a.failure_class, a.hold_cents,
            a.settled_cents, a.grant_id, a.updated_at, sa.phase, sa.waiting_reason
            FROM campaign_stage_attempts a
            LEFT JOIN campaign_salad_attempts sa ON sa.attempt_id = a.id
            WHERE a.campaign_id=? AND a.stage_key=? ORDER BY a.attempt DESC LIMIT 1""",
            (cid, key),
        ).fetchone()
        failures = connection.execute(
            """SELECT failure_class, count(*) AS n FROM campaign_stage_attempts
            WHERE campaign_id=? AND stage_key=? AND failure_class IS NOT NULL
            GROUP BY failure_class ORDER BY failure_class""",
            (cid, key),
        ).fetchall()
        stages.append(
            {
                "stage_key": key,
                "status": stage["status"],
                "attempts_used": stage["attempts_used"],
                "provider_id": stage["provider_id"],
                "profile": stage["profile"],
                "placement": placement,
                "waiting": [
                    {
                        "reason": w["event_type"][len(_WAIT_PREFIX) :],
                        "count": w["n"],
                        "last_at": w["last_at"],
                    }
                    for w in waits
                ],
                "latest_attempt": None
                if latest is None
                else _attempt_view(connection, latest, now),
                "rejections": {"latest": latest_rejection, "counts": counts},
                "failure_counts": {f["failure_class"]: f["n"] for f in failures},
            }
        )
    return {
        "campaign_id": cid,
        "status": campaign["status"],
        "paused": bool(steering["paused"]) if steering else False,
        "max_concurrent_stages": steering["max_concurrent_stages"] if steering else None,
        "heartbeat_max_age_seconds": int(HEARTBEAT_MAX_AGE.total_seconds()),
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stages": stages,
    }


__all__ = ["stage_status"]
