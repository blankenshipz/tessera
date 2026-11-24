"""A paused campaign gives back a grant no worker took; resuming queues for a slot (ADR 0167).

The fixture project has ``max_concurrency=1`` and one one-slot local worker, so one unresolved
grant keeps every other campaign waiting at ``reserved``. Pauses are written as the steering
route writes them (a ``campaign_steering`` row) or as the runner's delegation pause leaves them.
"""

from __future__ import annotations

import pytest
from campaign_runner_harness import World, admit, stage_spec, started_but_unreported
from test_blocked_campaign_slot_reclaim import METRIC, _expire_release, _freed, _granted

RELEASED = "execution.paused_unleased_grant_released"
REACQUIRED = "campaign.slot_reacquired"


def _spec(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return spec


def _pause(world: World, cid: str, how: str) -> None:
    if how == "lapsed":
        world.env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", cid)
        return
    row = world.row("SELECT org_id, project_id FROM campaigns WHERE id=?", cid)
    world.env.execute(
        """INSERT INTO campaign_steering (campaign_id, org_id, project_id, paused,
        paused_by_role, paused_reason, paused_at, pause_count, updated_at)
        VALUES (?,?,?,1,'agent','test','2026-01-01T00:00:00+00:00',1,'2026-01-01T00:00:00+00:00')
        ON CONFLICT(campaign_id) DO UPDATE SET paused=1""",
        cid,
        row["org_id"],
        row["project_id"],
    )


def _resume(world: World, cid: str, how: str) -> None:
    if how == "lapsed":
        world.env.execute("UPDATE campaigns SET status='running' WHERE id=?", cid)
    else:
        world.env.execute("UPDATE campaign_steering SET paused=0 WHERE campaign_id=?", cid)


def _holder(world: World, how: str) -> tuple[str, dict]:
    """A campaign paused while its admitted grant is live and no worker took a lease."""
    holder = world.campaign(_spec(world))
    admit(world, holder["id"])
    (attempt,) = world.attempts(holder["id"])
    _pause(world, holder["id"], how)
    # Another campaign uses the same input snapshot here, so the holder's release must not
    # lock it out (a transition to a paused status frees it, as a person's data would differ).
    _expire_release(world, attempt)
    return holder["id"], attempt


@pytest.mark.parametrize("how", ["steering", "lapsed"])
def test_a_paused_campaign_releases_an_unleased_live_grant_and_a_new_campaign_is_admitted(
    tmp_path, how
):
    world = World(tmp_path)
    holder, attempt = _holder(world, how)
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    (admitted,) = world.attempts(fresh["id"])
    assert admitted["status"] == "admitted" and admitted["grant_id"]
    (closed,) = world.attempts(holder)
    assert closed["status"] == "failed_transient" and closed["settled_cents"] == 0
    assert closed["failure_class"] == "not_started"  # an attempt number, not a retry
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    (event,) = _freed(world, holder)
    assert event["reason"] == "paused_unleased_grant"
    assert event["grant_id"] == attempt["grant_id"] and event["released_by"] == "system"
    (released,) = [e for e in world.events_of(RELEASED) if e["payload"]["grant_id"] == grant["id"]]
    assert released["payload"]["consumed_scientific_retry"] is False
    for _ in range(3):
        world.runner.tick()
    assert len(_freed(world, holder)) == 1 and len(world.attempts(holder)) == 1


@pytest.mark.parametrize("how", ["steering", "lapsed"])
def test_resume_when_full_waits_in_line_then_takes_the_next_freed_slot(tmp_path, how):
    world = World(tmp_path)
    holder, first = _holder(world, how)
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    (held,) = world.attempts(fresh["id"])
    _expire_release(world, held)  # same shared snapshot: only the project slot is contended
    _resume(world, holder, how)
    for _ in range(12):
        world.runner.tick()
    waiting = [a for a in world.attempts(holder) if a["attempt"] == 2]
    assert len(waiting) == 1 and waiting[0]["grant_id"] is None  # never exceeds the limit
    queued = [e for e in world.events_of("campaign.stage_waiting_limits")]
    assert any(e["payload"]["attempt_id"] == waiting[0]["id"] for e in queued)
    # A campaign that arrives after the resumed one queues behind it, whatever the tick order.
    newcomer = world.campaign(_spec(world))
    for _ in range(12):
        world.runner.tick()
    assert not _granted(world, newcomer["id"])
    # The slot frees: the resumed campaign, first in line, gets it.
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled' WHERE id=?", held["grant_id"]
    )
    world.env.execute("UPDATE campaigns SET status='cancelled' WHERE id=?", fresh["id"])
    admit(world, holder)
    assert not _granted(world, newcomer["id"])
    second = [a for a in world.attempts(holder) if a["attempt"] == 2][0]
    assert second["status"] == "admitted"
    (again,) = [
        e for e in world.events_of(REACQUIRED) if e["payload"]["attempt_id"] == second["id"]
    ]
    assert again["payload"]["previous_attempt_id"] == first["id"]
    assert again["payload"]["grant_id"] == second["grant_id"]


def test_pausing_and_resuming_again_is_idempotent(tmp_path):
    world = World(tmp_path)
    holder, _ = _holder(world, "steering")
    for _ in range(5):
        world.runner.tick()
    _pause(world, holder, "steering")  # a second pause changes nothing
    for _ in range(5):
        world.runner.tick()
    assert len(_freed(world, holder)) == 1 and len(world.attempts(holder)) == 1
    _resume(world, holder, "steering")
    admit(world, holder)
    second = [a for a in world.attempts(holder) if a["attempt"] == 2]
    assert len(second) == 1 and second[0]["status"] == "admitted"
    assert len(world.events_of(REACQUIRED)) == 1
    _pause(world, holder, "steering")
    for _ in range(3):
        world.runner.tick()  # released again, once, as a new attempt number
    assert len(_freed(world, holder)) == 2


@pytest.mark.parametrize("how", ["steering", "lapsed"])
def test_a_grant_a_worker_took_is_never_released_by_a_pause(tmp_path, how):
    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    attempt = started_but_unreported(world, holder["id"], [])
    # lapse() expired the grant; make it live again: only the lease and operation matter.
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2999-01-01T00:00:00+00:00' WHERE id=?",
        attempt["grant_id"],
    )
    _pause(world, holder["id"], how)
    fresh = world.campaign(_spec(world))
    for _ in range(15):
        world.runner.tick()
    assert not _granted(world, fresh["id"])
    assert not _freed(world, holder["id"])
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "active"
