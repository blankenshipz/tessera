"""The runner holds an approved campaign until its spec's not_before (G-06)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import campaign_provider_worker as worker

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    worker.register_from_config(None)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _v2(stage: dict) -> dict:
    stage = dict(stage)
    stage.pop("backend")
    stage["runtime"] = "docker-cpu-v1"
    stage["provider"] = "local-docker"
    stage["outputs"] = [
        {"name": "data", "path": path, "max_bytes": stage["max_output_bytes"]}
        for path in stage.pop("output_paths")
    ]
    del stage["max_output_bytes"]
    return stage


def _world(tmp_path, not_before: datetime | None, **overrides):
    world = World(tmp_path)
    spec = world.env.spec(**{"deadline_seconds": 14400, **overrides})
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    if not_before is not None:
        spec["version"] = 2
        spec.pop("worker_principal_id", None)
        spec["stages"] = [_v2(stage) for stage in spec["stages"]]
        spec["not_before"] = _stamp(not_before)
    if not_before is not None:
        # A v2 stage names its provider; local-docker needs the operator's worker principal.
        worker.register_from_config(
            {"local_docker": {"worker_principal_id": world.env.worker["id"]}}
        )
        world.env.trust()
    campaign = world.campaign(spec)
    clock = [datetime.now(UTC)]
    world.runner.now = lambda: clock[0]
    return world, campaign, clock


def _footprint(world: World, cid: str) -> tuple:
    return (
        world.stages(cid),
        len(world.attempts(cid)),
        world.row("SELECT count(*) AS n FROM campaign_step_intents")["n"],
        world.row("SELECT count(*) AS n FROM events")["n"],
        dict(world.row("SELECT held_cents, status FROM campaigns WHERE id=?", cid)),
    )


def test_nothing_starts_before_not_before(tmp_path):
    world, campaign, _ = _world(tmp_path, datetime.now(UTC) + timedelta(hours=1))
    cid = campaign["id"]
    before = _footprint(world, cid)
    reports = [world.runner.tick() for _ in range(5)]
    world.poller.run(max_cycles=1)
    assert any(f"waiting:{cid}:not_before" in report.actions for report in reports)
    assert _footprint(world, cid) == before
    assert set(world.stages(cid).values()) == {"pending"}
    assert world.attempts(cid) == []
    assert world.status(cid) == "running"
    assert world.worker_runs == []


def test_the_campaign_runs_once_the_clock_passes_not_before(tmp_path):
    # Short wait: the fixture credentials expire two hours after real time (deadline check).
    start = datetime.now(UTC) + timedelta(minutes=10)
    world, campaign, clock = _world(tmp_path, start)
    cid = campaign["id"]
    for _ in range(3):
        world.runner.tick()
    assert world.attempts(cid) == []
    clock[0] = start + timedelta(seconds=1)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert world.stages(cid) == {"s1": "succeeded"}


def test_a_past_not_before_runs_immediately(tmp_path):
    world, campaign, _ = _world(tmp_path, datetime.now(UTC) - timedelta(hours=1))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded"


def test_cancel_while_waiting_reaches_cancelled(tmp_path):
    world, campaign, _ = _world(tmp_path, datetime.now(UTC) + timedelta(hours=1))
    cid = campaign["id"]
    world.runner.tick()
    world.cancel(campaign)
    world.tick_all(cid, worker=False)
    assert world.status(cid) == "cancelled"
    assert world.attempts(cid) == []
    assert world.worker_runs == []


def test_an_approval_expiring_while_waiting_pauses_and_never_runs(tmp_path):
    now = datetime.now(UTC)
    world, campaign, clock = _world(tmp_path, now + timedelta(seconds=540), deadline_seconds=600)
    cid = campaign["id"]
    world.runner.tick()
    assert world.status(cid) == "running"
    clock[0] = now + timedelta(seconds=700)
    report = world.runner.tick()
    assert f"paused:{cid}:approval_expired" in report.actions
    assert world.status(cid) == "paused_delegation_lapsed"
    assert (
        world.events_of("campaign.delegation_lapsed")[0]["payload"]["reason"] == "approval_expired"
    )
    world.runner.tick()
    assert world.attempts(cid) == []
    assert world.worker_runs == []


def test_a_spec_without_not_before_is_unchanged(tmp_path):
    world, campaign, _ = _world(tmp_path, None)
    cid = campaign["id"]
    report = world.runner.tick()
    assert not any(action.startswith("waiting:") for action in report.actions)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
