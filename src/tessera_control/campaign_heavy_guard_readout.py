"""Read-only view of the heavy-stage placement guard's recorded events (ADR 0107).

The guard itself records ``campaign.heavy_stage_local_fallback`` (a warning) and the transient
skip reason ``heavy_local_refused`` inside ``campaign.stage_waiting_placement``. This module only
reads them, for campaign status and the stuck detector. It writes nothing and returns ids,
codes and timestamps, never free text beyond the recorded reason codes.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

FALLBACK_EVENT = "campaign.heavy_stage_local_fallback"
WAITING_EVENT = "campaign.stage_waiting_placement"
PLACED_EVENT = "campaign.stage_placed"
REFUSED_REASON = "heavy_local_refused"


def _events(connection: sqlite3.Connection, project_id, campaign_id, event_type) -> list[dict]:
    rows = connection.execute(
        """SELECT cursor, payload_json, created_at FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (project_id, event_type, campaign_id),
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            continue
        if isinstance(payload, dict):
            out.append({**payload, "_cursor": row["cursor"], "_at": row["created_at"]})
    return out


def _reasons_of_skip(skip: dict[str, Any]) -> list[str]:
    detail = skip.get("detail")
    if isinstance(detail, list):
        return [str(item) for item in detail]
    return [part.strip() for part in str(detail or "").split(",") if part.strip()]


def _refusal(event: dict[str, Any]) -> dict[str, Any] | None:
    for skip in event.get("skipped") or ():
        if isinstance(skip, dict) and skip.get("reason") == REFUSED_REASON:
            return {
                "mode": "refuse",
                "reasons": _reasons_of_skip(skip),
                "signals": list(event.get("signals") or []),
                "provider_id": skip.get("provider_id"),
                "attempt": event.get("attempt"),
                "recorded_at": event["_at"],
                "_cursor": event["_cursor"],
            }
    return None


def stage_guards(
    connection: sqlite3.Connection, project_id: str, campaign_id: str
) -> dict[str, dict[str, Any]]:
    """Per stage key, the latest warning or refusal. ``waiting`` is true for a refusal the stage
    has not been placed past. Stages with no such event are absent."""
    fallbacks = _events(connection, project_id, campaign_id, FALLBACK_EVENT)
    waits = _events(connection, project_id, campaign_id, WAITING_EVENT)
    placed = _events(connection, project_id, campaign_id, PLACED_EVENT)
    latest: dict[str, dict[str, Any]] = {}
    for event in fallbacks:
        latest[event.get("stage_key")] = {
            "mode": event.get("mode") or "warn",
            "reasons": list(event.get("reasons") or []),
            "signals": list(event.get("signals") or []),
            "provider_id": event.get("provider_id"),
            "attempt": event.get("attempt"),
            "waiting": False,
            "recorded_at": event["_at"],
            "_cursor": event["_cursor"],
        }
    last_wait: dict[str, dict[str, Any]] = {}
    for event in waits:
        last_wait[event.get("stage_key")] = event
    for key, event in last_wait.items():
        refusal = _refusal(event)
        if refusal is None:
            continue
        placed_since = any(
            p.get("stage_key") == key and (p.get("attempt") or 0) >= (event.get("attempt") or 0)
            for p in placed
        )
        refusal["waiting"] = not placed_since and not event.get("final")
        if key not in latest or refusal["_cursor"] > latest[key]["_cursor"]:
            latest[key] = refusal
    return latest


def status_view(
    connection: sqlite3.Connection, project_id: str, campaign_id: str
) -> list[dict[str, Any]]:
    """The campaign status ``heavy_guard`` list: one entry per stage with a recorded warning or
    refusal ({stage_key, mode, reasons, signals, provider_id?, waiting, recorded_at}), else []."""
    out = []
    for key, item in stage_guards(connection, project_id, campaign_id).items():
        entry = {
            "stage_key": key,
            "mode": item["mode"],
            "reasons": item["reasons"],
            "signals": item["signals"],
            "waiting": item["waiting"],
            "recorded_at": item["recorded_at"],
        }
        if item.get("provider_id"):
            entry["provider_id"] = item["provider_id"]
        out.append(entry)
    return sorted(out, key=lambda e: e["stage_key"])
