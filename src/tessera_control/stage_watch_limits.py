"""Shared limits of the stage watch report path (ADR 0087, first slice).

One setting for both sides: the control route enforces these per operation and the worker's
stage contract (``max_reports``) and watcher are bound by the same numbers, so a watcher can
never plan to send more than control will accept, and the budget that is held back for the
closing reports (``stuck`` and ``failed``) cannot be spent by routine reports.
"""

from __future__ import annotations

# Total reports recorded per operation (all kinds). A stage contract's ``max_reports`` is at
# most this.
MAX_REPORTS_PER_OPERATION = 200

# Held back for escalation kinds: ``heartbeat`` and ``progress`` are accepted only while
# fewer than ``MAX_REPORTS_PER_OPERATION - TERMINAL_RESERVE`` reports are recorded;
# ``stuck`` and ``failed`` are accepted until the total cap, so a later ``failed`` is never
# refused because routine reports used the budget up.
TERMINAL_RESERVE = 20

ROUTINE_KINDS = frozenset({"heartbeat", "progress"})
TERMINAL_KINDS = frozenset({"stuck", "failed"})

# At most one recorded report of each routine kind per interval per operation (the worker
# paces itself the same way; control coalesces anything faster).
MIN_ROUTINE_INTERVAL_SECONDS = 30


def routine_limit(max_reports: int) -> int:
    """How many routine reports a watch with this ``max_reports`` may plan to send."""
    reserve = min(TERMINAL_RESERVE, max_reports // 4)
    return max(0, max_reports - reserve)
