"""A paused campaign gives its input release back and queues for it on resume (ADR 0167).

Every campaign here uses the same input snapshot, so the unique active release is the thing
they contend for. Nothing expires a release by hand: the release is freed by the pause, the
second campaign's own admission takes it, and the resumed campaign waits its turn.
"""

from __future__ import annotations

from campaign_runner_harness import World, admit
from test_paused_campaign_slot_release import _pause, _resume, _spec

FREED = "campaign.release_freed"
WAITING = "campaign.stage_waiting_release"


def _paused_holder(world: World, how: str = "steering") -> str:
    holder = world.campaign(_spec(world))
    admit(world, holder["id"])
    _pause(world, holder["id"], how)
    for _ in range(4):
        world.runner.tick()
    return holder["id"]


def _freed(world: World, cid: str) -> list[dict]:
    return [e["payload"] for e in world.events_of(FREED) if e["payload"]["campaign_id"] == cid]


def _waiting(world: World, cid: str) -> list[dict]:
    return [e["payload"] for e in world.events_of(WAITING) if e["payload"]["campaign_id"] == cid]


def _release_of(world: World, cid: str):
    return world.row(
        "SELECT r.* FROM snapshot_releases r JOIN campaign_stage_attempts a ON a.release_id=r.id "
        "WHERE a.campaign_id=? ORDER BY a.attempt LIMIT 1",
        cid,
    )


def test_a_second_campaign_on_the_same_snapshot_is_admitted_while_the_first_is_paused(tmp_path):
    world = World(tmp_path)
    holder = _paused_holder(world)
    (event,) = _freed(world, holder)
    assert event["reason"] == "paused" and event["released_by"] == "system"
    assert event["trigger"] == "paused_campaign_release"
    assert _release_of(world, holder)["status"] == "revoked"
    second = world.campaign(_spec(world))
    admit(world, second["id"])
    (attempt,) = world.attempts(second["id"])
    assert attempt["status"] == "admitted" and attempt["release_id"]
    for _ in range(3):
        world.runner.tick()
    assert len(_freed(world, holder)) == 1  # idempotent


def test_resume_waits_for_the_snapshot_then_takes_it_ahead_of_a_later_campaign(tmp_path):
    world = World(tmp_path)
    holder = _paused_holder(world)
    second = world.campaign(_spec(world))
    admit(world, second["id"])
    _resume(world, holder, "steering")
    for _ in range(10):
        world.runner.tick()
    (waiting,) = [a for a in world.attempts(holder) if a["attempt"] == 2]
    # Waits (no failure, no release, no grant); it never exceeds the single active release.
    assert waiting["status"] == "reserved" and waiting["release_id"] is None
    (note,) = _waiting(world, holder)
    assert note["reason"] == "held_by_campaign" and note["returning_from_pause"] is True
    assert note["attempt_id"] == waiting["id"]
    # A campaign that arrives after the resume queues behind it.
    third = world.campaign(_spec(world))
    for _ in range(10):
        world.runner.tick()
    assert not any(a["release_id"] for a in world.attempts(third["id"]))
    assert world.status(third["id"]) == "running"
    # The campaign holding the snapshot finishes; the resumed campaign is first in line.
    world.tick_all(second["id"])  # it runs to the end, so its release is freed
    assert world.status(second["id"]) == "succeeded"
    world.tick_until(
        lambda: any(a["release_id"] for a in world.attempts(holder) if a["attempt"] == 2),
        worker=False,
    )
    assert not any(a["release_id"] for a in world.attempts(third["id"]))
    assert len(_waiting(world, holder)) == 1  # once per attempt


def test_pause_resume_pause_is_idempotent_and_frees_the_release_each_time(tmp_path):
    world = World(tmp_path)
    holder = _paused_holder(world)
    for _ in range(5):
        world.runner.tick()
    _pause(world, holder, "steering")
    for _ in range(3):
        world.runner.tick()
    assert len(_freed(world, holder)) == 1
    _resume(world, holder, "steering")
    admit(world, holder)  # nobody else wants the snapshot: re-acquired at once, no waiting
    assert not _waiting(world, holder)
    _pause(world, holder, "steering")
    for _ in range(4):
        world.runner.tick()
    assert len(_freed(world, holder)) == 2


def test_a_paused_campaign_with_a_leased_grant_keeps_its_release(tmp_path):
    from campaign_runner_harness import started_but_unreported

    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    attempt = started_but_unreported(world, holder["id"], [])
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2999-01-01T00:00:00+00:00' WHERE id=?",
        attempt["grant_id"],
    )
    _pause(world, holder["id"], "steering")
    for _ in range(8):
        world.runner.tick()
    assert not _freed(world, holder["id"])
    assert _release_of(world, holder["id"])["status"] == "active"
