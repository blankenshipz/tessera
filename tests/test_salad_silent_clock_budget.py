"""An executing attempt whose lease is renewed is not silent inside the executor's budget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from salad_campaign_fixture import SaladWorld

from tessera_control.campaign_salad import (
    EXECUTING_RENEWED_GRACE_SECONDS,
    SILENT_EXECUTOR_GRACE_SECONDS,
)


def _executing(world: SaladWorld) -> int:
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    row = world.salad()
    assert row["phase"] == "executing"
    return world.route_state()["proposal"]["selection"]["resources"]["max_seconds"]


def _at(world: SaladWorld, seconds: float, *, renewed: bool) -> None:
    start = datetime.fromisoformat(world.salad()["updated_at"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    now = start + timedelta(seconds=seconds)
    world.runner.now = lambda: now
    if renewed:
        world.env.execute(
            "UPDATE execution_leases SET renewed_at=?, expires_at=? WHERE id=?",
            (now - timedelta(seconds=20)).isoformat(),
            (now + timedelta(minutes=10)).isoformat(),
            world.salad()["lease_id"],
        )


def _settle_ticks(world: SaladWorld) -> None:
    world.tick(3)


def test_renewed_executing_attempt_is_not_unknown_past_window_plus_grace(tmp_path):
    world = SaladWorld(tmp_path)
    window = _executing(world)
    _at(world, window + SILENT_EXECUTOR_GRACE_SECONDS + 700, renewed=True)
    _settle_ticks(world)
    assert world.salad()["phase"] == "executing"
    assert world.status() == "running"


def test_unrenewed_executing_attempt_is_unknown_at_window_plus_grace(tmp_path):
    world = SaladWorld(tmp_path)
    window = _executing(world)
    _at(world, window + SILENT_EXECUTOR_GRACE_SECONDS + 1, renewed=False)
    _settle_ticks(world)
    assert world.salad()["phase"] == "unknown"


def test_renewals_past_the_cap_still_go_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    window = _executing(world)
    _at(world, window + EXECUTING_RENEWED_GRACE_SECONDS + 1, renewed=True)
    _settle_ticks(world)
    assert world.salad()["phase"] == "unknown"
