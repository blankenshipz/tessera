"""M1: grants are issued only as far as the executor can route, so a wide campaign waits
before any grant or lease instead of after it.

The fixture's stages share one snapshot, so a second parallel stage would revoke the first's
release; the slot another route occupies is therefore played by a ghost attempt row."""

from __future__ import annotations

import sqlite3

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
)

from tessera_control.campaign_provider_salad import (
    SaladRouteProvider,
    register_from_config,
    register_provider_instance,
)


def _sql(world: SaladWorld, sql: str, *args) -> None:
    """Raw write with foreign keys off, so a ghost row needs no campaign of its own."""
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3", timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(sql, args)
        connection.commit()
    finally:
        connection.close()


# The attempt status the runner records at each phase: the route start flips ``reserved`` to
# ``running`` in the same transaction as the phase, and a route's end keeps ``running`` until
# the attempt is classified.
RUNNING_PHASES = {"route_started", "executing", "outcome_recorded", "awaiting_charge", "unknown"}


def _status(phase: str) -> str:
    return "running" if phase in RUNNING_PHASES else "reserved"


def _occupy(world: SaladWorld, phase: str, ghost: str = "ghost") -> None:
    """Another campaign's attempt that holds a grant at ``phase``, with that phase's status."""
    _sql(
        world,
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, grant_id, revision, created_at, updated_at, provider_id) "
        "VALUES (?, 'ghost_campaign', 'x', 1, ?, 0, ?, 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'salad')",
        ghost,
        _status(phase),
        ghost + "_grant",
    )
    _sql(
        world,
        "INSERT INTO campaign_salad_attempts (attempt_id, phase, revision, updated_at) "
        "VALUES (?, ?, 1, '2026-01-01T00:00:00Z')",
        ghost,
        phase,
    )


def _ghost_phase(world: SaladWorld, phase: str, ghost: str = "ghost") -> None:
    _sql(world, "UPDATE campaign_salad_attempts SET phase=? WHERE attempt_id=?", phase, ghost)
    _sql(world, "UPDATE campaign_stage_attempts SET status=? WHERE id=?", _status(phase), ghost)


def _start(world: SaladWorld, width: int | None) -> None:
    register_provider_instance(world.profiles, guardian_reader=world.guardian, route_width=width)
    world.start()
    world.tick_until(
        lambda: (
            world.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
            is not None
        )
    )
    world.assess_source("g1")


def _grant(world: SaladWorld):
    return world.attempt("g1")["grant_id"] if world.attempt("g1") else None


def test_a_full_width_means_no_grant_until_a_route_slot_frees(tmp_path):
    world = SaladWorld(tmp_path)
    _start(world, 1)
    _occupy(world, "leased")
    world.tick(10)
    # The stage is ready and reserved, but holds no grant, so nothing can be leased, nothing
    # starts a lease clock, and no attempt ends as not started.
    attempt = world.attempt("g1")
    assert attempt is not None and attempt["grant_id"] is None, world.dump()
    assert world.events("campaign.stage_waiting_slot")[0]["stage_key"] == "g1"
    assert attempt["failure_class"] is None
    _ghost_phase(world, "executing")
    world.tick(4)
    assert _grant(world) is None
    # The other route is over: the waiter takes the slot and its grant lifetime starts now.
    _ghost_phase(world, "awaiting_charge")
    world.tick_until(lambda: _grant(world) is not None)
    assert world.phase() in {"awaiting_lease", None}


def test_a_wider_width_admits_the_stage(tmp_path):
    world = SaladWorld(tmp_path)
    _start(world, 2)
    _occupy(world, "route_started")
    world.tick_until(lambda: _grant(world) is not None)
    assert not world.events("campaign.stage_waiting_slot")


def test_no_recorded_width_keeps_the_unbounded_behaviour(tmp_path):
    world = SaladWorld(tmp_path)
    _start(world, None)
    _occupy(world, "executing")
    world.tick_until(lambda: _grant(world) is not None)
    assert not world.events("campaign.stage_waiting_slot")


@pytest.mark.parametrize(
    ("phase", "free"),
    [
        ("awaiting_lease", 0),
        ("leased", 0),
        ("awaiting_review", 0),
        ("reviewed", 0),
        ("capability_ready", 0),
        ("route_proposed", 0),
        ("route_started", 0),
        ("executing", 0),
        ("outcome_recorded", 1),
        ("awaiting_charge", 1),
        ("unknown", 1),
        ("settled", 1),
        ("unclaimed", 1),
    ],
)
def test_slot_count_covers_every_phase_between_grant_and_route_end(tmp_path, phase, free):
    world = SaladWorld(tmp_path)
    register_provider_instance(world.profiles, guardian_reader=world.guardian, route_width=1)
    _occupy(world, phase)
    provider = SaladRouteProvider(route_width=1)
    assert provider.free_slots(world.runner, None, None) == free
    assert provider.slot_free(world.runner, None, None) is bool(free)


def test_a_granted_attempt_with_no_salad_row_yet_still_counts(tmp_path):
    world = SaladWorld(tmp_path)
    _occupy(world, "leased")
    _sql(world, "DELETE FROM campaign_salad_attempts WHERE attempt_id='ghost'")
    assert SaladRouteProvider(route_width=1).free_slots(world.runner, None, None) == 0
    assert SaladRouteProvider(route_width=2).free_slots(world.runner, None, None) == 1


def test_unset_width_is_unbounded_and_bad_widths_are_refused():
    assert SaladRouteProvider().free_slots(None, None, None) is None
    assert SaladRouteProvider().slot_free(None, None, None) is True
    for bad in (0, 5, True, "2"):
        with pytest.raises(ValueError, match="salad_max_concurrent_routes"):
            register_from_config({"salad_max_concurrent_routes": bad})


@pytest.mark.parametrize("phase", ["route_started", "executing"])
def test_a_running_route_holds_its_slot(tmp_path, phase):
    """Regression: the route start flips the attempt to ``running``; that route still holds
    the executor's only slot, so no second grant is issued while it runs."""
    world = SaladWorld(tmp_path)
    _occupy(world, phase)
    assert (
        world.row("SELECT status FROM campaign_stage_attempts WHERE id='ghost'")["status"]
        == "running"
    )
    assert SaladRouteProvider(route_width=1).free_slots(world.runner, None, None) == 0
    assert SaladRouteProvider(route_width=1).slot_free(world.runner, None, None) is False
    assert SaladRouteProvider(route_width=2).free_slots(world.runner, None, None) == 1


@pytest.mark.parametrize("status", ["succeeded", "failed_transient", "failed_permanent"])
def test_a_finished_attempt_frees_its_slot_whatever_its_last_phase(tmp_path, status):
    world = SaladWorld(tmp_path)
    _occupy(world, "executing")
    _sql(world, "UPDATE campaign_stage_attempts SET status=? WHERE id='ghost'", status)
    assert SaladRouteProvider(route_width=1).free_slots(world.runner, None, None) == 1
