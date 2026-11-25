"""Idle-holder reconcile sweep cursor and the stuck-campaign classifier's skip cases (ADR 0153).

The sweep visits ids up to the nearer end of the two windows and keeps its cursor when the
database is busy. The stuck-campaign observation never classifies attempts it must leave to
another path (route providers, unregistered providers, ungranted attempts, grants with no
lease and no operation)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from campaign_runner_harness import World, stage_spec, started_but_unreported

from tessera_control import campaign_runner

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00+00:00"


# -- cursor ---------------------------------------------------------------------------------


def _sweep(world: World, monkeypatch, held, slots) -> list[str]:
    """Run one sweep with canned windows; returns the campaign ids it visited."""
    world.runner.tick()  # takes the runner lease
    visited: list[str] = []
    monkeypatch.setattr(campaign_runner, "idle_holders", lambda connection, after: held)
    monkeypatch.setattr(campaign_runner, "idle_slot_holders", lambda connection, after: slots)
    monkeypatch.setattr(
        world.runner,
        "_free_campaign_releases",
        lambda connection, cid, trigger: visited.append(cid),
    )
    world.runner._free_idle_releases()
    return visited


@pytest.mark.parametrize(
    ("held", "slots", "visited", "cursor"),
    [
        # The slot window ends first: ids past its end wait for the next pass.
        ((["a", "c"], "c"), (["b"], "b"), ["a", "b"], "b"),
        # The release window ends first.
        ((["a"], "a"), (["b", "c"], "c"), ["a"], "a"),
        # One short window: the full one still bounds the pass.
        ((["a", "b"], None), (["c"], "c"), ["a", "b", "c"], "c"),
        # Both short: everything is visited and the cursor wraps to the start.
        ((["a", "b"], None), (["c"], None), ["a", "b", "c"], None),
    ],
)
def test_sweep_visits_up_to_the_nearer_window_end(
    tmp_path, monkeypatch, held, slots, visited, cursor
):
    world = World(tmp_path)
    assert _sweep(world, monkeypatch, held, slots) == visited
    assert world.runner._idle_cursor == cursor


def test_a_busy_database_keeps_the_cursor(tmp_path, monkeypatch):
    world = World(tmp_path)
    _sweep(world, monkeypatch, (["a"], "a"), (["a"], "a"))
    assert world.runner._idle_cursor == "a"

    def busy(connection, after):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(campaign_runner, "idle_holders", busy)
    world.runner._free_idle_releases()
    assert world.runner._idle_cursor == "a"

    def broken(connection, after):
        raise sqlite3.OperationalError("no such table: nope")

    monkeypatch.setattr(campaign_runner, "idle_holders", broken)
    with pytest.raises(sqlite3.OperationalError):
        world.runner._free_idle_releases()
    assert world.runner._idle_cursor == "a"


# -- attempts the stuck classifier leaves alone ----------------------------------------------


def _stuck_holder(world: World) -> tuple[str, dict]:
    """Leased and started, every report lost, grant lapsed, campaign blocked."""
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    cid = world.campaign(spec)["id"]
    attempt = started_but_unreported(world, cid, [])
    world.env.execute(
        "UPDATE snapshot_releases SET expires_at=? WHERE id=?", PAST, attempt["release_id"]
    )
    world.env.execute(
        "UPDATE campaigns SET status='blocked', stop_reason='budget_overrun: test' WHERE id=?",
        cid,
    )
    return cid, attempt


def _only_attempt(world: World, cid: str):
    (attempt,) = world.attempts(cid)
    return attempt


def _tick(world: World, times: int = 3) -> None:
    for _ in range(times):
        world.runner.tick()


def test_the_unmodified_holder_is_classified(tmp_path):
    """Control for the cases below: with nothing special, the expired grant goes unknown."""
    world = World(tmp_path)
    cid, _ = _stuck_holder(world)
    _tick(world)
    assert _only_attempt(world, cid)["status"] == "unknown"


def test_a_route_provider_is_left_to_its_own_guardian(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, attempt = _stuck_holder(world)
    route = SimpleNamespace(descriptor=SimpleNamespace(kind="route"))
    monkeypatch.setattr(world.runner, "_provider", lambda stage: route)
    _tick(world)
    kept = _only_attempt(world, cid)
    assert (kept["status"], kept["revision"]) == (attempt["status"], attempt["revision"])
    assert kept["failure_class"] is None


def test_an_unregistered_provider_fails_closed(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, attempt = _stuck_holder(world)

    def missing(stage):
        raise KeyError("unregistered")

    monkeypatch.setattr(world.runner, "_provider", missing)
    _tick(world)
    kept = _only_attempt(world, cid)
    assert (kept["status"], kept["revision"]) == (attempt["status"], attempt["revision"])
    assert kept["failure_class"] is None
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "active"


def test_a_granted_attempt_is_not_observed(tmp_path):
    world = World(tmp_path)
    cid, attempt = _stuck_holder(world)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='granted' WHERE id=?", attempt["id"]
    )
    _tick(world)
    kept = _only_attempt(world, cid)
    assert kept["status"] == "granted" and kept["failure_class"] is None


def test_a_leased_grant_without_an_operation_is_classified_unknown(tmp_path):
    world = World(tmp_path)
    cid, attempt = _stuck_holder(world)
    db = sqlite3.connect(world.env.state / "tessera.sqlite3")  # foreign keys off: rows only
    try:
        db.execute("DELETE FROM external_operations WHERE grant_id=?", (attempt["grant_id"],))
        db.commit()
    finally:
        db.close()
    _tick(world)
    kept = _only_attempt(world, cid)
    assert (kept["status"], kept["failure_class"]) == ("unknown", "observed_unknown")
    assert "without an operation" in kept["failure_detail"]


def test_a_live_grant_is_never_classified(tmp_path):
    world = World(tmp_path)
    cid, attempt = _stuck_holder(world)
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2999-01-01T00:00:00+00:00' WHERE id=?",
        attempt["grant_id"],
    )
    _tick(world)
    assert _only_attempt(world, cid)["status"] == attempt["status"]
