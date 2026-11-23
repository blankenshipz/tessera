"""The idle-holder reconcile sweep pages with a cursor, so no holder is starved (ADR 0153).

Idle campaigns whose release is still needed and that sort first must not keep a later
idle holder out of the bounded sweep forever; the cursor wraps around once a window comes
up short."""

from __future__ import annotations

import sqlite3
from functools import partial

from campaign_runner_harness import World, stage_spec

from tessera_control import campaign_runner
from tessera_control.campaign_release_scope import idle_holders

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
LIMIT = 1


def _spec(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return spec


def _finished_holder(world: World) -> tuple[str, str]:
    """A failed campaign whose settled attempt used a release (left revoked for now, so the
    next campaign on the same snapshot can be admitted)."""
    campaign = world.campaign(_spec(world))
    cid = campaign["id"]
    complete = world.runner._maybe_complete
    world.runner._maybe_complete = lambda ctx: None
    try:
        world.tick_until(
            lambda: (
                any(a["settled_cents"] is not None for a in world.attempts(cid))
                and world.attempts(cid)[0]["status"] == "succeeded"
            )
        )
    finally:
        world.runner._maybe_complete = complete
    (attempt,) = world.attempts(cid)
    world.env.execute("UPDATE campaigns SET status='failed' WHERE id=?", cid)
    world.env.execute(
        "UPDATE snapshot_releases SET status='revoked' WHERE id=?", attempt["release_id"]
    )
    return cid, attempt["release_id"]


def _own_worker(world: World, release_id: str, index: int) -> None:
    """Give the release its own worker principal so several can be active at once."""
    release = world.row("SELECT * FROM snapshot_releases WHERE id=?", release_id)
    worker = dict(world.row("SELECT * FROM principals WHERE id=?", release["worker_principal_id"]))
    worker["id"] = f"{worker['id']}_copy{index}"
    worker["token_hash"] = f"{worker['token_hash']}_copy{index}"
    columns = ",".join(worker)
    marks = ",".join("?" for _ in worker)
    world.env.execute(f"INSERT INTO principals ({columns}) VALUES ({marks})", *worker.values())
    world.env.execute(
        "UPDATE snapshot_releases SET worker_principal_id=?, status='active' WHERE id=?",
        worker["id"],
        release_id,
    )


def _world(tmp_path, monkeypatch) -> tuple[World, list[tuple[str, str]]]:
    """Three idle holders sorted by id; the first two still need their release."""
    world = World(tmp_path)
    holders = sorted(_finished_holder(world) for _ in range(3))
    for index, (_, release_id) in enumerate(holders):
        _own_worker(world, release_id, index)
    for cid, _ in holders[:2]:
        # The attempt's run may still be live, so its release must stay held.
        world.env.execute(
            "UPDATE campaign_stage_attempts SET status='unknown' WHERE campaign_id=?", cid
        )
    monkeypatch.setattr(
        campaign_runner, "idle_holders", partial(campaign_runner.idle_holders, limit=LIMIT)
    )
    monkeypatch.setattr(
        campaign_runner,
        "idle_slot_holders",
        partial(campaign_runner.idle_slot_holders, limit=LIMIT),
    )
    world.restart_runner()
    return world, holders


def _release_status(world: World, release_id: str) -> str:
    return world.row("SELECT status FROM snapshot_releases WHERE id=?", release_id)["status"]


def _ticks_until_revoked(world: World, release_id: str, bound: int) -> int | None:
    for count in range(1, bound + 1):
        world.runner.tick()
        if _release_status(world, release_id) == "revoked":
            return count
    return None


def test_later_idle_holder_is_freed_past_holders_that_still_need_theirs(tmp_path, monkeypatch):
    world, holders = _world(tmp_path, monkeypatch)
    (first, first_release), (_, second_release), (_, last_release) = holders
    # One holder per pass: the last one is reached on the third pass, not never.
    assert _ticks_until_revoked(world, last_release, len(holders)) == len(holders)
    assert _release_status(world, first_release) == "active"
    assert _release_status(world, second_release) == "active"
    # The first holder's run settles; the sweep wraps around and frees it within a cycle.
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='succeeded' WHERE campaign_id=?", first
    )
    assert _ticks_until_revoked(world, first_release, len(holders)) is not None
    assert _release_status(world, second_release) == "active"


def test_idle_holders_window_and_cursor_wrap_around(tmp_path, monkeypatch):
    world, holders = _world(tmp_path, monkeypatch)
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        ids = [cid for cid, _ in holders]
        seen, cursor = [], None
        for _ in range(len(ids)):
            window, cursor = idle_holders(connection, after=cursor, limit=LIMIT)
            seen += window
        assert seen == ids and cursor == ids[-1]
        # Past the end the window is short: no cursor, so the next pass starts over.
        assert idle_holders(connection, after=cursor, limit=LIMIT) == ([], None)
        assert idle_holders(connection, after=None, limit=LIMIT) == ([ids[0]], ids[0])
        # A window that is not full also ends the cycle.
        assert idle_holders(connection, after=None, limit=len(ids) + 1) == (ids, None)
    finally:
        connection.close()
