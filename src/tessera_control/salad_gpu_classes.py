"""Read-only listing of the Salad GPU classes the operator's profiles allow.

Eligible means named by a GPU profile in the operator providers file (``salad_profiles``);
that profile set is the only local source of truth for GPU class ids (the provider's own class
catalog is read live at admission and is never fetched here). Per class this reports the id,
the profiles that name it and their cost ceilings, the last hourly rate a guardian recorded on a
receipt and the allocation-instability counts recorded in a bounded recent window.

Nothing is invented: a class name or price that no receipt recorded is ``null`` with a
``not_recorded`` note, and instability counts are ``null`` (never zero) when no attempt in the
window carries a guardian final record. No credential reference, custody host or file content
is returned, only profile names and class ids.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .salad_campaign_profiles import SaladCampaignProfile, SaladCampaignProfiles

SCHEMA = "tessera.salad.gpu-classes.v1"
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 90
MAX_ROWS = 2000
NOT_RECORDED = "not_recorded"
NAME_NOTE = (
    "Class names are not recorded locally; the provider's class catalog is read live at "
    "admission. Select by id."
)
TRUNCATED_NOTE = (
    f"Only the newest {MAX_ROWS} recorded attempts were read, so the counts may be too low."
)
INSTABILITY_BASIS = (
    "Guardian final records of Salad attempts whose profile names the class: pre-start "
    "re-binds and attempts flagged allocation_uncertain."
)


class GpuClassListingError(ValueError):
    """The listing cannot be built safely; nothing is returned (fail closed)."""


def profiles_from_document(document: Mapping[str, Any]) -> list[SaladCampaignProfile]:
    raw = document.get("salad_profiles")
    if raw is None:
        return []
    try:
        return list(SaladCampaignProfiles.model_validate(raw).profiles)
    except ValueError:
        raise GpuClassListingError(
            "The Salad profiles in the providers file are invalid."
        ) from None


def load_profiles_from_file(path: Path) -> list[SaladCampaignProfile]:
    """The profiles of an operator providers file, read the way ``serve`` reads it."""
    from . import provider_registration as registration

    try:
        document, _digest, exists = registration.read_document(path)
    except (ValueError, OSError):
        raise GpuClassListingError("The providers file cannot be read.") from None
    if not exists:
        raise GpuClassListingError("The providers file does not exist.")
    return profiles_from_document(document)


def read_recent_attempts(
    database_path: Path,
    *,
    org_id: str | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Salad attempts in the window as ``{profile, guardian_final}`` (read-only connection)."""
    if type(days) is not int or not 1 <= days <= MAX_WINDOW_DAYS:
        raise GpuClassListingError(f"The window must be 1 to {MAX_WINDOW_DAYS} days.")
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        connection = sqlite3.connect(
            f"{Path(database_path).resolve().as_uri()}?mode=ro", uri=True, timeout=10
        )
    except sqlite3.Error:
        raise GpuClassListingError("The control database cannot be opened read-only.") from None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        query = """SELECT s.profile AS profile, sa.guardian_final_json AS final
            FROM campaign_salad_attempts sa
            JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
            JOIN campaign_salad_stages s
              ON s.campaign_id = a.campaign_id AND s.stage_key = a.stage_key
            JOIN campaigns c ON c.id = a.campaign_id
            WHERE sa.updated_at >= ?"""
        parameters: list[Any] = [since]
        if org_id is not None:
            query += " AND c.org_id = ?"
            parameters.append(org_id)
        query += " ORDER BY sa.updated_at DESC LIMIT ?"
        parameters.append(MAX_ROWS)
        rows = connection.execute(query, parameters).fetchall()
    except sqlite3.Error:
        raise GpuClassListingError("The recorded attempts cannot be read.") from None
    finally:
        connection.close()
    return [{"profile": row["profile"], "guardian_final": row["final"]} for row in rows]


def _final(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _rate(final: Mapping[str, Any]) -> str | None:
    value = final.get("published_hourly_rate_usd")
    return value if isinstance(value, str) and 0 < len(value) <= 20 else None


def build_listing(
    profiles: Iterable[SaladCampaignProfile],
    attempts: Iterable[Mapping[str, Any]] | None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """The listing from loaded profiles and recorded attempts (``None``: receipts unavailable)."""
    profiles = list(profiles)
    attempts = None if attempts is None else list(attempts)
    truncated = attempts is not None and len(attempts) >= MAX_ROWS
    by_class: dict[str, list[SaladCampaignProfile]] = {}
    for profile in profiles:
        if profile.gpu_class is not None:
            by_class.setdefault(profile.gpu_class, []).append(profile)
    class_of = {p.name: p.gpu_class for p in profiles if p.gpu_class is not None}
    seen: dict[str, dict[str, Any]] = {
        cid: {"attempts": 0, "rebinds": 0, "uncertain": 0, "rate": None} for cid in by_class
    }
    for attempt in attempts or ():
        cid = class_of.get(attempt.get("profile"))
        final = _final(attempt.get("guardian_final"))
        if cid is None or final is None:
            continue
        tally = seen[cid]
        tally["attempts"] += 1
        rebinds = final.get("prestart_rebinds")
        count = rebinds.get("count") if isinstance(rebinds, dict) else 0
        tally["rebinds"] += count if type(count) is int and count > 0 else 0
        tally["uncertain"] += 1 if final.get("allocation_uncertain") is True else 0
        if tally["rate"] is None:  # rows arrive newest first
            tally["rate"] = _rate(final)
    classes = []
    for cid in sorted(by_class):
        tally = seen[cid]
        recorded = attempts is not None and tally["attempts"] > 0
        instability: dict[str, Any] = {
            "status": "recorded" if recorded else NOT_RECORDED,
            "window_days": window_days,
            "attempts_with_guardian_record": tally["attempts"] if recorded else None,
            "prestart_rebinds": tally["rebinds"] if recorded else None,
            "allocation_uncertain_attempts": tally["uncertain"] if recorded else None,
        }
        if truncated:
            instability["truncated"] = True
        classes.append(
            {
                "id": cid,
                "name": None,
                "name_status": NOT_RECORDED,
                "profiles": sorted(p.name for p in by_class[cid]),
                "max_run_cents": max(p.max_run_cents for p in by_class[cid]),
                "published_hourly_rate_usd": tally["rate"],
                "price_status": "recorded_on_latest_receipt" if tally["rate"] else NOT_RECORDED,
                "allocation_instability": instability,
            }
        )
    return {
        "schema": SCHEMA,
        "classes": classes,
        "cpu_only_profiles": sum(1 for p in profiles if p.gpu_class is None),
        "notes": [NAME_NOTE, INSTABILITY_BASIS] + ([TRUNCATED_NOTE] if truncated else []),
    }


def list_gpu_classes(
    profiles: Iterable[SaladCampaignProfile],
    database_path: Path | None,
    *,
    org_id: str | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Listing for the (optionally one organization's) profiles; no database means no counts."""
    chosen = [p for p in profiles if org_id is None or p.org_id == org_id]
    attempts = (
        None
        if database_path is None
        else read_recent_attempts(database_path, org_id=org_id, days=days, now=now)
    )
    return build_listing(chosen, attempts, window_days=days)


def render_text(listing: Mapping[str, Any]) -> str:
    lines = [f"{len(listing['classes'])} eligible Salad GPU class(es)"]
    for item in listing["classes"]:
        stats = item["allocation_instability"]
        rate = item["published_hourly_rate_usd"] or "not_recorded"
        if stats["status"] == "recorded":
            seen = (
                f"{stats['attempts_with_guardian_record']} attempts, "
                f"{stats['prestart_rebinds']} re-binds, "
                f"{stats['allocation_uncertain_attempts']} uncertain"
            )
        else:
            seen = "not_recorded"
        if stats.get("truncated"):
            seen += " (truncated)"
        lines.append(
            f"{item['id']}  profiles={','.join(item['profiles'])}  "
            f"max_run_cents={item['max_run_cents']}  usd/hour={rate}  "
            f"instability({stats['window_days']}d)={seen}"
        )
    lines.extend(listing["notes"])
    return "\n".join(lines)
