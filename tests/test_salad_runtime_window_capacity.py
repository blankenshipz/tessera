"""A Salad route's lease must cover its window, and the guardian must have room (ADR 0207).

Fakes only: real stores and runner on synthetic state (``SaladWorld``), a fake control port for
the executor, an in-process safety service with a fake provider. No network, no live Salad.

* ``window_shortfall``: pure table.
* Runner: a lease that waited and no longer covers the route window is requeued ``not_started``
  (``lease_window_short_before_route``) at 0, in ``route_proposed`` and ``route_started``.
* Executor: a short lease or a full/unreadable guardian defers the launch with nothing marked.
* Safety service ``open_frames`` and the pre-create refusal receipt / its requeue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_executor import PLAN, PROFILE, FakeControl, Harness, item
from test_salad_executor_concurrent_routes import (
    ConcurrentControl,
    GatedRunner,
    actions,
    executor_for,
    started,
)

from tessera_control import salad_precreate as pc
from tessera_control.campaign_autoreconcile import MAX_AUTO_REQUEUES, NOT_STARTED
from tessera_control.campaign_infra_retry import infra_attempts
from tessera_control.campaign_salad import FRAME_FENCE_BEFORE_CREATE, FRAME_FENCE_REFUSAL
from tessera_control.campaign_salad_executor import EXECUTOR_WINDOW_MARGIN_SECONDS
from tessera_control.campaign_salad_lease_cap import needed_seconds
from tessera_control.campaign_salad_lease_lapse import (
    LEASE_WINDOW_SHORT_BEFORE_ROUTE,
    PRE_ROUTE_WAIT_TOLERANCE_SECONDS,
    REQUEUE_EVENT,
    window_shortfall,
)
from tessera_control.salad_server import GuardianRefused

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
WINDOW = 3000


def _at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


# -- 1. window_shortfall ------------------------------------------------------------------------


def test_window_shortfall_covered_returns_none():
    assert window_shortfall(_at(-3600), _at(WINDOW + 500), NOW, WINDOW) is None


def test_window_shortfall_exactly_equal_remaining_is_covered():
    assert window_shortfall(_at(-3600), _at(WINDOW), NOW, WINDOW) is None


def test_window_shortfall_short_but_barely_waited_is_none():
    issued = _at(-PRE_ROUTE_WAIT_TOLERANCE_SECONDS)  # waited exactly the tolerance
    assert window_shortfall(issued, _at(WINDOW - 1000), NOW, WINDOW) is None
    assert window_shortfall(_at(-60), _at(10), NOW, WINDOW) is None


def test_window_shortfall_short_after_waiting_is_the_ceiled_gap():
    issued = _at(-(PRE_ROUTE_WAIT_TOLERANCE_SECONDS + 1))
    assert window_shortfall(issued, _at(WINDOW - 1000), NOW, WINDOW) == 1000
    assert window_shortfall(issued, _at(WINDOW - 0.4), NOW, WINDOW) == 1


def test_window_shortfall_margin_shifts_both_remaining_life_and_waiting():
    issued, hard = _at(-3600), _at(WINDOW + 50)
    assert window_shortfall(issued, hard, NOW, WINDOW) is None
    assert window_shortfall(issued, hard, NOW, WINDOW, margin=120) == 70
    # The margin also counts as waiting: 590 s waited is within tolerance, 590 + 20 is not.
    issued = _at(-590)
    assert window_shortfall(issued, _at(100), NOW, WINDOW) is None
    assert window_shortfall(issued, _at(100), NOW, WINDOW, margin=5) is None
    assert window_shortfall(issued, _at(100), NOW, WINDOW, margin=20) == WINDOW - 80


# -- 2. runner ----------------------------------------------------------------------------------


def _window(world: SaladWorld) -> int:
    state = world.route_state()
    return int(state["proposal"]["selection"]["resources"]["max_seconds"])


def _long_lived_credentials(world: SaladWorld) -> None:
    """A worker credential that outlives the route window, so a fresh lease could cover it and the
    short-window requeue applies (this world's credentials otherwise last about an hour, which
    caps every fresh lease below the 3600 s window: ``lease_capped_by_expiry``)."""
    later = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    world.env.execute(
        "UPDATE credentials SET expires_at=? WHERE principal_id=?", later, world.worker["id"]
    )


def _age_lease(world: SaladWorld, *, remaining_vs_window: int, waited: int = 3600) -> None:
    """Rewrite the attempt's lease: issued ``waited`` s ago, hard expiry ``window + delta`` s
    from now (the soft expiry equal to it, so the lease is current and only its window is
    short)."""
    _long_lived_credentials(world)
    now = datetime.now(UTC)
    hard = (now + timedelta(seconds=_window(world) + remaining_vs_window)).isoformat()
    world.env.execute(
        "UPDATE execution_leases SET issued_at=?, expires_at=?, hard_expires_at=? WHERE id=?",
        (now - timedelta(seconds=waited)).isoformat(),
        hard,
        hard,
        world.salad()["lease_id"],
    )


def _first(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=1",
        world.campaign_id,
    )


def _closed(world: SaladWorld) -> bool:
    return _first(world)["status"] in {"failed_transient", "failed_permanent"}


def _assert_requeued_free(world: SaladWorld) -> None:
    first = _first(world)
    assert first["status"] == "failed_transient", world.dump()
    assert first["failure_class"] == NOT_STARTED
    assert LEASE_WINDOW_SHORT_BEFORE_ROUTE in first["failure_detail"]
    assert first["settled_cents"] in (0, None)
    assert first["settlement_basis"] == "unclaimed_zero"
    assert [
        e for e in world.events(REQUEUE_EVENT) if e.get("reason") == LEASE_WINDOW_SHORT_BEFORE_ROUTE
    ]
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    assert world.status() == "running"


@pytest.mark.parametrize("phase", ["route_proposed", "route_started"])
def test_a_lease_that_waited_and_no_longer_covers_the_window_requeues_free(tmp_path, phase):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until_phase("route_proposed")
    if phase == "route_started":
        world.tick_until_phase("route_started")
    _age_lease(world, remaining_vs_window=-100)
    world.tick_until(lambda: _closed(world))
    _assert_requeued_free(world)
    # Control never saw a provider effect.
    assert world.salad(attempt=1)["provider_id"] is None


@pytest.mark.parametrize("phase", ["route_proposed", "route_started"])
def test_a_lease_whose_remaining_life_equals_the_window_is_not_requeued(tmp_path, phase):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until_phase("route_proposed")
    if phase == "route_started":
        world.tick_until_phase("route_started")
    # A generous margin so the real clock advancing during the ticks cannot make it short.
    _age_lease(world, remaining_vs_window=600)
    world.tick(3)
    assert _first(world)["status"] not in {"failed_transient", "failed_permanent"}
    assert not world.events(REQUEUE_EVENT)


def test_a_short_lease_that_barely_waited_is_not_requeued(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _age_lease(world, remaining_vs_window=-100, waited=60)
    world.tick(3)
    assert _first(world)["status"] not in {"failed_transient", "failed_permanent"}
    assert not world.events(REQUEUE_EVENT)


def test_repeats_of_the_short_window_past_the_bound_keep_t1_salad_unclaimed(tmp_path):
    world = SaladWorld(tmp_path, stages=lambda env: [_stage(env)], max_task_cents=1000)
    reasons = []
    for number in range(1, MAX_AUTO_REQUEUES + 2):
        if number == 1:
            world.to_awaiting_review()
        else:
            world.tick_until(lambda n=number: world.attempt()["attempt"] == n, limit=120)
            world.tick_until(lambda: world.salad() is not None and world.phase() is not None)
            world.tick_until_phase("awaiting_lease", limit=120)
            world.lease_and_package()
            # The carry-over of the first attempt's approvals may skip the review stop.
            world.tick_until(
                lambda: (
                    world.phase() in {"awaiting_review", "reviewed", "capability_ready"}
                    or world.phase() == "route_proposed"
                ),
                limit=120,
            )
        if world.phase() == "awaiting_review":
            world.resolve_decisions()
        world.tick_until_phase("route_proposed", limit=120)
        _age_lease(world, remaining_vs_window=-100)
        current = world.attempt()["id"]
        world.tick_until(
            lambda c=current: (
                world.row("SELECT status FROM campaign_stage_attempts WHERE id=?", c)["status"]
                in {"failed_transient", "failed_permanent"}
            ),
            limit=120,
        )
        reasons.append(
            world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", current)["failure_class"]
        )
        if reasons[-1] != NOT_STARTED:
            break
    assert len(reasons) == MAX_AUTO_REQUEUES + 1
    assert reasons[:MAX_AUTO_REQUEUES] == [NOT_STARTED] * MAX_AUTO_REQUEUES
    assert reasons[-1] == "t1_salad_unclaimed"


def _stage(env):
    from salad_campaign_fixture import salad_stage

    return salad_stage(env, attempts=MAX_AUTO_REQUEUES + 3)


# -- 3. executor --------------------------------------------------------------------------------


class BoundedControl(FakeControl):
    def __init__(self, *, bounds=None, capacity=None):
        super().__init__()
        self.bounds, self.capacity = bounds, capacity

    def lease_bounds(self, lease_id):
        self._c("lease_bounds", lease_id)
        if isinstance(self.bounds, BaseException):
            raise self.bounds
        return self.bounds

    def guardian_capacity(self, route_id):
        self._c("guardian_capacity", route_id)
        if isinstance(self.capacity, BaseException):
            raise self.capacity
        return self.capacity


# What the executor's lease must still cover: the route window, never more than a lease is
# issued for (the plan's duration plus the provisioning allowance).
NEED = needed_seconds(PROFILE.route_max_seconds, PLAN["max_duration_seconds"])


def _bounds(*, remaining, waited=3600):
    return NOW - timedelta(seconds=waited), NOW + timedelta(seconds=remaining)


def _harness(tmp_path, control):
    harness = Harness(tmp_path, control)
    harness.executor.wall_clock = lambda: NOW
    return harness


def _details(log):
    return [(a["action"], a["detail"]) for a in log]


def test_a_short_lease_defers_the_launch_with_nothing_marked(tmp_path):
    control = BoundedControl(bounds=_bounds(remaining=NEED - 10))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    log = h.executor.run_once()
    deferred = [d for a, d in _details(log) if a == "route_deferred"]
    assert len(deferred) == 1 and deferred[0].startswith("lease_window_short")
    assert "mark_executing" not in h.names() and h.runs == []


def test_the_executor_margin_defers_before_control_would_requeue(tmp_path):
    # Remaining life covers the window by less than the margin: control would not requeue yet,
    # the executor already defers.
    control = BoundedControl(bounds=_bounds(remaining=NEED + EXECUTOR_WINDOW_MARGIN_SECONDS - 5))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    log = h.executor.run_once()
    assert any(a == "route_deferred" for a, _ in _details(log)) and h.runs == []


def test_a_lease_that_covers_the_window_launches(tmp_path):
    control = BoundedControl(bounds=_bounds(remaining=NEED + EXECUTOR_WINDOW_MARGIN_SECONDS + 5))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert "mark_executing" in h.names() and len(h.runs) == 1


def test_a_short_lease_that_barely_waited_launches(tmp_path):
    control = BoundedControl(bounds=_bounds(remaining=10, waited=30))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert len(h.runs) == 1


def test_an_unreadable_lease_defers(tmp_path):
    control = BoundedControl(bounds=LookupError("lease unavailable"))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    log = h.executor.run_once()
    assert ("route_deferred", "lease_unreadable:LookupError") in _details(log)
    assert "mark_executing" not in h.names() and h.runs == []


def test_a_control_port_without_the_reads_launches_as_before(tmp_path):
    h = _harness(tmp_path, FakeControl())
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert len(h.runs) == 1


def test_a_full_guardian_defers_then_launches_when_capacity_frees(tmp_path):
    control = BoundedControl(bounds=_bounds(remaining=10**6), capacity=(1, 1))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    log = h.executor.run_once()
    assert ("route_deferred", "guardian_frame_limit:1/1") in _details(log)
    assert "mark_executing" not in h.names() and h.runs == []
    assert control.rejections == []
    control.capacity = (0, 1)
    h.executor.run_once()
    assert "mark_executing" in h.names() and len(h.runs) == 1


def test_an_unreadable_guardian_capacity_defers(tmp_path):
    control = BoundedControl(bounds=_bounds(remaining=10**6), capacity=ValueError("down"))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    log = h.executor.run_once()
    assert ("route_deferred", "guardian_capacity_unreadable:ValueError") in _details(log)
    assert "mark_executing" not in h.names() and h.runs == []


def test_no_guardian_answer_launches(tmp_path):
    control = BoundedControl(bounds=_bounds(remaining=10**6), capacity=None)
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert len(h.runs) == 1


class ConcurrentBounded(ConcurrentControl):
    def __init__(self, *, bounds=None, capacity=None):
        super().__init__(limit=4)
        self.bounds, self.capacity = bounds, capacity

    def lease_bounds(self, lease_id):
        if isinstance(self.bounds, BaseException):
            raise self.bounds
        return self.bounds

    def guardian_capacity(self, route_id):
        if isinstance(self.capacity, BaseException):
            raise self.capacity
        return self.capacity


def test_concurrent_mode_defers_a_short_lease_and_a_full_guardian(tmp_path):
    control = ConcurrentBounded(bounds=_bounds(remaining=1), capacity=(1, 1))
    control.items = started((1,))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    executor.wall_clock = lambda: NOW
    log = executor.run_once()
    assert actions(log, "att_1")[0][0] == "route_deferred"
    assert actions(log, "att_1")[0][1].startswith("lease_window_short")
    assert ("mark_executing", "att_1") not in control.calls and runner.threads == {}
    control.bounds = _bounds(remaining=10**6)
    log = executor.run_once()
    assert ("route_deferred", "guardian_frame_limit:1/1") in actions(log, "att_1")
    assert ("mark_executing", "att_1") not in control.calls and runner.threads == {}
    control.capacity = ValueError("down")
    log = executor.run_once()
    assert ("route_deferred", "guardian_capacity_unreadable:ValueError") in actions(log, "att_1")
    control.capacity = (0, 1)
    log = executor.run_once()
    runner.wait_started("route_1")
    assert ("route_launched", "") in actions(log, "att_1")
    runner.finish(executor, "route_1")
    executor.run_once()


# -- 4. safety service open_frames --------------------------------------------------------------

pytest.importorskip("cryptography")
from test_salad_safety_concurrent_frames import _body, _reserve, _service  # noqa: E402


def test_open_frames_reports_the_count_and_limit_without_writes(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2)
    try:
        asking = _body("c" * 32, clock[0])["reservation"]
        ask = dict(operation="open_frames", reservation=asking)
        assert service.admin(ask)["open_billable_frames"] == 0
        _reserve(service, clock, "a" * 32)
        before = service.db.execute("SELECT count(*) FROM reservations").fetchone()[0]
        answer = service.admin(ask)
        assert answer["open_billable_frames"] == 1
        assert answer["max_open_billable_frames"] == 2
        _reserve(service, clock, "b" * 32)
        assert service.admin(ask)["open_billable_frames"] == 2
        assert service.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == before + 1
    finally:
        service.close()


@pytest.mark.parametrize("bad", [None, "", "z" * 64, "a" * 63, "A" * 64, 5])
def test_open_frames_rejects_an_invalid_reservation(tmp_path, bad):
    service, _clock, _ = _service(tmp_path)
    try:
        with pytest.raises(ValueError, match="invalid_admin_request"):
            service.admin(dict(operation="open_frames", reservation=bad))
    finally:
        service.close()


# -- 5. the pre-create fence race ---------------------------------------------------------------


def _refusing(code):
    def dispatch(action):
        raise GuardianRefused(code)

    return dispatch


def _drive(code):
    receipt: dict = {}
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    with pytest.raises(GuardianRefused):
        pc.dispatch_create(
            _refusing(code),
            lambda: {},
            clock=lambda: clock[0],
            sleep=sleep,
            deadline=10**6,
            receipt=receipt,
        )
    return receipt


def test_a_transient_final_refusal_is_recorded_on_the_receipt():
    assert FRAME_FENCE_REFUSAL in pc.TRANSIENT_RESERVE_REFUSALS
    receipt = _drive(FRAME_FENCE_REFUSAL)
    assert receipt["reason"] == pc.CREATE_NOT_ATTEMPTED
    assert receipt["create_preflight_refusal"] == FRAME_FENCE_REFUSAL


def test_a_non_transient_refusal_leaves_the_key_absent():
    receipt = _drive("budget_exhausted")
    assert receipt["reason"] == pc.CREATE_NOT_ATTEMPTED
    assert "create_preflight_refusal" not in receipt


PRE_CREATE_RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": pc.CREATE_NOT_ATTEMPTED,
    "stop_confirmed": False,
    "outputs": [],
}


def _record_never_sent(world: SaladWorld, refusal: str | None) -> None:
    """The executor's honest outcome of a run that ended before any create was sent."""
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    receipt = dict(PRE_CREATE_RECEIPT)
    if refusal:
        receipt["create_preflight_refusal"] = refusal
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=receipt,
            manifest_id=None,
            provider_id=None,
            estimated_exposure={"estimated_exposure_cents": 0},
        ),
    )


def test_a_never_sent_receipt_with_a_full_fence_requeues_free(tmp_path):
    world = SaladWorld(tmp_path)
    _record_never_sent(world, FRAME_FENCE_REFUSAL)
    world.tick_until(lambda: _closed(world))
    first = _first(world)
    assert first["status"] == "failed_transient", world.dump()
    assert first["failure_class"] == NOT_STARTED
    assert FRAME_FENCE_BEFORE_CREATE in first["failure_detail"]
    assert first["settled_cents"] == 0
    assert infra_attempts(world.env.query("SELECT * FROM campaign_stage_attempts")) == 0
    world.tick_until(lambda: world.attempt()["attempt"] == 2)


def test_a_never_sent_receipt_without_the_refusal_is_the_ordinary_unclaimed_unwind(tmp_path):
    world = SaladWorld(tmp_path)
    _record_never_sent(world, None)
    world.tick_until(lambda: _closed(world))
    first = _first(world)
    assert first["failure_class"] != NOT_STARTED
    assert FRAME_FENCE_BEFORE_CREATE not in first["failure_detail"]
