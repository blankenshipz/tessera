"""Pre-route leases of a blocked or paused campaign are listed, for keepalive only (N5).

Real store through the SaladWorld fixture; the executor's renewal is replayed with the same
store call it makes. Nothing is launched, granted or leased while the campaign is held."""

from __future__ import annotations

import pytest
from salad_campaign_fixture import SaladWorld

from tessera_control.campaign_salad_executor import LEASE_HELD_PHASES
from tessera_control.execution import ExecutionStore
from tessera_control.store import Conflict

HELD = ["blocked", "paused_delegation_lapsed"]


def _hold(world: SaladWorld, status: str) -> None:
    world.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, world.campaign_id)


def _set_phase(world: SaladWorld, phase: str) -> None:
    world.env.execute(
        "UPDATE campaign_salad_attempts SET phase=? WHERE attempt_id=?",
        phase,
        world.salad()["attempt_id"],
    )


def _listed(world: SaladWorld) -> dict:
    return {i["attempt_id"]: i for i in world.env.store.list_salad_work(world.worker)}


def _renew(world: SaladWorld) -> dict:
    lease = world.lease
    _, renewed = ExecutionStore(world.env.state).renew_execution_lease(
        world.worker, lease["id"], world.key("renew"), world.key("renew"), lease["epoch"]
    )
    return renewed


@pytest.mark.parametrize("status", HELD)
@pytest.mark.parametrize("phase", sorted(LEASE_HELD_PHASES))
def test_held_campaign_lists_pre_route_leases_and_they_stay_renewable(tmp_path, status, phase):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    _set_phase(world, phase)
    attempt_id = world.salad()["attempt_id"]
    _hold(world, status)

    item = _listed(world)[attempt_id]
    assert item["phase"] == phase and item["campaign_status"] == status

    renewed = _renew(world)
    assert renewed["epoch"] == world.lease["epoch"]
    _hold(world, "running")
    row = world.env.query("SELECT * FROM campaign_salad_attempts WHERE attempt_id=?", attempt_id)[0]
    assert row["phase"] == phase
    assert "lease_lapsed_before_route" not in world.dump()


@pytest.mark.parametrize("status", HELD)
def test_awaiting_lease_is_not_listed_while_held(tmp_path, status):
    world = SaladWorld(tmp_path)
    world.start()
    world.tick_until(
        lambda: (
            world.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
            is not None
        )
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    attempt_id = world.salad()["attempt_id"]
    assert attempt_id in _listed(world)
    _hold(world, status)
    assert attempt_id not in _listed(world)


@pytest.mark.parametrize("status", HELD)
def test_forward_commands_stay_refused_while_held(tmp_path, status):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    _set_phase(world, "route_started")
    attempt_id = world.salad()["attempt_id"]
    _hold(world, status)
    with pytest.raises(Conflict):
        world.env.store.mark_salad_executing(world.worker, attempt_id, world.key("executing"))
    assert world.salad()["phase"] == "route_started"
