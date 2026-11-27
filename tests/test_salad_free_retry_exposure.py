"""M2: a retry of a failure that cost nothing is not held back by a healthy running sibling;
unresolved cost (a charge owed or unknown) still blocks every new positive hold."""

from __future__ import annotations

import sqlite3

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_stage,
)

from tessera_control.campaign_providers import get_provider
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_salad import EXPOSURE_PHASES, UNRESOLVED_EXPOSURE_PHASES


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


def _sibling(world: SaladWorld, phase: str) -> None:
    """A second stage's attempt of the same campaign, at ``phase``."""
    _sql(
        world,
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, revision, created_at, updated_at, provider_id) "
        "VALUES ('sibling', ?, 'g2', 1, 'reserved', 0, 1, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', 'salad')",
        world.campaign_id,
    )
    _sql(
        world,
        "INSERT INTO campaign_salad_attempts (attempt_id, phase, revision, updated_at) "
        "VALUES ('sibling', ?, 1, '2026-01-01T00:00:00Z')",
        phase,
    )


def _fail_first_attempt(world: SaladWorld, failure_class: str, settled: int | None = 0) -> None:
    """The first attempt ended for an infrastructure reason after its hold was released."""
    first = world.attempt()
    _sql(
        world,
        "UPDATE campaign_stage_attempts SET status='failed_transient', failure_class=?, "
        "settled_cents=?, settlement_basis='unclaimed_zero' WHERE id=?",
        failure_class,
        settled,
        first["id"],
    )
    _sql(
        world,
        "UPDATE campaign_salad_attempts SET phase='unclaimed' WHERE attempt_id=?",
        first["id"],
    )
    _sql(world, "UPDATE campaigns SET held_cents=0 WHERE id=?", world.campaign_id)


def _world_with_failed_first(tmp_path, failure_class="infra_salad_safety_stop", settled=0):
    world = SaladWorld(tmp_path, stages=stages)
    world.to_route_started()
    _fail_first_attempt(world, failure_class, settled)
    return world


def test_phase_sets_split_a_running_route_from_an_unresolved_cost():
    assert set(UNRESOLVED_EXPOSURE_PHASES) < set(EXPOSURE_PHASES)
    for running in ("leased", "route_started", "executing"):
        assert running not in UNRESOLVED_EXPOSURE_PHASES
    assert {"awaiting_charge", "unknown", "outcome_recorded"} == set(UNRESOLVED_EXPOSURE_PHASES)


@pytest.mark.parametrize(
    ("phase", "broad", "narrow"),
    [
        ("leased", True, False),
        ("route_started", True, False),
        ("executing", True, False),
        ("outcome_recorded", True, True),
        ("awaiting_charge", True, True),
        ("unknown", True, True),
        ("settled", False, False),
    ],
)
def test_provider_exposure_probes(tmp_path, phase, broad, narrow):
    world = SaladWorld(tmp_path, stages=stages)
    world.start()
    _sibling(world, phase)
    provider = get_provider("salad")
    with world.runner.store.runner_connection() as connection:
        cid = world.campaign_id
        assert provider.exposure_open(connection, cid) is broad
        assert provider.unresolved_exposure_open(connection, cid) is narrow
        assert CampaignRunner._provider_exposure(provider, connection, cid, False) is broad
        assert CampaignRunner._provider_exposure(provider, connection, cid, True) is narrow


def test_a_provider_without_the_narrow_probe_keeps_the_whole_gate():
    class Plain:
        def exposure_open(self, connection, campaign_id):
            return True

    assert CampaignRunner._provider_exposure(Plain(), None, "c", True) is True


def test_a_zero_cent_infra_retry_is_not_blocked_by_an_executing_sibling(tmp_path):
    world = _world_with_failed_first(tmp_path)
    _sibling(world, "executing")
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    assert not world.events("campaign.stage_waiting_exposure"), world.dump()
    assert world.events("campaign.stage_infra_retry")


@pytest.mark.parametrize("phase", ["awaiting_charge", "unknown", "outcome_recorded"])
def test_unresolved_cost_still_blocks_the_retry(tmp_path, phase):
    world = _world_with_failed_first(tmp_path)
    _sibling(world, phase)
    world.tick(8)
    assert world.attempt()["attempt"] == 1, world.dump()
    assert world.events("campaign.stage_waiting_exposure")


def test_a_retry_after_a_charged_failure_keeps_the_whole_gate(tmp_path):
    # The failed attempt settled above zero: not a free retry, so a running sibling holds it.
    world = _world_with_failed_first(tmp_path, settled=3)
    _sibling(world, "executing")
    world.tick(8)
    assert world.attempt()["attempt"] == 1, world.dump()
    assert world.events("campaign.stage_waiting_exposure")


def test_a_scientific_failure_keeps_the_whole_gate(tmp_path):
    world = _world_with_failed_first(tmp_path, failure_class="t1_salad_unclaimed")
    _sibling(world, "executing")
    runner: CampaignRunner = world.runner
    ctx = type("C", (), {"campaign": {"id": world.campaign_id}})()
    assert runner._free_retry(ctx, "g1") is False
