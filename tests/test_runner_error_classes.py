"""The runner's error table: classification, the step bounds and the host-fault pause (ADR 0210).

``runner_faults.classify`` decides what a store or filesystem error means. A missing file fails
the smallest unit once; a full disk raises one process-wide alarm and pauses the runner without
counting or blocking any campaign; a brief I/O error that never clears becomes a host fault.
Real stores, executor and runner on temporary state; errors are simulated by patching one
runner method or one prepared store call, and the runner and executor clocks are injected.
"""

from __future__ import annotations

import dataclasses
import errno
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import URLError

import pytest
from campaign_runner_harness import World, stage_spec
from fastapi.testclient import TestClient
from test_campaign_runner import METRIC, _spec

from tessera_control import campaign_steps, runner_faults
from tessera_control.api import create_app
from tessera_control.campaign_runner import (
    RUNNER_ERROR_EVENT,
    RUNNER_ERROR_THRESHOLD,
    CampaignRunner,
)
from tessera_control.campaign_steps import StepExecutor, StepFailed, StepTransient
from tessera_control.runner_faults import (
    BACKOFF_AFTER_REPLAYS,
    HOST_ALARM,
    HOST_PAUSE_SECONDS,
    IO_STREAK_LIMIT,
    TRANSIENT_REPLAY_LIMIT,
    HostFault,
    classify,
    replay_count,
    replay_marker,
)
from tessera_control.store import initialize_state


@pytest.fixture(autouse=True)
def _clean_alarm():
    """The host alarm is process-wide: no test may inherit or leak one."""
    HOST_ALARM.clear()
    yield
    HOST_ALARM.clear()


class Clock:
    """A settable runner clock."""

    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def _world(tmp_path, stages: int = 1) -> tuple[World, Clock, str]:
    world = World(tmp_path)
    clock = Clock()
    world.runner.now = clock
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, key) for key in "ab"[:stages]]
    return world, clock, world.campaign(spec)["id"]


def _raise_in_stage(monkeypatch, error: BaseException, only: str | None = None) -> list[str]:
    """Patch stage advancement to raise ``error`` (once, for stage ``only``; else every stage)."""
    original = CampaignRunner._advance_stage
    seen: list[str] = []

    def raising(self, ctx, key):
        if only is not None and (key != only or seen):
            # A stage that already failed closed is not advanced past the fault again.
            return original(self, ctx, key)
        seen.append(key)
        raise error

    monkeypatch.setattr(CampaignRunner, "_advance_stage", raising)
    return seen


def _campaign(world: World, cid: str):
    return world.row("SELECT status, stop_reason FROM campaigns WHERE id=?", cid)


# -- the table -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (sqlite3.OperationalError("database is locked"), "contention"),
        (sqlite3.OperationalError("database table is locked"), "contention"),
        (sqlite3.OperationalError("database is busy"), "contention"),
        (sqlite3.OperationalError("disk I/O error"), "io"),
        (OSError(errno.EIO, "io"), "io"),
        (sqlite3.OperationalError("database or disk is full"), "environment"),
        (sqlite3.OperationalError("attempt to write a readonly database"), "environment"),
        (OSError(errno.ENOSPC, "full"), "environment"),
        (OSError(errno.EDQUOT, "quota"), "environment"),
        (OSError(errno.EROFS, "read-only"), "environment"),
        (OSError(errno.EACCES, "denied"), "deterministic"),
        (OSError(errno.EPERM, "denied"), "deterministic"),
        (OSError(errno.EMFILE, "fds"), "environment"),
        (OSError(errno.ENFILE, "fds"), "environment"),
        (OSError(errno.ENOMEM, "memory"), "environment"),
        (HostFault("ENOSPC", "OSError"), "environment"),
        (FileNotFoundError(errno.ENOENT, "gone"), "deterministic"),
        (OSError(errno.ENOTDIR, "not a dir"), "deterministic"),
        (OSError(errno.EISDIR, "a dir"), "deterministic"),
        (OSError(errno.ELOOP, "loop"), "deterministic"),
        (TimeoutError("slow"), "transient"),
        (ConnectionError("reset"), "transient"),
        (OSError(errno.EAGAIN, "again"), "transient"),
        (OSError(errno.EINTR, "interrupted"), "transient"),
        (OSError(errno.EBUSY, "busy"), "transient"),
        (OSError(errno.ETIMEDOUT, "timed out"), "transient"),
        (OSError("no errno"), "deterministic"),
        (OSError(errno.EEXIST, "exists"), "deterministic"),
        (OSError(errno.EXDEV, "cross-device"), "deterministic"),
        (OSError(errno.EINVAL, "invalid"), "deterministic"),
        (URLError("unreachable"), "deterministic"),
        (ValueError("bad"), "deterministic"),
        (KeyError("missing"), "deterministic"),
        (sqlite3.OperationalError("no such table: nope"), "deterministic"),
    ],
)
def test_classify_table(error, expected):
    assert classify(error) == expected


def test_replay_marker_round_trips_and_ignores_other_values():
    assert replay_count(replay_marker(7, "TimeoutError")) == 7
    assert replay_count(None) == 0
    assert replay_count("ValueError") == 0
    assert replay_count("transient:x:ValueError") == 0


# -- the step executor ----------------------------------------------------------------


def _capture(world: World, monkeypatch, target: str) -> dict:
    """Tick the real runner until it first reaches ``target``; return that slot's kwargs."""
    seen: dict = {}
    original = StepExecutor.run

    def spy(self, **kwargs):
        if kwargs["step"] == target and not seen:
            seen.update(kwargs)
            raise StepTransient("captured")
        return original(self, **kwargs)

    monkeypatch.setattr(StepExecutor, "run", spy)
    for _ in range(8):
        world.runner.tick()
        if seen:
            break
    monkeypatch.setattr(StepExecutor, "run", original)
    assert seen, f"{target} was never reached"
    return seen


def _step_world(tmp_path, monkeypatch) -> tuple[World, str, dict]:
    world = World(tmp_path)
    spec = _spec(world, [stage_spec(world.env, "s1", metrics=METRIC)])
    cid = world.campaign(spec)["id"]
    return world, cid, _capture(world, monkeypatch, "issue_grant")


def _slot(world: World, cid: str) -> dict:
    (intent,) = world.env.query(
        "SELECT * FROM campaign_step_intents WHERE campaign_id=? AND step='issue_grant'", cid
    )
    return intent


def _raising(monkeypatch, error: Exception) -> list[int]:
    """Make every prepared store call raise ``error``; the list counts the calls."""
    real_prepare = campaign_steps.prepare
    calls: list[int] = []

    def patched(*args, **kw):
        prepared = real_prepare(*args, **kw)

        def call(principal, key):
            calls.append(1)
            raise error

        return dataclasses.replace(prepared, call=call)

    monkeypatch.setattr(campaign_steps, "prepare", patched)
    return calls


def _executor_clock(world: World) -> list[float]:
    now = [1000.0]
    world.runner.steps.monotonic = lambda: now[0]
    return now


def test_environment_error_from_a_step_raises_a_host_fault_and_leaves_the_intent_pending(
    tmp_path, monkeypatch
):
    world, cid, kwargs = _step_world(tmp_path, monkeypatch)
    failed_before = len(world.events_of("campaign.step_failed"))
    _raising(monkeypatch, OSError(errno.ENOSPC, "No space left on device"))
    with pytest.raises(HostFault) as raised:
        world.runner.steps.run(**kwargs)
    assert raised.value.kind == "ENOSPC"
    intent = _slot(world, cid)
    assert intent["status"] == "pending" and intent["error_class"] is None
    assert len(world.events_of("campaign.step_failed")) == failed_before


def test_deterministic_missing_file_fails_the_forward_intent_once(tmp_path, monkeypatch):
    world, cid, kwargs = _step_world(tmp_path, monkeypatch)
    failed_before = len(world.events_of("campaign.step_failed"))
    calls = _raising(monkeypatch, FileNotFoundError(errno.ENOENT, "gone"))
    with pytest.raises(StepFailed) as raised:
        world.runner.steps.run(**kwargs)
    assert raised.value.error_class == "FileNotFoundError"
    intent = _slot(world, cid)
    assert intent["status"] == "failed" and intent["error_class"] == "FileNotFoundError"
    with pytest.raises(StepFailed):
        world.runner.steps.run(**kwargs)
    assert len(calls) == 1
    assert len(world.events_of("campaign.step_failed")) == failed_before + 1


def test_transient_replays_persist_a_count_and_back_off(tmp_path, monkeypatch):
    world, cid, kwargs = _step_world(tmp_path, monkeypatch)
    now = _executor_clock(world)
    calls = _raising(monkeypatch, TimeoutError("slow"))
    for count in range(1, BACKOFF_AFTER_REPLAYS + 1):
        with pytest.raises(StepTransient) as raised:
            world.runner.steps.run(**kwargs)
        assert raised.value.kind == "transient"
        intent = _slot(world, cid)
        assert intent["status"] == "pending"
        assert intent["error_class"] == f"transient:{count}:TimeoutError"
    assert len(calls) == BACKOFF_AFTER_REPLAYS

    # The next replay waits: the store is not called until the clock passes the delay.
    with pytest.raises(StepTransient, match="replay backing off"):
        world.runner.steps.run(**kwargs)
    assert len(calls) == BACKOFF_AFTER_REPLAYS
    now[0] += 3600
    with pytest.raises(StepTransient) as raised:
        world.runner.steps.run(**kwargs)
    assert "backing off" not in str(raised.value)
    assert len(calls) == BACKOFF_AFTER_REPLAYS + 1
    assert _slot(world, cid)["error_class"] == f"transient:{BACKOFF_AFTER_REPLAYS + 1}:TimeoutError"


def test_a_transient_forward_step_fails_its_intent_past_the_replay_bound(tmp_path, monkeypatch):
    world, cid, kwargs = _step_world(tmp_path, monkeypatch)
    now = _executor_clock(world)
    failed_before = len(world.events_of("campaign.step_failed"))
    calls = _raising(monkeypatch, TimeoutError("slow"))
    for _ in range(TRANSIENT_REPLAY_LIMIT):
        now[0] += 3600
        with pytest.raises(StepTransient):
            world.runner.steps.run(**kwargs)
    assert _slot(world, cid)["status"] == "pending"
    now[0] += 3600
    with pytest.raises(StepFailed) as raised:
        world.runner.steps.run(**kwargs)
    assert raised.value.error_class == "TimeoutError"
    assert raised.value.detail == "transient replay bound reached"
    intent = _slot(world, cid)
    assert intent["status"] == "failed" and intent["error_class"] == "TimeoutError"
    assert len(calls) == TRANSIENT_REPLAY_LIMIT + 1
    assert len(world.events_of("campaign.step_failed")) == failed_before + 1


def test_a_brief_io_error_that_never_clears_becomes_a_host_fault_not_a_step_failure(
    tmp_path, monkeypatch
):
    world, cid, kwargs = _step_world(tmp_path, monkeypatch)
    now = _executor_clock(world)
    failed_before = len(world.events_of("campaign.step_failed"))
    _raising(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    for _ in range(TRANSIENT_REPLAY_LIMIT):
        now[0] += 3600
        with pytest.raises(StepTransient) as raised:
            world.runner.steps.run(**kwargs)
        assert raised.value.kind == "io"
    now[0] += 3600
    with pytest.raises(HostFault) as fault:
        world.runner.steps.run(**kwargs)
    assert fault.value.kind == "disk_io_error"
    assert _slot(world, cid)["status"] == "pending"
    assert len(world.events_of("campaign.step_failed")) == failed_before


# -- the runner ------------------------------------------------------------------------


def test_a_missing_blob_fails_the_stage_once_while_a_sibling_keeps_advancing(tmp_path, monkeypatch):
    world, _, cid = _world(tmp_path, stages=2)
    blob = world.runner.state_dir / "artifacts" / "ab" / ("ab" * 32)
    seen = _raise_in_stage(
        monkeypatch, FileNotFoundError(errno.ENOENT, "gone", str(blob)), only="b"
    )
    actions: list[str] = []
    for _ in range(RUNNER_ERROR_THRESHOLD + 2):
        actions.extend(world.runner.tick().actions)
        world.poller.run(max_cycles=1)
    assert actions.count("stage_failed:b:os_error:ENOENT") == 1
    assert world.stages(cid)["b"] == "failed"
    assert seen, "the failing stage was reached"
    # The sibling, advanced before the failing stage in the same pass, got its attempt.
    assert any(row["stage_key"] == "a" for row in world.attempts(cid))
    assert world.events_of(RUNNER_ERROR_EVENT) == []
    assert HOST_ALARM.active() is None


def test_a_full_disk_raises_one_alarm_and_never_trips_a_campaign_breaker(tmp_path, monkeypatch):
    world, clock, cid = _world(tmp_path)
    with monkeypatch.context() as patch:
        _raise_in_stage(patch, OSError(errno.ENOSPC, "No space left on device"))
        first = world.runner.tick()
        assert "host_fault:ENOSPC" in first.actions
        alarm = HOST_ALARM.active()
        assert alarm is not None and alarm["kind"] == "ENOSPC" and alarm["count"] == 1
        for _ in range(3 * RUNNER_ERROR_THRESHOLD):
            assert world.runner.tick().actions == ("host_paused",)
        assert HOST_ALARM.active()["count"] == 1
        assert HOST_ALARM.active() is not None
        assert _campaign(world, cid)["status"] == "running"
        assert world.events_of(RUNNER_ERROR_EVENT) == []
        assert world.events_of("campaign.runner_error") == []

        # The pause elapses while the fault stands: the alarm is raised again, not duplicated.
        clock.advance(HOST_PAUSE_SECONDS + 1)
        assert "host_fault:ENOSPC" in world.runner.tick().actions
        assert _campaign(world, cid)["status"] == "running"
        assert world.events_of(RUNNER_ERROR_EVENT) == []

    # The disk is freed: still paused until the (doubled) pause passes, then a clean tick
    # clears the alarm and the same campaign finishes with no re-approval.
    assert world.runner.tick().actions == ("host_paused",)
    clock.advance(2 * HOST_PAUSE_SECONDS + 1)
    cleared = world.runner.tick()
    assert "host_fault_cleared" in cleared.actions
    assert HOST_ALARM.active() is None
    world.tick_all(cid)
    assert world.status(cid) == "succeeded"
    assert world.events_of("campaign.unblocked") == []


def test_a_full_disk_in_the_tick_body_is_a_host_fault(tmp_path, monkeypatch):
    world, _, cid = _world(tmp_path)

    def full(self):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(CampaignRunner, "_release_expired_grants", full)
    report = world.runner.tick()
    assert "host_fault:database_full" in report.actions
    assert HOST_ALARM.active() is not None
    assert _campaign(world, cid)["status"] == "running"


def test_a_full_disk_in_a_step_pauses_the_runner_and_leaves_the_intent_pending(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    clock = Clock()
    world.runner.now = clock
    spec = _spec(world, [stage_spec(world.env, "s1", metrics=METRIC)])
    cid = world.campaign(spec)["id"]
    _raising(monkeypatch, OSError(errno.ENOSPC, "No space left on device"))
    reports = [world.runner.tick() for _ in range(3)]
    assert any("host_fault:ENOSPC" in report.actions for report in reports)
    assert HOST_ALARM.active() is not None
    assert world.status(cid) == "running"
    assert world.events_of(RUNNER_ERROR_EVENT) == []
    assert world.events_of("campaign.step_failed") == []
    intents = world.env.query("SELECT status, error_class FROM campaign_step_intents")
    assert intents and all(row["status"] == "pending" for row in intents)
    assert all(row["error_class"] is None for row in intents)


def test_a_brief_io_streak_defers_the_campaign_and_only_then_is_a_host_fault(tmp_path, monkeypatch):
    world, clock, cid = _world(tmp_path)
    with monkeypatch.context() as patch:
        _raise_in_stage(patch, sqlite3.OperationalError("disk I/O error"))
        for _ in range(IO_STREAK_LIMIT - 1):
            report = world.runner.tick()
            assert f"deferred:{cid}" in report.actions
            assert not any(a.startswith("host_fault") for a in report.actions)
            assert HOST_ALARM.active() is None
        report = world.runner.tick()
        assert "host_fault:disk_io_error" in report.actions
        assert HOST_ALARM.active() is not None
        assert _campaign(world, cid)["status"] == "running"
        assert world.events_of(RUNNER_ERROR_EVENT) == []
        for _ in range(3 * RUNNER_ERROR_THRESHOLD):
            world.runner.tick()
        assert _campaign(world, cid)["status"] == "running"
        assert world.events_of(RUNNER_ERROR_EVENT) == []
    clock.advance(HOST_PAUSE_SECONDS + 1)
    assert "host_fault_cleared" in world.runner.tick().actions
    assert HOST_ALARM.active() is None


def test_a_clean_tick_resets_the_io_streak(tmp_path, monkeypatch):
    world, _, cid = _world(tmp_path)
    original = CampaignRunner._advance_stage
    broken = {"on": True}

    def flaky(self, ctx, key):
        if broken["on"]:
            raise sqlite3.OperationalError("disk I/O error")
        return original(self, ctx, key)

    monkeypatch.setattr(CampaignRunner, "_advance_stage", flaky)
    for _ in range(IO_STREAK_LIMIT - 1):
        world.runner.tick()
    broken["on"] = False
    world.runner.tick()
    broken["on"] = True
    for _ in range(IO_STREAK_LIMIT - 1):
        report = world.runner.tick()
        assert not any(a.startswith("host_fault") for a in report.actions)
    assert HOST_ALARM.active() is None
    assert _campaign(world, cid)["status"] == "running"


def test_health_reports_the_host_fault_only_while_the_alarm_stands(tmp_path: Path):
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    name, value = state.joinpath("owner.header").read_text().strip().split(": ", 1)

    def alerts() -> list[dict]:
        response = client.get("/operator/health", headers={name: value})
        assert response.status_code == 200
        return response.json()["alerts"]

    alarm = {"code": "runner_host_fault", "severity": "critical"}
    assert alarm not in alerts()
    assert runner_faults.HOST_ALARM.raise_alarm("ENOSPC", "OSError", datetime.now(UTC))
    assert alarm in alerts()
    HOST_ALARM.clear()
    assert alarm not in alerts()
