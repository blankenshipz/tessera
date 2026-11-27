"""The runner breaker: a campaign that keeps raising one error class is blocked (ADR 0210).

Reduce-only: the block leaves every attempt, grant and hold as it was. A clean drive resets the
count, control flow is never counted, a runner restart does not lift the block and a person's
re-approval does. Real stores and runner on temporary state; errors are simulated by patching
one runner method.
"""

from __future__ import annotations

import sqlite3

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_providers import Waiting as _Waiting
from tessera_control.campaign_runner import (
    CANCEL_RUNNER_ERROR_EVENT,
    RUNNER_ERROR_EVENT,
    RUNNER_ERROR_THRESHOLD,
    CampaignRunner,
)
from tessera_control.campaign_steps import StepTransient


def _reserved(world: World) -> tuple[str, dict]:
    """A one-stage campaign whose attempt holds its reservation."""
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1")]
    cid = world.campaign(spec)["id"]
    world.tick_until(lambda: bool(world.attempts(cid)), worker=False)
    return cid, _money(world, cid)


def _money(world: World, cid: str) -> dict:
    campaign = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)
    return {
        "campaign": dict(campaign),
        "attempts": [
            (row["id"], row["status"], row["grant_id"], row["settled_cents"])
            for row in world.attempts(cid)
        ],
        "grants": world.env.query(
            "SELECT id, status FROM execution_grants WHERE campaign_id=?", cid
        )
        if _has_campaign_column(world)
        else [],
    }


def _has_campaign_column(world: World) -> bool:
    columns = world.env.query("PRAGMA table_info(execution_grants)")
    return any(column["name"] == "campaign_id" for column in columns)


def _raise_in_stage(monkeypatch, error: BaseException) -> None:
    def raising(self, ctx, key):
        raise error

    monkeypatch.setattr(CampaignRunner, "_advance_stage", raising)


def _campaign(world: World, cid: str):
    return world.row("SELECT status, stop_reason FROM campaigns WHERE id=?", cid)


def test_identical_errors_block_the_campaign_once_with_holds_untouched(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, before = _reserved(world)
    _raise_in_stage(monkeypatch, KeyError("synthetic"))
    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        world.runner.tick()
    assert _campaign(world, cid)["status"] == "running"
    report = world.runner.tick()
    assert f"blocked:{cid}:runner_error:KeyError" in report.actions
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", "runner_error:KeyError")
    for _ in range(RUNNER_ERROR_THRESHOLD + 2):
        world.runner.tick()
    events = [e["payload"] for e in world.events_of(RUNNER_ERROR_EVENT)]
    assert events == [
        {
            "campaign_id": cid,
            "error_class": "KeyError",
            "consecutive": RUNNER_ERROR_THRESHOLD,
            "threshold": RUNNER_ERROR_THRESHOLD,
        }
    ]
    blocked = [e["payload"] for e in world.events_of("campaign.blocked")]
    assert blocked == [{"campaign_id": cid, "reason": "runner_error", "error_class": "KeyError"}]
    # Nothing settled, cancelled or released: the hold waits for a person.
    assert _money(world, cid) == before
    assert _campaign(world, cid)["status"] == "blocked"


def test_a_tick_level_error_counts_too(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _ = _reserved(world)

    def raising(self, ctx):
        raise ValueError("synthetic")

    monkeypatch.setattr(CampaignRunner, "_drive_running", raising)
    for _ in range(RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", "runner_error:ValueError")


def test_a_clean_drive_resets_the_streak(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    original = CampaignRunner._advance_stage
    clean = {"on": False}

    def raising(self, ctx, key):
        if clean["on"]:
            return original(self, ctx, key)
        raise KeyError("synthetic")

    monkeypatch.setattr(CampaignRunner, "_advance_stage", raising)
    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        world.runner.tick()
    clean["on"] = True
    world.runner.tick()
    clean["on"] = False
    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        world.runner.tick()
    assert _campaign(world, cid)["status"] == "running"
    assert world.events_of(RUNNER_ERROR_EVENT) == []


def test_a_different_class_is_a_different_streak(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    errors = iter([KeyError, ValueError] * RUNNER_ERROR_THRESHOLD)

    def raising(self, ctx, key):
        raise next(errors)("synthetic")

    monkeypatch.setattr(CampaignRunner, "_advance_stage", raising)
    for _ in range(RUNNER_ERROR_THRESHOLD + 2):
        world.runner.tick()
    assert _campaign(world, cid)["status"] == "running"


@pytest.mark.parametrize(
    "error",
    [StepTransient("issue_grant: OSError"), sqlite3.OperationalError("database is locked")],
    ids=["step_transient", "busy_database"],
)
def test_control_flow_is_never_counted(tmp_path, monkeypatch, error):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    _raise_in_stage(monkeypatch, error)
    for _ in range(RUNNER_ERROR_THRESHOLD + 3):
        report = world.runner.tick()
        assert f"deferred:{cid}" in report.actions
    assert _campaign(world, cid)["status"] == "running"
    assert world.events_of(RUNNER_ERROR_EVENT) == []


def test_restart_keeps_the_block_and_re_approval_resumes(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    with monkeypatch.context() as patch:
        _raise_in_stage(patch, KeyError("synthetic"))
        for _ in range(RUNNER_ERROR_THRESHOLD):
            world.runner.tick()
    assert _campaign(world, cid)["status"] == "blocked"
    # The fix is deployed and the process restarts: the block stands until a person acts.
    world.restart_runner()
    for _ in range(3):
        world.runner.tick()
    assert _campaign(world, cid)["status"] == "blocked"
    assert world.events_of("campaign.unblocked") == []
    current = dict(world.row("SELECT * FROM campaigns WHERE id=?", cid))
    approved = world.env.approve(current)
    assert approved["status"] == "blocked"  # re-approval itself never resumes forward work
    world.runner.tick()
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("running", None)
    (unblocked,) = [e["payload"] for e in world.events_of("campaign.unblocked")]
    assert unblocked["cleared_stop_reason"] == "runner_error:KeyError"
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)


def test_a_deterministic_reduce_only_replay_trips_the_breaker(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, before = _reserved(world)
    _raise_in_stage(
        monkeypatch,
        StepTransient("revoke_grant: ValueError", deterministic=True, error_class="ValueError"),
    )
    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        report = world.runner.tick()
        assert f"deferred:{cid}" in report.actions
    assert _campaign(world, cid)["status"] == "running"
    world.runner.tick()
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", "runner_error:ValueError")
    assert len(world.events_of(RUNNER_ERROR_EVENT)) == 1
    assert _money(world, cid) == before


def test_an_earlier_stage_error_counts_when_a_later_stage_defers(tmp_path, monkeypatch):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "a"), stage_spec(world.env, "b")]
    cid = world.campaign(spec)["id"]

    def raising(self, ctx, key):
        if key == "a":
            raise KeyError("synthetic")
        raise StepTransient("issue_grant: OperationalError")

    monkeypatch.setattr(CampaignRunner, "_advance_stage", raising)
    for _ in range(RUNNER_ERROR_THRESHOLD):
        report = world.runner.tick()
    assert f"deferred:{cid}" in report.actions
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", "runner_error:KeyError")


def test_waiting_passes_neither_count_nor_clear_the_streak(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    passes = {"n": 0}

    def alternating(self, ctx):
        passes["n"] += 1
        if passes["n"] % 2 == 0:
            raise _Waiting
        raise KeyError("synthetic")

    monkeypatch.setattr(CampaignRunner, "_drive_running", alternating)
    for _ in range(2 * RUNNER_ERROR_THRESHOLD - 2):
        world.runner.tick()
    assert _campaign(world, cid)["status"] == "running"
    world.runner.tick()  # the tenth error pass
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", "runner_error:KeyError")


def test_a_cancelling_campaign_is_never_blocked_but_the_loop_is_recorded_once(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    cid, before = _reserved(world)
    world.cancel({"id": cid})
    assert _campaign(world, cid)["status"] == "cancel_requested"

    def raising(self, ctx):
        raise StepTransient(
            "revoke_grant: ValueError", deterministic=True, error_class="ValueError"
        )

    monkeypatch.setattr(CampaignRunner, "_drive_cancel", raising)
    for _ in range(RUNNER_ERROR_THRESHOLD + 5):
        world.runner.tick()
    assert _campaign(world, cid)["status"] == "cancel_requested"
    assert world.events_of(RUNNER_ERROR_EVENT) == []
    events = [e["payload"] for e in world.events_of(CANCEL_RUNNER_ERROR_EVENT)]
    assert events == [
        {"campaign_id": cid, "error_class": "ValueError", "threshold": RUNNER_ERROR_THRESHOLD}
    ]
    assert _money(world, cid) == before
