"""The ``not_before`` hold (G-06): an approved campaign waits until its start time.

Derived, never stored: a campaign whose status is ``running`` and whose spec carries a
``not_before`` still in the future is *waiting*. The runner does not drive its stages and every
read shows the state and reason. Nothing is persisted, so there is no migration, and the
approval's own expiry still bounds the wait (the runner pauses an expired approval first).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .campaign_models import parse_not_before

WAITING_STATUSES = frozenset({"running"})
REASON = "not_before"


def not_before_of(spec_json: str | dict[str, Any] | None) -> datetime | None:
    """The spec's start instant, or None (absent, v1, or unreadable: never a hold)."""
    try:
        spec = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
        value = spec.get("not_before") if isinstance(spec, dict) else None
        return parse_not_before(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def waiting(
    spec_json: str | dict[str, Any] | None, status: str, now: datetime
) -> dict[str, Any] | None:
    """``{"state": "waiting", "reason": "not_before", ...}`` while the hold is on, else None."""
    if status not in WAITING_STATUSES:
        return None
    start = not_before_of(spec_json)
    if start is None or now >= start:
        return None
    return {
        "state": "waiting",
        "reason": REASON,
        "not_before": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "seconds_remaining": int((start - now).total_seconds()),
    }
