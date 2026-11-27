"""Runner and Salad driver follow-ups on the v0.104.0 evidence pass.

* A refused reduce-only step inside the evidence pass fails that attempt (the stage loop's
  attempt-failure path); it never escapes to the tick and wedges every later pass.
* The wait for a termination-confirmed route's import is bounded: one more route window plus
  grace, then ``unknown`` for the silent reason, still late-importable.
* A never-sent requeue is bounded by ADR 0046's identical-repeat limit.

Real stores and runner (``SaladWorld``) on synthetic state; no provider, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control.campaign_autoreconcile import MAX_AUTO_REQUEUES, repeat_count
from tessera_control.campaign_salad import SILENT_NEVER_SENT
from tessera_control.campaign_salad_commands import SILENT_EXECUTOR, late_importable
from tessera_control.campaign_steps import StepFailed

PRE_CREATE_RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": "create_not_attempted",
    "stop_confirmed": False,
    "outputs": [],
}
CONTENT = b'{"score": 1}'


def _later(world: SaladWorld, hours: float) -> None:
    world.runner.now = lambda: datetime.now(UTC) + timedelta(hours=hours)


def _executing(world: SaladWorld) -> None:
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))


def _record(world: SaladWorld, receipt: dict[str, Any]) -> None:
    row = world.salad()
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


def _attempt_one(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=1",
        world.campaign_id,
    )


# -- F-1: a refused unwind step in the evidence pass ----------------------------------------


def test_refused_unwind_in_the_evidence_pass_fails_the_attempt_not_the_tick(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    _executing(world)
    _record(world, PRE_CREATE_RECEIPT)
    driver_type = type(world.driver)

    def sticky_refusal(self, *args, **kwargs):
        # A failed reduce-only intent replays as the same refusal on every tick.
        raise StepFailed("cancel_route", "step_refused", "revision race")

    monkeypatch.setattr(driver_type, "_cancel_route", sticky_refusal)

    report = world.runner.tick()

    assert not [action for action in report.actions if action.startswith("error:")]
    first = _attempt_one(world)
    assert first["status"] == "failed_permanent"
    assert first["failure_class"] == "step_refused"
    assert "revision race" in first["failure_detail"]
    # Later passes run their stage loop (no tick-level error), and the stage ends as the
    # stage loop's own attempt-failure path would leave it.
    for _ in range(3):
        later = world.runner.tick()
        assert not [action for action in later.actions if action.startswith("error:")]
    stage = world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )
    assert stage["status"] == "failed"


def test_an_escaping_hook_refusal_ends_the_hook_not_the_pass(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    driver_type = type(world.driver)

    def refusing(self, ctx):
        raise StepFailed("cancel_route", "step_refused", "sticky")

    monkeypatch.setattr(driver_type, "evidence_pass", refusing)

    report = world.runner.tick()

    assert not [action for action in report.actions if action.startswith("error:")]
    assert any(action.startswith("evidence_failed:") for action in report.actions)
    assert world.status() == "running"


# -- F-2: the import wait for a termination-confirmed route is bounded ----------------------


def _ran_and_stopped(world: SaladWorld) -> None:
    """A route that ran and stopped with provider-confirmed termination, whose receipt the
    executor never imports (its route directory is gone)."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    world.upload_manifest(CONTENT)
    world.terminate_route()
    world.observe_exit()


def _window_seconds(world: SaladWorld) -> int:
    return world.route_state()["proposal"]["selection"]["resources"]["max_seconds"]


def test_import_wait_is_bounded_then_unknown_and_still_late_importable(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _ran_and_stopped(world)
    window_hours = (_window_seconds(world) + 600) / 3600
    start = 2 * window_hours + 0.5  # past the silent window
    _later(world, start)

    world.tick(2)
    assert world.attempt()["status"] == "running"
    assert len(world.events("campaign.salad_import_awaited")) == 1

    # Still inside one more window plus grace from the first wait: still waiting.
    _later(world, start + window_hours - 0.1)
    world.tick(2)
    assert world.attempt()["status"] == "running"
    assert world.status() == "running"

    # Past it: unknown for the silent reason, a person is asked, the late import stays open.
    _later(world, start + window_hours + 0.1)
    world.tick(2)
    attempt = world.attempt()
    assert attempt["status"] == "unknown"
    assert attempt["failure_detail"] == SILENT_EXECUTOR
    assert attempt["settled_cents"] is None
    assert world.salad()["waiting_reason"] == SILENT_EXECUTOR
    assert world.status() == "blocked"
    listed = {item["attempt_id"]: item for item in world.env.store.list_salad_work(world.worker)}
    item = listed[attempt["id"]]
    assert item["phase"] == "unknown"
    gate = world.row(
        """SELECT sa.phase, sa.waiting_reason, a.status AS attempt_status,
        a.settled_cents AS attempt_settled_cents, g.status AS grant_status
        FROM campaign_salad_attempts sa JOIN campaign_stage_attempts a ON a.id=sa.attempt_id
        JOIN execution_grants g ON g.id=a.grant_id WHERE sa.attempt_id=?""",
        attempt["id"],
    )
    assert late_importable(gate)
    # Nothing was settled or reconciled without evidence.
    assert world.events("execution.grant_reconciled_unclaimed") == []


def test_unterminated_silent_route_still_goes_unknown_at_the_first_window(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    _later(world, (_window_seconds(world) + 600) / 3600 * 2 + 0.5)

    world.tick(2)

    assert world.attempt()["status"] == "unknown"
    assert world.events("campaign.salad_import_awaited") == []


# -- F-3: never-sent requeues honor the identical-repeat bound ------------------------------


def test_a_real_never_sent_release_is_counted_as_an_identical_repeat(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _later(world, 3)

    world.tick_until(lambda: _attempt_one(world)["status"] == "failed_transient", limit=10)

    first = dict(_attempt_one(world))
    assert first["failure_class"] == "not_started"
    assert f"({SILENT_NEVER_SENT})" in first["failure_detail"]
    assert repeat_count([first], SILENT_NEVER_SENT) == 1
    assert world.events("campaign.autoreconcile_stopped") == []


def test_never_sent_release_past_the_repeat_bound_is_not_requeued_for_free(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    # Two identical never-sent releases came before this attempt (the ADR 0046 bound).
    prior = [
        {
            "id": f"prior-{index}",
            "attempt": 0,
            "status": "failed_transient",
            "failure_class": "not_started",
            "failure_detail": f"{SILENT_NEVER_SENT}: released at 0 ({SILENT_NEVER_SENT})",
        }
        for index in range(MAX_AUTO_REQUEUES)
    ]
    # Seen only by the release's own repeat count, not by the stage loop's retry counts.
    real_attempts = world.runner._attempts
    driver_type = type(world.driver)
    real_abandon = driver_type._abandon

    def abandon_with_history(self, *args, **kwargs):
        self.runner._attempts = lambda ctx, key: [*prior, *real_attempts(ctx, key)]
        try:
            return real_abandon(self, *args, **kwargs)
        finally:
            del self.runner._attempts

    monkeypatch.setattr(driver_type, "_abandon", abandon_with_history)
    _later(world, 3)

    world.tick_until(lambda: _attempt_one(world)["status"] == "failed_transient", limit=10)

    first = _attempt_one(world)
    # The release at 0 stands (nothing was sent) ...
    assert first["settled_cents"] == 0 and first["settlement_basis"] == "unclaimed_zero"
    # ... but it is no longer a free not-started requeue.
    assert first["failure_class"] == "t1_salad_unclaimed"
    assert f"repeated {MAX_AUTO_REQUEUES + 1} times" in first["failure_detail"]
    (stopped,) = world.events("campaign.autoreconcile_stopped")
    assert stopped["not_started_reason"] == SILENT_NEVER_SENT
    # The stage's own scientific bound (one attempt) ends it; no attempt 2 is queued.
    world.tick_until(
        lambda: (
            world.row(
                "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
                world.campaign_id,
            )["status"]
            == "failed"
        ),
        limit=8,
    )
    assert (
        world.row(
            "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=2",
            world.campaign_id,
        )
        is None
    )
