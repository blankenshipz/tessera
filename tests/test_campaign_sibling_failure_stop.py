"""M3: a sibling's failure stops scientific work, not another stage's 0 cent infrastructure
retries, and no queued route that was never sent anything launches after the stop."""

from __future__ import annotations

import sqlite3

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_stage,
)

from tessera_control.campaign_stage_failure_stop import (
    HELD_CAMPAIGN_STATUS,
    attempt_held,
    is_stage_failure_stop,
    keeps_retry,
)

SIBLING_STOP = "stage_failed:other:t1_salad_unclaimed_exhausted"


def stages(env):
    return [salad_stage(env, "g1", retry={"max_attempts": 1, "infra_retries": 2})]


def _sql(world: SaladWorld, sql: str, *args) -> None:
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3", timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(sql, args)
        connection.commit()
    finally:
        connection.close()


def _stop(world: SaladWorld, reason: str) -> None:
    _sql(world, "UPDATE campaigns SET stop_reason=? WHERE id=?", reason, world.campaign_id)


def _fail_first(world: SaladWorld, failure_class: str) -> None:
    first = world.attempt()
    _sql(
        world,
        "UPDATE campaign_stage_attempts SET status='failed_transient', failure_class=?, "
        "settled_cents=0, settlement_basis='unclaimed_zero' WHERE id=?",
        failure_class,
        first["id"],
    )
    _sql(
        world,
        "UPDATE campaign_salad_attempts SET phase='unclaimed' WHERE attempt_id=?",
        first["id"],
    )
    _sql(world, "UPDATE campaigns SET held_cents=0 WHERE id=?", world.campaign_id)


def _stage_status(world: SaladWorld) -> str:
    return world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )["status"]


def test_classes_that_keep_their_retry():
    for kept in (
        "infra_salad_safety_stop",
        "infra_salad_create_not_sent",
        "infra_container_killed",
        "provider_reallocated_before_custody",
        "prestart_no_spend",
    ):
        assert keeps_retry(kept), kept
    for ended in ("t1_salad_unclaimed", "not_started", "undeclared_exit_code", None):
        assert not keeps_retry(ended), ended


def test_only_a_stage_failure_is_a_sibling_stop():
    assert is_stage_failure_stop(SIBLING_STOP)
    for other in (None, "", "cancel:operator", "budget_overrun:1", "stop_rule:g1.loss"):
        assert not is_stage_failure_stop(other)


def test_an_infra_retry_survives_a_siblings_failure(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _fail_first(world, "infra_salad_safety_stop")
    _stop(world, SIBLING_STOP)
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    assert _stage_status(world) != "failed", world.dump()
    assert world.events("campaign.stage_infra_retry")


def test_a_scientific_failure_still_ends_at_a_siblings_failure(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _fail_first(world, "t1_salad_unclaimed")
    _stop(world, SIBLING_STOP)
    world.tick_until(lambda: _stage_status(world) == "failed")
    assert world.attempt()["attempt"] == 1
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert stop["stop_reason"] == SIBLING_STOP


def test_a_never_started_attempt_ends_as_not_started_not_exhausted(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _fail_first(world, "not_started")
    _stop(world, SIBLING_STOP)
    world.tick_until(lambda: _stage_status(world) == "failed")
    assert world.attempt()["attempt"] == 1
    # The stage is the sibling's stop reason's first failure only if nothing else set it; the
    # report names the honest class, not "exhausted" (checked on the event timeline's stage).
    assert world.attempt()["failure_class"] == "not_started"


@pytest.mark.parametrize("reason", ["cancel:operator", "budget_overrun:1"])
def test_other_stops_keep_ending_retries(tmp_path, reason):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _fail_first(world, "infra_salad_safety_stop")
    _stop(world, reason)
    world.tick(6)
    assert world.attempt()["attempt"] == 1, world.dump()


def test_a_queued_route_is_listed_held_and_released_at_zero(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    (listed,) = [w for w in world.env.store.list_salad_work(world.worker)]
    assert listed["campaign_status"] == "running"
    _stop(world, SIBLING_STOP)
    (listed,) = [w for w in world.env.store.list_salad_work(world.worker)]
    assert listed["campaign_status"] == HELD_CAMPAIGN_STATUS
    world.tick_until(lambda: world.attempt()["failure_class"] is not None)
    attempt = world.attempt()
    assert attempt["failure_class"] == "not_started", world.dump()
    assert attempt["settled_cents"] == 0
    assert "sibling stage failed" in (attempt["failure_detail"] or "")


def test_an_executing_route_is_never_held(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _sql(
        world,
        "UPDATE campaign_salad_attempts SET phase='executing' WHERE attempt_id=?",
        world.attempt()["id"],
    )
    _stop(world, SIBLING_STOP)
    (listed,) = [w for w in world.env.store.list_salad_work(world.worker)]
    assert listed["campaign_status"] == "running"


def test_a_cancel_does_not_hold_a_queued_route_here(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _stop(world, "cancel:operator")
    with world.runner.store.runner_connection() as connection:
        assert attempt_held(connection, world.attempt()["id"]) is False


def test_an_honoured_retry_still_launches_after_the_stop(tmp_path):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    current = world.attempt()
    # Make the live attempt the second of the stage, after a 0 cent infrastructure failure.
    _sql(world, "UPDATE campaign_stage_attempts SET attempt=2 WHERE id=?", current["id"])
    _sql(
        world,
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, settled_cents, failure_class, revision, created_at, updated_at, provider_id) "
        "VALUES ('first', ?, 'g1', 1, 'failed_transient', 0, 0, 'infra_salad_safety_stop', 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'salad')",
        world.campaign_id,
    )
    _stop(world, SIBLING_STOP)
    with world.runner.store.runner_connection() as connection:
        assert attempt_held(connection, current["id"]) is False
    (listed,) = [w for w in world.env.store.list_salad_work(world.worker)]
    assert listed["campaign_status"] == "running"


def test_a_class_fallback_keeps_its_own_bound_under_a_siblings_failure(tmp_path):
    """A pre-start move keeps ``retry.max_class_fallbacks`` while the campaign is stopping on a
    sibling's failure: with a third class still listed but the bound of one move used, the
    stage fails as exhausted and is never placed again."""
    from test_campaign_class_fallback import _attempts, _prestart_failure, _to_next_route
    from test_campaign_class_fallback import _world as fallback_world

    world = fallback_world(
        tmp_path, alternates=("gpu-alt", "gpu-third"), retry={"max_class_fallbacks": 1}
    )
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    _to_next_route(world)
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    _stop(world, SIBLING_STOP)
    world.record_charge(0)
    world.tick_until(lambda: _stage_status(world) == "failed")
    world.tick(4)
    assert len(_attempts(world)) == 2, world.dump()
    assert _attempts(world)[-1]["failure_class"] == "prestart_no_spend"
