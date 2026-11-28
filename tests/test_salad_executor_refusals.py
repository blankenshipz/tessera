"""ADR 0212 addendum: the executor reports its refusals, trips a persisted step breaker, and its
health file counts what it used to swallow. Fakes only (no subprocess, no network, no Salad).

* An attempt for a profile the executor does not serve, or whose plan budget is above the
  profile's cap, is reported to control as a typed refusal; control unwinds it at once on its
  own no-claim proof and retries it within the stage's budget, never by waiting out the grant.
* An attempt whose pre-launch step fails ``STEP_BREAKER_THRESHOLD`` passes in a row (across
  restarts) is no longer tried and is reported as ``step_breaker_open``.
* ``control.work()`` failures and per-item errors are counted for the health file.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_executor import PLAN, PROFILE, WORKER, FakeControl, config_for, item

from tessera_control import campaign_salad_executor as module
from tessera_control.campaign_salad_commands import (
    EXECUTOR_REFUSED_EVENT,
    EXECUTOR_REFUSED_PREFIX,
)
from tessera_control.campaign_salad_executor import (
    STEP_BREAKER_FILE,
    STEP_BREAKER_THRESHOLD,
    SaladCampaignExecutor,
    StoreControl,
)
from tessera_control.store import Conflict, StoreError
from tessera_worker.service_loop import run_service


class RefusalControl(FakeControl):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.refusals = []
        self.refusal_error = None
        self.work_error = None

    def record_refusal(self, attempt_id, code, step):
        self._c("record_refusal", attempt_id, code, step)
        if self.refusal_error:
            raise self.refusal_error
        self.refusals.append((attempt_id, code, step))

    def work(self):
        if self.work_error:
            raise self.work_error
        return super().work()


def _executor(tmp_path, control, config=None):
    return SaladCampaignExecutor(
        config or config_for(tmp_path),
        PROFILE,
        control,
        worker_principal_id=WORKER,
        route_runner=lambda *a: None,
        importer=lambda *a: [],
        sleep=lambda _s: None,
    )


def _lease_item(**over):
    return item("awaiting_lease", route_id=None, operation_id=None, **over)


# -- the executor reports what it refuses ---------------------------------------------------


@pytest.mark.parametrize(
    "over, plan, code",
    [
        pytest.param({"profile": "other"}, None, "different_profile", id="profile"),
        pytest.param({}, dict(PLAN, budget_cents=10**6), "plan_budget_above_profile", id="budget"),
    ],
)
def test_a_refusal_is_reported_once_as_a_typed_code(tmp_path, over, plan, code):
    control = RefusalControl(plan=plan)
    control.items = [_lease_item(**over)]
    executor = _executor(tmp_path, control)
    executor.run_once()
    executor.run_once()
    assert control.refusals == [("att_1", code, "grant_poll")]
    assert "acquire" not in [call[0] for call in control.calls]
    assert executor.health()["refusals_reported_total"] == 1


def test_a_failed_refusal_report_is_counted_and_sent_again(tmp_path):
    control = RefusalControl()
    control.items = [_lease_item(profile="other")]
    control.refusal_error = Conflict("busy")
    executor = _executor(tmp_path, control)
    notes = executor.run_once()
    assert ("refusal_report_failed", "Conflict") in [(n["action"], n["detail"]) for n in notes]
    assert executor.health()["refusal_report_failures_total"] == 1
    control.refusal_error = None
    executor.run_once()
    assert control.refusals == [("att_1", "different_profile", "grant_poll")]


# -- the step breaker -------------------------------------------------------------------------


def test_a_failing_pre_launch_step_trips_the_breaker_and_is_reported(tmp_path):
    control = RefusalControl()
    control.items = [_lease_item()]
    control.package_error = RuntimeError("control down")
    executor = _executor(tmp_path, control)
    for _ in range(STEP_BREAKER_THRESHOLD):
        executor.run_once()
    packages = [call for call in control.calls if call[0] == "package"]
    assert len(packages) == STEP_BREAKER_THRESHOLD and control.refusals == []
    notes = executor.run_once()
    assert [n["action"] for n in notes if n["attempt_id"] == "att_1"][:1] == ["breaker_open"]
    assert control.refusals == [("att_1", "step_breaker_open", "package")]
    # Not tried again while open.
    executor.run_once()
    assert len([call for call in control.calls if call[0] == "package"]) == STEP_BREAKER_THRESHOLD
    health = executor.health()
    assert health["breaker_open_attempts"] == 1
    assert health["longest_failure_streak"] == STEP_BREAKER_THRESHOLD
    assert health["item_errors_total"] == STEP_BREAKER_THRESHOLD
    assert health["last_item_error_class"] == "RuntimeError"


def test_the_streak_survives_a_restart(tmp_path):
    control = RefusalControl()
    control.items = [_lease_item()]
    control.package_error = RuntimeError("control down")
    config = config_for(tmp_path)
    executor = _executor(tmp_path, control, config)
    for _ in range(STEP_BREAKER_THRESHOLD - 1):
        executor.run_once()
    saved = json.loads((tmp_path / "work" / STEP_BREAKER_FILE).read_text())
    assert saved["streaks"]["att_1"] == {"step": "package", "count": STEP_BREAKER_THRESHOLD - 1}
    restarted = _executor(tmp_path, control, config)  # a crash loop keeps counting
    restarted.run_once()
    restarted.run_once()
    assert control.refusals == [("att_1", "step_breaker_open", "package")]


def test_a_success_ends_the_streak_and_an_unlisted_attempt_drops_it(tmp_path):
    control = RefusalControl()
    control.items = [_lease_item()]
    control.package_error = RuntimeError("control down")
    executor = _executor(tmp_path, control)
    for _ in range(STEP_BREAKER_THRESHOLD - 1):
        executor.run_once()
    control.package_error = None
    executor.run_once()
    assert executor.health()["longest_failure_streak"] == 0
    control.package_error = RuntimeError("again")
    executor.run_once()
    control.items = []
    executor.run_once()
    saved = json.loads((tmp_path / "work" / STEP_BREAKER_FILE).read_text())
    assert saved == {"streaks": {}}


def test_an_unreadable_breaker_file_starts_empty(tmp_path):
    config = config_for(tmp_path)
    (tmp_path / "work" / STEP_BREAKER_FILE).write_text("{not json")
    executor = _executor(tmp_path, RefusalControl(), config)
    assert executor.health()["longest_failure_streak"] == 0


def test_errors_after_the_launch_never_trip_the_breaker(tmp_path):
    """A renewal error of a lease-held attempt is counted but never refused: the lease must be
    kept for the runner's own release."""
    control = RefusalControl()
    control.items = [item("leased", route_id=None)]
    control.renew_error = RuntimeError("flaky")
    executor = _executor(tmp_path, control)
    for _ in range(STEP_BREAKER_THRESHOLD + 2):
        executor.run_once()
    assert control.refusals == []
    assert executor.health()["breaker_open_attempts"] == 0


# -- health -----------------------------------------------------------------------------------


def test_work_unavailable_is_counted_for_the_health_file(tmp_path):
    control = RefusalControl()
    control.work_error = OSError("database is locked")
    executor = _executor(tmp_path, control)
    executor.run_once()
    executor.run_once()
    health = executor.health()
    assert health["work_unavailable_total"] == 2
    assert health["work_unavailable_consecutive"] == 2
    assert health["last_work_error_class"] == "OSError"
    control.work_error = None
    executor.run_once()
    assert executor.health()["work_unavailable_consecutive"] == 0
    assert executor.health()["work_unavailable_total"] == 2


def test_the_service_health_file_carries_the_executors_counters(tmp_path):
    control = RefusalControl()
    control.work_error = OSError("database is locked")
    executor = _executor(tmp_path, control)
    path = tmp_path / "health.json"
    run_service(
        step=executor.run_once,
        interval=0,
        once=False,
        max_cycles=2,
        health_file=path,
        name="campaign-salad-executor",
        health_extra=executor.health,
        wait=lambda _s: False,
    )
    document = json.loads(path.read_text())
    assert document["service"]["work_unavailable_total"] == 2
    assert document["consecutive_errors"] == 0  # the step itself never raised


def test_a_failing_health_callback_never_stops_the_health_file(tmp_path):
    path = tmp_path / "health.json"

    def broken():
        raise RuntimeError("boom")

    run_service(
        step=lambda: [],
        interval=0,
        once=True,
        health_file=path,
        name="x",
        health_extra=broken,
    )
    assert json.loads(path.read_text())["service_error_class"] == "RuntimeError"


def test_main_passes_the_executor_health_to_the_service_loop(monkeypatch, tmp_path):
    seen = {}

    class Built:
        def run_once(self):
            return []

        def health(self):
            return {"ok": True}

    monkeypatch.setattr(module, "load_config", lambda path: config_for(tmp_path))
    monkeypatch.setattr(module, "build_executor", lambda config: Built())
    monkeypatch.setattr(module.maintenance_lock, "hold_shared_for_process", lambda *_: None)

    def fake_run_service(**kwargs):
        seen.update(kwargs)
        return 0

    import tessera_worker.service_loop as loop

    monkeypatch.setattr(loop, "run_service", fake_run_service)
    assert module.main(["--config", str(tmp_path / "c.json"), "--once"]) == 0
    assert seen["health_extra"]() == {"ok": True}


# -- control unwinds a refused attempt at once ------------------------------------------------


def _awaiting_lease(tmp_path, attempts: int = 2) -> SaladWorld:
    world = SaladWorld(
        tmp_path,
        stages=lambda env: [salad_stage(env, retry={"max_attempts": attempts, "infra_retries": 0})],
    )
    world.start()
    world.tick_until(lambda: world.row("SELECT 1 FROM campaign_salad_stages") is not None)
    world.tick_until(
        lambda: (
            world.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
            is not None
        )
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    return world


@pytest.mark.parametrize("code", ["different_profile", "plan_budget_above_profile"])
def test_control_unwinds_a_refused_attempt_and_retries_it(tmp_path, code):
    world = _awaiting_lease(tmp_path)
    row = world.salad()
    control = StoreControl(world.env.state, world.worker)
    control.record_refusal(row["attempt_id"], code, "grant_poll")
    assert world.salad()["waiting_reason"] == EXECUTOR_REFUSED_PREFIX + code
    assert world.events(EXECUTOR_REFUSED_EVENT)[-1]["code"] == code
    world.tick_until(
        lambda: len(world.env.query("SELECT * FROM campaign_stage_attempts")) >= 2, limit=20
    )
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == "t1_salad_unclaimed"
    assert first["failure_detail"] == EXECUTOR_REFUSED_PREFIX + code
    assert first["settled_cents"] == 0
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", first["grant_id"])
    assert grant["status"] in ("revoked", "reconciled")
    # A replayed report is idempotent and never refuses the retry.
    control = StoreControl(world.env.state, world.worker)
    control.record_refusal(row["attempt_id"], code, "grant_poll")


def test_a_refusal_with_no_attempt_left_fails_the_stage_without_waiting(tmp_path):
    world = _awaiting_lease(tmp_path, attempts=1)
    row = world.salad()
    StoreControl(world.env.state, world.worker).record_refusal(
        row["attempt_id"], "different_profile", "grant_poll"
    )
    world.tick_until(lambda: world.status() in ("failed", "stopped"), limit=20)
    assert world.attempt()["settled_cents"] == 0


def test_control_refuses_a_refusal_it_cannot_honour(tmp_path):
    world = _awaiting_lease(tmp_path)
    row = world.salad()
    control = StoreControl(world.env.state, world.worker)
    with pytest.raises(StoreError):
        control.record_refusal(row["attempt_id"], "made_up", "grant_poll")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    with pytest.raises(Conflict, match="past the point"):
        control.record_refusal(row["attempt_id"], "different_profile", "grant_poll")
    assert world.salad()["waiting_reason"] is None or not str(
        world.salad()["waiting_reason"]
    ).startswith(EXECUTOR_REFUSED_PREFIX)
