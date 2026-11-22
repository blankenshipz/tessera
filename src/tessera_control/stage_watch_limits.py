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

# Watch decision requests (ADR 0111): at most this many are recorded per operation, with a
# ``decision_seq`` of at most ``MAX_DECISION_SEQ``. A request is an advisory record only.
MAX_DECISIONS_PER_OPERATION = 16
MAX_DECISION_SEQ = 1000
# The closed kind-to-reason pairing; any other pair is refused ``decision_not_allowed``.
DECISION_KIND_REASONS = {
    "retry_request": frozenset({"transient_exit", "probe_lost"}),
    "resize_request": frozenset({"oom_killed", "resource_limit"}),
}


# Closed set of ``reason`` codes on a refused watch report (body ``{"detail", "reason"}``).
# PERMANENT: the same body can never succeed, so a worker may settle the report as refused.
# TRANSIENT: the identical body may succeed later. A refusal with no reason, or a reason
# outside this set (auth, restore hold, missing Idempotency-Key, schema), is treated as
# transient by the worker. ``wrong_worker`` is a worker whose credential no longer covers the
# grant's project; an operation that is not this worker's at all answers ``unknown_operation``
# with the same 404 as a missing one, so the code is not an existence oracle. The decision route
# shares this set: ``decision_budget_exhausted`` (the operation already has
# ``MAX_DECISIONS_PER_OPERATION`` recorded decisions) and ``decision_not_allowed`` (a kind and
# reason outside ``DECISION_KIND_REASONS``) are its own.
PERMANENT_REASONS = frozenset(
    {
        "unknown_operation",
        "wrong_worker",
        "worker_fenced",
        "lease_mismatch",
        "stale_epoch",
        "grant_mismatch",
        "grant_reconciled",
        "before_lease",
        "out_of_order",
        "routine_budget_exhausted",
        "budget_exhausted",
        "key_conflict",
        "decision_budget_exhausted",
        "decision_not_allowed",
    }
)
TRANSIENT_REASONS = frozenset({"too_early"})
REASON_STATUS = {
    "unknown_operation": 404,
    "wrong_worker": 404,
    "worker_fenced": 409,
    "lease_mismatch": 409,
    "stale_epoch": 409,
    "grant_mismatch": 409,
    "grant_reconciled": 409,
    "before_lease": 409,
    "out_of_order": 409,
    "routine_budget_exhausted": 409,
    "budget_exhausted": 409,
    "key_conflict": 409,
    "decision_budget_exhausted": 409,
    "decision_not_allowed": 409,
    "too_early": 425,
}
if set(REASON_STATUS) != PERMANENT_REASONS | TRANSIENT_REASONS or (
    PERMANENT_REASONS & TRANSIENT_REASONS
):
    raise RuntimeError("stage watch reason codes are inconsistent")


def routine_limit(max_reports: int) -> int:
    """How many routine reports a watch with this ``max_reports`` may plan to send."""
    reserve = min(TERMINAL_RESERVE, max_reports // 4)
    return max(0, max_reports - reserve)
