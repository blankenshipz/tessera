"""Map one Salad live-check poll to a non-secret stage-watch signal snapshot.

Pure: no I/O, no network, no clock, and no import from the Salad plane modules or the
worker. It turns values the live-check poll loop already holds into the closed mapping
of schema ``tessera.stage-watch.signals.v1`` that a remote watcher reads (see
docs/stage-watcher.md, "Remote placements").

What the output is and is not:

- ``state`` comes only from the provider status and the stop/termination flags;
  ``termination_confirmed``, like the stop flags, confirms the group ended (exited once
  ``poll_count`` >= 1).
- ``progress_marker`` is ``poll_count``, the driver's count of successful observe plus
  evidence passes. A marker that stops rising therefore means the provider-side driver
  stopped observing, not that the workload stalled. Salad gives this loop no output,
  CPU or memory counter, so none is reported.
- ``exit_code`` is omitted: the provider status carries none here.
- No identifiers, group names, hostnames, secrets or money appear. Nothing financial
  is accepted as an input, so none can reach the snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA = "tessera.stage-watch.signals.v1"

_EXITED_STATUSES = frozenset({"stopped", "succeeded", "failed"})


def state_from_poll(
    *,
    provider_status: object,
    termination_confirmed: bool = False,
    stop_confirmed: bool = False,
    provider_stop_confirmed: bool = False,
    poll_count: int = 0,
) -> str:
    """Return running, exited, absent or unknown for one poll.

    ``stopped`` before any successful post-start poll is the group's settled create
    state (unbilled, never started), so it stays unknown rather than exited.
    """
    status = provider_status if isinstance(provider_status, str) else None
    started = isinstance(poll_count, int) and not isinstance(poll_count, bool) and poll_count > 0
    if status == "deleted":
        return "absent"
    if started and (termination_confirmed or stop_confirmed or provider_stop_confirmed):
        return "exited"
    if status == "running":
        return "running"
    if started and status in _EXITED_STATUSES:
        return "exited"
    return "unknown"


def signals_from_poll(
    *,
    provider_status: object,
    termination_confirmed: bool = False,
    stop_confirmed: bool = False,
    provider_stop_confirmed: bool = False,
    poll_count: int = 0,
    now: datetime,
) -> dict[str, Any]:
    """Build the closed snapshot mapping for one successful poll.

    ``now`` must be timezone-aware; the caller supplies it (this module reads no clock).
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    snapshot: dict[str, Any] = {
        "schema": SCHEMA,
        "observed_at": now.isoformat(),
        "state": state_from_poll(
            provider_status=provider_status,
            termination_confirmed=termination_confirmed,
            stop_confirmed=stop_confirmed,
            provider_stop_confirmed=provider_stop_confirmed,
            poll_count=poll_count,
        ),
    }
    if isinstance(poll_count, int) and not isinstance(poll_count, bool) and poll_count >= 0:
        snapshot["progress_marker"] = poll_count
    return snapshot
