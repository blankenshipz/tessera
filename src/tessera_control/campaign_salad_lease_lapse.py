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

The same unwind also takes an attempt whose lease is still current but can no longer cover its
route window (``LEASE_WINDOW_SHORT_BEFORE_ROUTE``): the lease's hard expiry is fixed at issue,
before packaging, so a long review or a long executor/guardian queue comes out of the run itself,
and the guardian would cut the workload at the shortened deadline (a permanent
``route_deadline_reached``). Before ``start_route`` and while a started route has no launch effect
(the executor has not marked it executing and nothing was ever sent), a lease that lost more than
``PRE_ROUTE_WAIT_TOLERANCE_SECONDS`` to waiting and whose remaining hard life is shorter than the
route window is requeued for a fresh grant and lease, at 0 on the store's own no-claim proof,
under the same repeat bound. The route still never runs past its lease, grant or credential.
The run a lease must still cover is the route window, but never more than any lease is issued
for (declared duration plus provisioning allowance), and when no fresh lease could cover it either
(a credential, grant or approval expiry binds) the attempt is closed ``lease_capped_by_expiry``
instead of being requeued in a loop, or ``lease_bound_by_grant_ttl`` (the stage fails) when the
stage's own grant lifetime is what caps it (``campaign_salad_lease_cap``).

Everything here is read from the store, never parsed from a refusal message, and it applies
only before the route ran: a completed ``start_route`` takes this path only while the route has no
create or start effect, and a claimed provider effect still takes the claimed unwind inside
``unwind``. A refusal that is not about
worker authority (a review, policy, capability or configuration refusal) is untouched and stays
fail-closed.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import campaign_salad_lease_cap as lease_cap
from .campaign_autoreconcile import MAX_AUTO_REQUEUES, NOT_STARTED, repeat_count
from .salad_precreate import launch_effects
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
# The lease is current but its remaining hard life no longer covers the route window, after more
# than PRE_ROUTE_WAIT_TOLERANCE_SECONDS of waiting since it was issued (reviews, queues).
LEASE_WINDOW_SHORT_BEFORE_ROUTE = "lease_window_short_before_route"
# Waiting before the route that a lease may absorb: the route window already carries a 1500 s
# provisioning allowance beyond the declared run (ADR 0206), so a few minutes of review or queue
# leave the run whole. Past this the attempt takes a fresh lease instead of a shortened run.
PRE_ROUTE_WAIT_TOLERANCE_SECONDS = 600
# Phases checked for a short lease window: just before ``start_route``, and a started route the
# executor has not launched yet (no create or start effect).
WINDOW_CHECKED_PHASES = frozenset({"route_proposed", "route_started"})
_DETAILS = {
    LEASE_WINDOW_SHORT_BEFORE_ROUTE: (
        "the lease's remaining hard life no longer covered the route window after waiting "
        "before the route; nothing was sent to the provider"
    ),
}


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


def window_shortfall(
    issued: datetime, hard: datetime, now: datetime, window_seconds: int, *, margin: float = 0
) -> int | None:
    """Seconds by which the lease's remaining hard life (at ``now + margin``) falls short of the
    route window, or None when it covers it or when the lease waited at most
    ``PRE_ROUTE_WAIT_TOLERANCE_SECONDS`` since it was issued.

    Only time lost to waiting counts: a lease that a grant or credential clamped from its issue
    would be no longer if taken again, so it is never short here before it has waited. Shared by
    the runner (before ``start_route``) and the executor (before ``mark_executing``)."""
    at = now + timedelta(seconds=margin)
    remaining = (hard - at).total_seconds()
    if remaining >= window_seconds:
        return None
    if (at - issued).total_seconds() <= PRE_ROUTE_WAIT_TOLERANCE_SECONDS:
        return None
    return math.ceil(window_seconds - remaining)


def window_problem(driver: SaladStageDriver, row: dict[str, Any], now: datetime) -> str | None:
    """``LEASE_WINDOW_SHORT_BEFORE_ROUTE`` when the attempt's lease can no longer cover its route
    window (``window_shortfall``) and nothing was sent for the route, else None."""
    return None if short_need(driver, row, now) is None else LEASE_WINDOW_SHORT_BEFORE_ROUTE


def short_need(driver: SaladStageDriver, row: dict[str, Any], now: datetime) -> int | None:
    """The seconds the lease must still cover (``lease_cap.needed_seconds``) when it falls short
    of them after waiting and nothing was sent for the route, else None.

    Only a route with no create or start effect, no provider reference and no live receipt, in
    ``route_proposed`` or ``route_started`` (never ``executing``) qualifies. Anything unreadable
    is None here: the ordinary checks still apply."""
    if (
        row.get("phase") not in WINDOW_CHECKED_PHASES
        or row.get("lease_id") is None
        or row.get("route_id") is None
        or row.get("provider_id") is not None
        or row.get("live_receipt_json") is not None
    ):
        return None
    state = driver.route_state(row["route_id"])
    if state is None or state.get("provider_id") is not None or launch_effects(state):
        return None
    try:
        window = int(state["proposal"]["selection"]["resources"]["max_seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    lease = driver.runner._one(
        """SELECT l.issued_at, l.hard_expires_at, t.max_duration_seconds
        FROM execution_leases l LEFT JOIN execution_grants g ON g.id = l.grant_id
        LEFT JOIN tasks t ON t.id = g.task_id WHERE l.id=?""",
        (row["lease_id"],),
    )
    if lease is None:
        return None
    try:
        issued = _parse_utc(lease["issued_at"])
        hard = _parse_utc(lease["hard_expires_at"])
    except (TypeError, ValueError):
        return None
    need = lease_cap.needed_seconds(window, lease["max_duration_seconds"])
    if window_shortfall(issued, hard, now, need) is None:
        return None
    return need


def capped_problem(
    driver: SaladStageDriver, ctx: _Ctx, attempt: dict[str, Any], need: int, now: datetime
) -> str | None:
    """``LEASE_CAPPED_BY_EXPIRY`` (its record emitted once) when no fresh grant and lease could
    cover ``need`` seconds either, so a requeue would only loop, or ``LEASE_BOUND_BY_GRANT_TTL``
    when the stage's own grant lifetime is what caps it (waiting could never end); else None
    (requeue)."""
    from .campaign_salad import salad_config
    from .campaign_steps import StepFailed

    try:
        worker = driver._grant(attempt)["worker_principal_id"]
        ttl = salad_config(driver._spec(ctx, attempt["stage_key"])).grant_ttl_seconds
        record = lease_cap.shortfall(driver.runner, ctx.approval, worker, ttl, need, now)
    except (KeyError, TypeError, ValueError, StepFailed):
        return None  # unreadable: the ordinary bounded requeue still applies
    if record is None:
        return None
    driver._emit_once(
        ctx,
        lease_cap.CAPPED_EVENT,
        {"attempt_id": attempt["id"]},
        {
            "stage_key": attempt["stage_key"],
            "attempt": attempt["attempt"],
            "worker_principal_id": worker,
            **record,
        },
    )
    if lease_cap.is_static(record):
        return lease_cap.LEASE_BOUND_BY_GRANT_TTL
    return lease_cap.LEASE_CAPPED_BY_EXPIRY


def worker_problem(driver: SaladStageDriver, grant: dict[str, Any], now: datetime) -> str | None:
    """Why control's ``require_ready_worker`` would refuse the grant's worker with a Conflict
    (not ready, invalid liveness, not seen within its heartbeat window), or None.

    Read the way control reads it: the registry row of the worker's own credential in the
    grant's org and project. A missing row is control's Forbidden "not registered", a
    configuration problem, never a transient lapse, so it is None here too."""
    worker = driver.runner._one(
        """SELECT registry.lifecycle_state, registry.last_seen_at FROM worker_registry registry
        JOIN credentials ON credentials.id = registry.credential_id
        WHERE registry.worker_principal_id=? AND credentials.principal_id=?
        AND registry.project_id=?
        AND registry.org_id=(SELECT org_id FROM projects WHERE id=?)""",
        (
            grant["worker_principal_id"],
            grant["worker_principal_id"],
            grant["project_id"],
            grant["project_id"],
        ),
    )
    if worker is None:
        return None
    if worker["lifecycle_state"] != "ready":
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
    if reason in (lease_cap.LEASE_CAPPED_BY_EXPIRY, lease_cap.LEASE_BOUND_BY_GRANT_TTL):
        # No fresh lease could cover the run: not requeued (that would loop), not a scientific
        # retry. A renewable bound makes the stage wait for it; the stage's own grant lifetime
        # fails the stage, since no renewal lifts it (``campaign_salad_lease_cap``).
        record = lease_cap.recorded(driver.runner, ctx, attempt["id"]) or {
            "binding": "unreadable",
            "available_seconds": None,
            "need_seconds": None,
        }
        static = reason == lease_cap.LEASE_BOUND_BY_GRANT_TTL
        driver.runner._set_attempt(
            attempt["id"],
            status="failed_permanent" if static else "failed_transient",
            failure_class=reason,
            failure_detail=lease_cap.detail(
                {**record, "binding": lease_cap.STATIC_BINDING} if static else record
            )[:500],
        )
        return True
    why = _DETAILS.get(
        reason,
        "worker authority lapsed before the route started; nothing was sent to the provider",
    )
    detail = f"{reason}: {why}; released at 0 ({reason})"
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
    if phase not in PRE_ROUTE and phase != "route_started":
        return None
    recorded = recorded_reason(driver, ctx, attempt)
    if recorded is not None:
        return recorded  # an unwind already under way finishes on later passes
    now = _control_now()
    if phase in CHECKED_PHASES and not _started(driver, ctx, attempt):
        lapsed = lease_problem(driver, row, now)
        if lapsed is not None:
            return lapsed
    need = short_need(driver, row, now)
    if need is None:
        return None
    return capped_problem(driver, ctx, attempt, need, now) or LEASE_WINDOW_SHORT_BEFORE_ROUTE


# The refusal classes control itself raises when a step's worker authority is not current
# (``require_ready_worker``'s Conflict; a lapsed or fenced lease's Conflict). A refusal the
# runner raised locally (``step_refused``: operator configuration, capability declaration,
# an intent that no longer matches) or any other class is never a lapse.
CONTROL_AUTHORITY_REFUSALS = frozenset({"Conflict"})


def after_refusal(
    driver: SaladStageDriver,
    ctx: _Ctx,
    attempt: dict[str, Any],
    row: dict[str, Any],
    error: Any,
) -> str | None:
    """A pre-route step was refused: the reason to requeue, or None.

    Only a refusal control itself returned with the authority class (a Conflict, not a local
    ``step_refused``, a Forbidden or a replay of some other failure), and only when the store
    then shows that very authority lapsed: the attempt's lease, or its worker as
    ``require_ready_worker`` reads it. Review, policy and configuration refusals stay
    fail-closed."""
    if getattr(error, "error_class", None) not in CONTROL_AUTHORITY_REFUSALS:
        return None
    if row["phase"] not in PRE_ROUTE or _started(driver, ctx, attempt):
        return None
    now = _control_now()
    return lease_problem(driver, row, now) or worker_problem(driver, driver._grant(attempt), now)


__all__ = [
    "LEASE_LAPSED_BEFORE_ROUTE",
    "LEASE_WINDOW_SHORT_BEFORE_ROUTE",
    "REQUEUE_EVENT",
    "WORKER_UNAVAILABLE_BEFORE_ROUTE",
    "after_refusal",
    "before_handler",
    "requeue",
    "window_shortfall",
]
