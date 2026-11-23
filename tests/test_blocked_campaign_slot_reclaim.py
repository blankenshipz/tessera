"""A stuck (blocked or paused) campaign stops holding capacity it provably is not using.

The fixture project has ``max_concurrency=1`` and one one-slot local worker, so a holder's
unresolved grant keeps every other campaign waiting at ``reserved``. The placement-queue
cases use two candidate providers (ADR 0060), the only path that counts placed attempts.
"""

from __future__ import annotations

import pytest
from campaign_runner_harness import World, admit, dump, stage_spec, started_but_unreported
from test_campaign_models_v2 import _spec2
from test_campaign_placement import NEAR, PLACED, _register, _stage, _world

from tessera_control import campaign_placement as placement
from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import unregister_provider

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00+00:00"
SLOT_FREED = "campaign.slot_freed"
OVERRUN = "budget_overrun: test"


@pytest.fixture
def providers():
    made: list[str] = []

    def make(provider_id: str, principal: str, slots: int = 1) -> None:
        _register(provider_id, principal, slots)
        made.append(provider_id)

    yield make
    for provider_id in made:
        unregister_provider(provider_id)


def _spec(world: World, **extra) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
    return spec


def _expire_release(world: World, attempt) -> None:
    world.env.execute(
        "UPDATE snapshot_releases SET expires_at=? WHERE id=?", PAST, attempt["release_id"]
    )


def _expire_grant(world: World, grant_id: str) -> None:
    world.env.execute("UPDATE execution_grants SET expires_at=? WHERE id=?", PAST, grant_id)


def _stick(world: World, cid: str, status: str) -> None:
    """Blocked as a late overrun leaves it (only a person moves it on), or paused."""
    reason = OVERRUN if status == "blocked" else None
    world.env.execute(
        "UPDATE campaigns SET status=?, stop_reason=? WHERE id=?", status, reason, cid
    )


def _freed(world: World, cid: str) -> list[dict]:
    return [e["payload"] for e in world.events_of(SLOT_FREED) if e["payload"]["campaign_id"] == cid]


def _granted(world: World, cid: str) -> bool:
    return any(a["grant_id"] for a in world.attempts(cid))


def _unclaimed_holder(world: World, status: str, **extra) -> tuple[str, dict]:
    """A campaign stuck in ``status`` whose admitted grant expired before any worker took it."""
    holder = world.campaign(_spec(world, **extra))
    admit(world, holder["id"])
    (attempt,) = world.attempts(holder["id"])
    _expire_grant(world, attempt["grant_id"])
    _expire_release(world, attempt)
    _stick(world, holder["id"], status)
    return holder["id"], attempt


# -- the project concurrency slot -------------------------------------------------------------


@pytest.mark.parametrize("status", ["blocked", "paused_delegation_lapsed"])
def test_an_expired_unclaimed_grant_of_a_stuck_campaign_frees_the_project_slot(tmp_path, status):
    world = World(tmp_path)
    holder, attempt = _unclaimed_holder(world, status)
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    (admitted,) = world.attempts(fresh["id"])
    assert admitted["status"] == "admitted" and admitted["grant_id"]
    # The holder keeps its status; its grant was reconciled at zero by the existing rule.
    assert world.status(holder) == status
    (closed,) = world.attempts(holder)
    assert closed["status"] == "failed_transient"
    assert closed["failure_class"] == "t1_grant_expired_unleased"
    assert closed["settled_cents"] == 0
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    (event,) = _freed(world, holder)
    assert event["reason"] == "expired_unclaimed_grant"
    assert event["grant_id"] == attempt["grant_id"] and event["attempt_id"] == attempt["id"]
    assert event["stage_key"] == "s1" and event["attempt"] == 1
    assert event["provider_id"] == attempt["provider_id"]
    assert event["campaign_status"] == status and event["released_by"] == "system"
    for _ in range(3):
        world.runner.tick()
    assert len(_freed(world, holder)) == 1
    (reconciled,) = [
        e
        for e in world.events_of("execution.expired_unattempted_grant_reconciled")
        if e["payload"]["grant_id"] == attempt["grant_id"]
    ]
    campaign = world.row("SELECT org_id, project_id FROM campaigns WHERE id=?", holder)
    assert (reconciled["org_id"], reconciled["project_id"]) == (
        campaign["org_id"],
        campaign["project_id"],
    )


def test_a_claimed_grant_without_a_confirmed_exit_keeps_the_slot(tmp_path):
    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    attempt = started_but_unreported(world, holder["id"], [])
    assert (
        world.row(
            "SELECT count(*) AS n FROM execution_leases WHERE grant_id=?", attempt["grant_id"]
        )["n"]
        == 1
    )
    _expire_release(world, attempt)
    _stick(world, holder["id"], "blocked")
    fresh = world.campaign(_spec(world))
    for _ in range(15):
        world.runner.tick()
    # It may still be running: a person must reconcile it before the slot is free.
    assert not _granted(world, fresh["id"])
    assert not _freed(world, holder["id"])
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "active"
    assert any(
        e["payload"]["campaign_id"] == fresh["id"]
        for e in world.events_of("campaign.stage_waiting_limits")
    )


def test_a_live_unclaimed_grant_keeps_the_slot(tmp_path):
    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    admit(world, holder["id"])
    (attempt,) = world.attempts(holder["id"])
    _expire_release(world, attempt)
    _stick(world, holder["id"], "blocked")
    fresh = world.campaign(_spec(world))
    for _ in range(15):
        world.runner.tick()
    assert not _granted(world, fresh["id"])
    assert not _freed(world, holder["id"])
    assert world.attempts(holder["id"])[0]["status"] == "admitted"


def test_pause_then_resume_retries_without_double_running(tmp_path):
    world = World(tmp_path)
    # The closed attempt uses a retry, as the same expiry does on a running campaign.
    holder, attempt = _unclaimed_holder(
        world, "paused_delegation_lapsed", retry={"max_attempts": 2}
    )
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    live = "SELECT id FROM execution_grants WHERE status IN ('active','cancel_requested')"
    assert [row["id"] for row in world.env.query(live)] == [
        world.attempts(fresh["id"])[0]["grant_id"]
    ]
    world.tick_all(fresh["id"])
    # Resumed once the newer campaign is done: a new attempt gets a new grant through the
    # normal checks; the expired grant stays reconciled and never ran.
    world.env.approve(dict(world.row("SELECT * FROM campaigns WHERE id=?", holder)))
    assert world.status(holder) == "running"
    world.tick_all(holder)
    assert world.status(fresh["id"]) == "succeeded"
    assert world.status(holder) == "succeeded", dump(world, holder)
    first, second = world.attempts(holder)
    assert first["id"] == attempt["id"] and first["status"] == "failed_transient"
    assert second["status"] == "succeeded" and second["grant_id"] != attempt["grant_id"]
    assert len(world.worker_runs) == len(set(world.worker_runs)) == 2
    assert attempt["grant_id"] not in world.worker_runs
    assert len(_freed(world, holder)) == 1


# -- the placement queue (multi-candidate stages) -----------------------------------------


def _queued_on_near(world: World, monkeypatch) -> str:
    """A campaign placed on NEAR whose grant waits for NEAR's slot (reserved, no grant)."""
    monkeypatch.setattr(worker.WorkerGrantProvider, "slot_free", lambda *args: False)
    campaign = world.campaign(_spec2(_stage(world.env, "a")))
    cid = campaign["id"]
    world.tick_until(lambda: any(a["release_id"] for a in world.attempts(cid)), worker=False)
    for _ in range(2):
        world.runner.tick()
    (attempt,) = world.attempts(cid)
    assert attempt["provider_id"] == NEAR and attempt["status"] == "reserved"
    assert attempt["grant_id"] is None
    return cid


def _placements(world: World, cid: str) -> list[dict]:
    return [e["payload"] for e in world.events_of(PLACED) if e["payload"]["campaign_id"] == cid]


def test_a_stuck_campaigns_queued_attempt_no_longer_takes_the_slot(
    tmp_path, providers, monkeypatch
):
    world = _world(tmp_path, providers)
    stuck = _queued_on_near(world, monkeypatch)
    assert placement.placed_not_granted(world.runner, NEAR) == 1
    _expire_release(world, world.attempts(stuck)[0])
    stage_status = world.stages(stuck)["a"]
    world.runner._stage_unknown(world.runner._load(stuck), "a", "test")
    world.env.execute("UPDATE campaigns SET stop_reason=? WHERE id=?", OVERRUN, stuck)
    monkeypatch.undo()
    assert placement.placed_not_granted(world.runner, NEAR) == 0
    fresh = world.campaign(_spec2(_stage(world.env, "a")))
    world.tick_until(lambda: _placements(world, fresh["id"]), worker=False)
    (placed,) = _placements(world, fresh["id"])
    assert placed["provider_id"] == NEAR and placed["skipped"] == []
    (event,) = _freed(world, stuck)
    assert event["reason"] == "placed_not_granted" and event["trigger"] == "campaign_blocked"
    assert event["provider_id"] == NEAR and event["campaign_status"] == "blocked"
    (kept,) = world.attempts(stuck)
    assert kept["status"] == "reserved" and world.status(stuck) == "blocked"
    # Resumed while the newer attempt holds NEAR's only slot: it counts again and waits.
    world.tick_until(lambda: _granted(world, fresh["id"]), worker=False)
    world.env.execute("UPDATE campaigns SET stop_reason=NULL WHERE id=?", stuck)
    world.env.execute(  # a person resolved what blocked it
        "UPDATE campaign_stages SET status=? WHERE campaign_id=?", stage_status, stuck
    )
    world.runner.tick()
    assert world.status(stuck) == "running"
    for _ in range(4):
        world.runner.tick()
    assert not _granted(world, stuck)
    assert any(
        e["payload"]["campaign_id"] == stuck for e in world.events_of("campaign.stage_waiting_slot")
    )


def test_a_running_campaigns_queued_attempt_still_takes_the_slot(tmp_path, providers, monkeypatch):
    world = _world(tmp_path, providers)
    queued = _queued_on_near(world, monkeypatch)
    assert world.status(queued) == "running"
    assert placement.placed_not_granted(world.runner, NEAR) == 1
    fresh = world.campaign(_spec2(_stage(world.env, "a")))
    world.tick_until(lambda: _placements(world, fresh["id"]), worker=False)
    (placed,) = _placements(world, fresh["id"])
    assert placed["provider_id"] != NEAR
    assert [s["reason"] for s in placed["skipped"]] == ["no_free_slot"]
    assert not _freed(world, queued)


def test_an_attempt_whose_grant_request_was_written_keeps_counting(
    tmp_path, providers, monkeypatch
):
    world = _world(tmp_path, providers)
    stuck = _queued_on_near(world, monkeypatch)
    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", stuck))
    approval = dict(world.row("SELECT id FROM campaign_approvals WHERE campaign_id=?", stuck))
    # A crash after the issue_grant intent: a replay issues the grant without a slot check.
    world.env.intent(
        campaign,
        approval,
        "issue_grant",
        {"project_id": campaign["project_id"]},
        {"synthetic": True},
        stage_key="a",
        attempt=1,
    )
    world.runner._stage_unknown(world.runner._load(stuck), "a", "test")
    assert world.status(stuck) == "blocked"
    assert placement.placed_not_granted(world.runner, NEAR) == 1
    assert not _freed(world, stuck)


def test_the_queue_event_is_written_once_per_spell(tmp_path, providers, monkeypatch):
    world = _world(tmp_path, providers)
    stuck = _queued_on_near(world, monkeypatch)
    world.runner._stage_unknown(world.runner._load(stuck), "a", "test")
    (first,) = _freed(world, stuck)
    # Nothing unknown is left, so the next tick unblocks it; the sweep adds nothing first.
    world.runner.tick()
    assert world.status(stuck) == "running" and len(_freed(world, stuck)) == 1
    assert placement.placed_not_granted(world.runner, NEAR) == 1
    world.runner._stage_unknown(world.runner._load(stuck), "a", "test")
    world.env.execute("UPDATE campaigns SET stop_reason=? WHERE id=?", OVERRUN, stuck)
    for _ in range(3):
        world.runner.tick()
    events = _freed(world, stuck)
    assert len(events) == 2 and events[0] == first
    assert [e["trigger"] for e in events] == ["campaign_blocked", "campaign_blocked"]


def test_the_sweep_records_a_campaign_that_went_idle_earlier(tmp_path, providers, monkeypatch):
    world = _world(tmp_path, providers)
    stuck = _queued_on_near(world, monkeypatch)
    _stick(world, stuck, "blocked")
    world.runner.tick()
    world.runner.tick()
    (event,) = _freed(world, stuck)
    assert event["trigger"] == "reconcile_sweep" and event["reason"] == "placed_not_granted"
