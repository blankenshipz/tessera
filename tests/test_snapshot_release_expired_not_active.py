"""A snapshot release whose expiry has passed is never treated as active."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from campaign_runner_harness import World, admit, stage_spec

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00+00:00"


def _spec(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return spec


def _stuck_holder(world: World, expires_at: str) -> tuple[str, str]:
    """A running campaign whose admitted attempt holds the release (not idle, so the
    idle-free sweep never reclaims it), with the release expiry moved to ``expires_at``."""
    holder = world.campaign(_spec(world))
    admit(world, holder["id"])
    (attempt,) = world.attempts(holder["id"])
    assert world.status(holder["id"]) == "running"
    world.env.execute(
        "UPDATE snapshot_releases SET expires_at=? WHERE id=?", expires_at, attempt["release_id"]
    )
    return holder["id"], attempt["release_id"]


def test_expired_release_of_a_stuck_campaign_does_not_block_a_new_campaign(tmp_path):
    world = World(tmp_path)
    holder, expired = _stuck_holder(world, PAST)
    fresh = world.campaign(_spec(world))
    # The stuck holder keeps its concurrency slot, so the new attempt gets its own release
    # and then waits for capacity; it is never refused.
    world.tick_until(
        lambda: any(a["release_id"] for a in world.attempts(fresh["id"])), worker=False
    )
    (attempt,) = world.attempts(fresh["id"])
    assert attempt["failure_class"] is None and world.status(fresh["id"]) == "running"
    assert attempt["release_id"] and attempt["release_id"] != expired
    old = world.row("SELECT status, revoked_at FROM snapshot_releases WHERE id=?", expired)
    assert old["status"] == "revoked" and old["revoked_at"]
    active = world.env.query("SELECT id FROM snapshot_releases WHERE status='active'")
    assert [row["id"] for row in active] == [attempt["release_id"]]
    (event,) = [
        e
        for e in world.events_of("workspace.snapshot_release_expired")
        if e["payload"]["release_id"] == expired
    ]
    assert event["payload"]["replaced_by"] == attempt["release_id"]
    assert event["payload"]["expired_at"] == PAST
    assert world.status(holder) == "running"


def test_release_not_yet_expired_still_refuses(tmp_path):
    world = World(tmp_path)
    soon = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    holder, release = _stuck_holder(world, soon)
    second = world.campaign(_spec(world))
    world.tick_until(lambda: world.attempts(second["id"]), worker=False)
    world.tick_until(lambda: world.status(second["id"]) == "failed", worker=False)
    (refused,) = world.attempts(second["id"])
    assert refused["failure_class"] == "step_refused" and refused["grant_id"] is None
    assert holder in refused["failure_detail"]
    assert not world.events_of("workspace.snapshot_release_expired")
