"""A Salad attempt whose worker authority lapsed before its route started (ADR 0046).

Between packaging and ``start_route`` a Salad attempt can wait a long time for its plan and
source-use reviews. The execution lease the executor took at packaging has a fixed hard expiry
(the grant, the worker credential and the task's own duration bound, counted from the lease),
and a soft lease that lapsed is never renewed outside a running live check. So a long review
wait can leave the attempt holding authority that can no longer route, and the executor's
worker registration can go stale while it has nothing current to renew. Nothing was ever sent
to the provider in these phases.

Such an attempt is not a permanent failure of the stage. The runner unwinds it through the
existing unclaimed path (cancel any proposed route, revoke the grant, reconcile at 0 on the
store's own no-claim proof) and requeues the stage as ``not_started``: an attempt number, not a
scientific retry, with a fresh grant and lease. The identical-repeat bound of ADR 0046 still
holds (``MAX_AUTO_REQUEUES``): past it the attempt keeps ``t1_salad_unclaimed`` and the stage's
own scientific retry bound decides.

Everything here is read from the store, never parsed from a refusal message, and it applies
only before the route started: a completed ``start_route`` never takes this path, and a claimed
provider effect still takes the claimed unwind inside ``unwind``. A refusal that is not about
worker authority (a review, policy, capability or configuration refusal) is untouched and stays
fail-closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .campaign_autoreconcile import MAX_AUTO_REQUEUES, NOT_STARTED, repeat_count
from .store import _parse_utc
from .workers import HEARTBEAT_MAX_AGE

if TYPE_CHECKING:
    from .campaign_runner import _Ctx
    from .campaign_salad import SaladStageDriver

# The lease the attempt was packaged under can no longer authorize its route.
LEASE_LAPSED_BEFORE_ROUTE = "lease_lapsed_before_route"
# Control refused a pre-route step because the attempt's worker is not ready or not seen
# within its heartbeat window (``WorkerRegistryStore.require_ready_worker``).
WORKER_UNAVAILABLE_BEFORE_ROUTE = "worker_unavailable_before_route"
REQUEUE_EVENT = "campaign.salad_requeued_before_route"
# Phases checked before their handler runs. ``awaiting_review`` is left to finish its review
# first: a requeue there would only open a new review stop for every lapse while a person is
# away; once the review is approved the next phase is checked and requeued once.
CHECKED_PHASES = frozenset({"leased", "reviewed", "capability_ready", "route_proposed"})
# Phases whose handler refusals are examined (every pre-route phase).
PRE_ROUTE = frozenset(
    {"leased", "awaiting_review", "reviewed", "capability_ready", "route_proposed"}
)


def _control_now() -> datetime:
    """Control's own clock: it judges lease and heartbeat bounds on wall-clock UTC (never the
    runner's scheduling clock), so these checks agree with the refusal they anticipate."""
    return datetime.now(UTC)


def lease_problem(driver: SaladStageDriver, row: dict[str, Any], now: datetime) -> str | None:
    """Why the attempt's recorded lease can no longer authorize a route, or None."""
    if row.get("lease_id") is None:
        return None
    lease = driver.runner._one(
        "SELECT epoch, expires_at, hard_expires_at FROM execution_leases WHERE id=?",
        (row["lease_id"],),
    )
    if lease is None:
        return LEASE_LAPSED_BEFORE_ROUTE
    try:
        soft = _parse_utc(lease["expires_at"])
        hard = _parse_utc(lease["hard_expires_at"])
    except (TypeError, ValueError):
        return LEASE_LAPSED_BEFORE_ROUTE
    if row.get("epoch") is not None and lease["epoch"] != row["epoch"]:
        return LEASE_LAPSED_BEFORE_ROUTE
    # A lapsed soft lease is renewed again only inside a running live check, never before
    # the route starts, so either bound passing ends this lease's authority.
    if soft <= now or hard <= now:
        return LEASE_LAPSED_BEFORE_ROUTE
    return None


def worker_problem(driver: SaladStageDriver, grant: dict[str, Any], now: datetime) -> str | None:
    """Why control would refuse the grant's worker new lease authority, or None."""
    worker = driver.runner._one(
        """SELECT lifecycle_state, last_seen_at FROM worker_registry
        WHERE worker_principal_id=? AND project_id=?
        ORDER BY last_seen_at DESC LIMIT 1""",
        (grant["worker_principal_id"], grant["project_id"]),
    )
    if worker is None or worker["lifecycle_state"] != "ready":
        return WORKER_UNAVAILABLE_BEFORE_ROUTE
    try:
        seen = _parse_utc(worker["last_seen_at"])
    except (TypeError, ValueError):
        return WORKER_UNAVAILABLE_BEFORE_ROUTE
    if seen < now - HEARTBEAT_MAX_AGE:
        return WORKER_UNAVAILABLE_BEFORE_ROUTE
    return None


def recorded_reason(driver: SaladStageDriver, ctx: _Ctx, attempt: dict[str, Any]) -> str | None:
    """The reason this attempt was already taken off its route for, if it was."""
    event = driver.runner._one(
        """SELECT json_extract(payload_json,'$.reason') AS reason FROM events
        WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
        (ctx.campaign["project_id"], REQUEUE_EVENT, ctx.campaign["id"], attempt["id"]),
    )
    return None if event is None else str(event["reason"])


def _started(driver: SaladStageDriver, ctx: _Ctx, attempt: dict[str, Any]) -> bool:
    with driver.runner.store.runner_connection() as connection:
        return (
            driver.runner.steps.completed(
                connection,
                ctx.campaign["id"],
                attempt["stage_key"],
                attempt["attempt"],
                "start_route",
                0,
            )
            is not None
        )


def requeue(
    driver: SaladStageDriver,
    ctx: _Ctx,
    attempt: dict[str, Any],
    reason: str,
) -> bool:
    """Unwind the attempt (possibly over several passes) and close it ``not_started`` within
    ADR 0046's repeat bound; True when this pass made progress."""
    key = attempt["stage_key"]
    driver._emit_once(
        ctx,
        REQUEUE_EVENT,
        {"attempt_id": attempt["id"]},
        {"stage_key": key, "attempt": attempt["attempt"], "reason": reason},
    )
    if not driver.unwind(ctx, key, attempt):
        return True
    detail = (
        f"{reason}: worker authority lapsed before the route started; nothing was sent to the "
        f"provider; released at 0 ({reason})"
    )
    others = [a for a in driver.runner._attempts(ctx, key) if a["id"] != attempt["id"]]
    repeats = repeat_count(others, reason)
    failure_class = NOT_STARTED
    if repeats >= MAX_AUTO_REQUEUES:
        # ADR 0046's identical-repeat bound: the release at 0 stands, but the stage's own
        # scientific retry bound decides from here.
        failure_class = "t1_salad_unclaimed"
        detail = f"{detail}; not requeued as not started: {reason} repeated {repeats + 1} times"
    driver.runner._set_attempt(
        attempt["id"],
        status="failed_transient",
        failure_class=failure_class,
        failure_detail=detail[:500],
    )
    return True


def before_handler(
    driver: SaladStageDriver,
    ctx: _Ctx,
    attempt: dict[str, Any],
    row: dict[str, Any],
) -> str | None:
    """The reason to requeue this pre-route attempt instead of running its phase, or None."""
    phase = row["phase"]
    if phase not in PRE_ROUTE:
        return None
    recorded = recorded_reason(driver, ctx, attempt)
    if recorded is not None:
        return recorded  # an unwind already under way finishes on later passes
    if phase not in CHECKED_PHASES or _started(driver, ctx, attempt):
        return None
    return lease_problem(driver, row, _control_now())


def after_refusal(
    driver: SaladStageDriver,
    ctx: _Ctx,
    attempt: dict[str, Any],
    row: dict[str, Any],
) -> str | None:
    """A pre-route step was refused: the reason to requeue when the store shows the worker's
    authority itself lapsed (never a review, policy or configuration refusal), else None."""
    if row["phase"] not in PRE_ROUTE or _started(driver, ctx, attempt):
        return None
    now = _control_now()
    return lease_problem(driver, row, now) or worker_problem(driver, driver._grant(attempt), now)


__all__ = [
    "LEASE_LAPSED_BEFORE_ROUTE",
    "REQUEUE_EVENT",
    "WORKER_UNAVAILABLE_BEFORE_ROUTE",
    "after_refusal",
    "before_handler",
    "requeue",
]
