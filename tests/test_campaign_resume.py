"""Campaign runner restart/resume (ADR 0038 section 8): every tick re-derives from rows."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import CampaignRunner, start_background
from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.store import Store

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _campaign(world: World, **stage_extra) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    return world.campaign(spec)


def _shape(world: World, campaign_id: str) -> dict:
    """The receipt with ids, digests and timestamps removed."""
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign_id)[
            "receipt_json"
        ]
    )
    return {
        "status": receipt["status"],
        "stop_reason": receipt["stop_reason"],
        "budget": receipt["budget"],
        "verdict": receipt["verdict"],
        "stages": [
            {
                "key": stage["stage_key"],
                "status": stage["status"],
                "stop_results": stage["stop_results"],
                "attempts": [
                    {
                        "status": attempt["status"],
                        "exit_code": attempt["exit_code"],
                        "failure_class": attempt["failure_class"],
                        "measurements": [(m["name"], m["value"]) for m in attempt["measurements"]],
                        "hold_cents": attempt["hold_cents"],
                        "settled_cents": attempt["settled_cents"],
                    }
                    for attempt in stage["attempts"]
                ],
            }
            for stage in receipt["stages"]
        ],
        "lineage": (len(receipt["lineage"]["node_ids"]), len(receipt["lineage"]["edge_ids"])),
    }


def _count(world: World, sql: str, *args) -> int:
    return world.row(sql, *args)["n"]


def _assert_one_of_everything(world: World, campaign_id: str) -> None:
    assert _count(world, "SELECT count(*) AS n FROM campaign_stages WHERE task_id IS NOT NULL") == 1
    assert _count(world, "SELECT count(*) AS n FROM tasks WHERE title LIKE '%/s1'") == 1
    assert _count(world, "SELECT count(*) AS n FROM research_experiments") == 1
    attempts = world.attempts(campaign_id)
    assert _count(world, "SELECT count(*) AS n FROM execution_grants") == len(attempts) == 1
    assert _count(world, "SELECT count(*) AS n FROM research_experiment_admitted_attempts") == len(
        attempts
    )
    statuses = {row["status"] for row in world.env.intents(campaign_id)}
    assert statuses <= {"done", "abandoned"} and "done" in statuses


_CLEAN: dict[str, dict] = {}


@pytest.fixture
def clean_shape(tmp_path):
    """The receipt shape of an uninterrupted run (computed once; fixtures are per-test)."""
    if "shape" not in _CLEAN:
        reference = tmp_path / "reference"
        reference.mkdir()
        world = World(reference)
        campaign = _campaign(world)
        world.tick_all(campaign["id"])
        _CLEAN["shape"] = _shape(world, campaign["id"])
    return _CLEAN["shape"]


def test_a_fresh_runner_every_tick_reaches_the_same_receipt(tmp_path, clean_shape):
    world = World(tmp_path)
    campaign = _campaign(world)
    for _ in range(300):
        world.restart_runner()
        world.runner.tick()
        world.poller.run(max_cycles=1)
        if world.status(campaign["id"]) == "succeeded":
            break
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    _assert_one_of_everything(world, campaign["id"])
    assert _shape(world, campaign["id"]) == clean_shape
    assert _count(world, "SELECT count(*) AS n FROM campaign_runner_lease") == 1


CRASH_POINTS = [
    (Store, "create_task"),
    (ExecutionStore, "issue_execution_grant"),
    (ExperimentStore, "admit_attempt"),
    (ExecutionStore, "reconcile_execution_grant"),
]


@pytest.mark.parametrize("when", ["before_effect", "after_effect"])
@pytest.mark.parametrize(("owner_class", "method"), CRASH_POINTS)
def test_crash_around_a_delegated_effect_replays_without_a_second_effect(
    tmp_path, monkeypatch, clean_shape, owner_class, method, when
):
    world = World(tmp_path)
    campaign = _campaign(world)
    cid = campaign["id"]
    original = getattr(owner_class, method)
    fired = []

    def crashing(self, *args, **kwargs):
        if fired:
            return original(self, *args, **kwargs)
        fired.append(True)
        if when == "after_effect":
            original(self, *args, **kwargs)  # committed, but the reply is lost
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(owner_class, method, crashing)
    world.tick_until(lambda: bool(fired))
    intents = [r for r in world.env.intents(cid)]
    pending = [r for r in intents if r["status"] == "pending"]
    # (a) a crash before the effect leaves a pending intent and no effect;
    # (b) a crash after it leaves the intent already done, in the effect's transaction.
    assert bool(pending) == (when == "before_effect")
    world.restart_runner()  # the process restarted
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    _assert_one_of_everything(world, cid)
    assert _shape(world, cid) == clean_shape


def test_re_approval_after_a_lapse_reissues_the_pending_intent_under_a_new_key(
    tmp_path, monkeypatch, clean_shape
):
    world = World(tmp_path)
    campaign = _campaign(world)
    cid = campaign["id"]
    first_key = []

    def crashing(self, principal, project_id, key, digest, payload):
        first_key.append(key)
        raise RuntimeError("synthetic crash")

    original = Store.create_task
    monkeypatch.setattr(Store, "create_task", crashing)
    world.tick_until(lambda: bool(first_key), worker=False)
    monkeypatch.setattr(Store, "create_task", original)
    assert [r["status"] for r in world.env.intents(cid)] == ["pending"]
    assert _count(world, "SELECT count(*) AS n FROM tasks WHERE title LIKE '%/s1'") == 0
    # The approver's authority lapses: the campaign pauses and runs nothing.
    world.env.execute(
        "UPDATE principals SET revoked_at='2026-01-01T00:00:00Z' WHERE id=?",
        campaign["approval"]["approving_principal_id"],
    )
    world.runner.tick()
    assert world.status(cid) == "paused_delegation_lapsed"
    assert world.events_of("campaign.delegation_lapsed")
    before = len(world.env.intents(cid))
    world.runner.tick()
    assert len(world.env.intents(cid)) == before
    # A different human re-approves the same digest; the pending intent is abandoned.
    row = dict(world.row("SELECT * FROM campaigns WHERE id=?", cid))
    second = world.env.approve(row, world.env.human("second"))
    assert (
        second["approval"]["approving_principal_id"]
        != campaign["approval"]["approving_principal_id"]
    )
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    statuses = [r["status"] for r in world.env.intents(cid)]
    assert statuses.count("abandoned") == 1 and "pending" not in statuses
    keys = [r["idempotency_key"] for r in world.env.intents(cid) if r["step"] == "create_task"]
    assert len(keys) == 2 and len(set(keys)) == 2 and keys[0] == first_key[0]
    _assert_one_of_everything(world, cid)
    assert _shape(world, cid) == clean_shape
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", cid)[
            "receipt_json"
        ]
    )
    assert (
        receipt["approval"]["approving_principal_id"]
        == second["approval"]["approving_principal_id"]
    )


def test_an_expired_approval_pauses_forward_work(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)
    cid = campaign["id"]
    world.env.execute(
        "UPDATE campaign_approvals SET expires_at='2000-01-01T00:00:00Z' WHERE campaign_id=?", cid
    )
    world.runner.tick()
    assert world.status(cid) == "paused_delegation_lapsed"
    assert world.env.intents(cid) == []


def test_only_one_runner_holds_the_lease_until_it_expires(tmp_path):
    world = World(tmp_path)
    first = CampaignRunner(world.env.state, holder_id="runner-a", lease_seconds=30)
    assert first.tick().campaigns == 0
    holder = world.row("SELECT holder_id FROM campaign_runner_lease")["holder_id"]
    assert holder == "runner-a"
    campaign = _campaign(world)
    rival = CampaignRunner(world.env.state, holder_id="runner-b", lease_seconds=30)
    report = rival.tick()
    assert report.actions == () and report.campaigns == 0
    assert world.env.intents(campaign["id"]) == []
    later = CampaignRunner(
        world.env.state,
        holder_id="runner-b",
        lease_seconds=30,
        now=lambda: datetime.now(UTC) + timedelta(seconds=120),
    )
    assert later.tick().campaigns == 1
    assert world.row("SELECT holder_id FROM campaign_runner_lease")["holder_id"] == "runner-b"
    assert first.tick().campaigns == 0  # the old holder yields while the lease is live


def test_background_runner_ticks_and_stops(tmp_path):
    world = World(tmp_path)
    handle = start_background(world.env.state, 0.05)
    try:
        for _ in range(100):
            if world.row("SELECT holder_id FROM campaign_runner_lease"):
                break
            time.sleep(0.05)
        assert world.row("SELECT holder_id FROM campaign_runner_lease")["holder_id"].startswith(
            "runner_"
        )
    finally:
        handle.stop(5.0)
    assert not handle._thread.is_alive()


def test_the_service_loop_backs_off_on_a_persistent_database_error(tmp_path, monkeypatch, caplog):
    import sqlite3
    import threading

    from tessera_control import campaign_runner
    from tessera_control.campaign_runner import backoff_seconds

    assert backoff_seconds(2, 0) == 2
    waits = [backoff_seconds(2, n) for n in range(1, 12)]
    assert waits == sorted(waits) and waits[0] > 2 and max(waits) == 60.0

    recorded: list[float] = []
    done = threading.Event()

    class Failing:
        def __init__(self, state_dir, **_):
            pass

        def tick(self):
            raise sqlite3.OperationalError("no such table: campaigns")

    class FakeStop(threading.Event):
        def wait(self, timeout=None):
            if timeout is None:
                return super().wait(timeout)
            recorded.append(timeout)
            if len(recorded) >= 4:
                self.set()
                done.set()
            return self.is_set()

    monkeypatch.setattr(campaign_runner, "CampaignRunner", Failing)
    monkeypatch.setattr(campaign_runner.threading, "Event", FakeStop)
    with caplog.at_level("ERROR"):
        handle = campaign_runner.start_background(tmp_path, 2)
        assert done.wait(5)
        handle.stop()
    assert recorded == sorted(recorded) and recorded[0] > 2 and len(set(recorded)) == 4
    assert any("in a row" in r.message and "OperationalError" in r.message for r in caplog.records)
