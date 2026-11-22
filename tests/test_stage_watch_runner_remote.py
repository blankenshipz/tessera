"""The stage watch runner beside a remote stage, through the generic probe seam. Fakes only."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_stage_watch_runner import (
    OPERATION,
    Clock,
    FakeChannel,
    FakeDocker,
    lease_for,
    make_runner,
)

import tessera_worker.stage_watch_runner as runner_module
from tessera_control.stage_watch_limits import MAX_REPORTS_PER_OPERATION
from tessera_worker.stage_watch_runner import (
    PROVIDER,
    SIGNALS_DIRECTORY,
    WATCH_DIRECTORY,
    RemoteOperationInfo,
    StageWatchRunner,
    probe_factory,
    register_probe_factory,
    registered_providers,
    snapshot_file_probe_factory,
    unregister_probe_factory,
)
from tessera_worker.stage_watcher import write_signal_snapshot

REMOTE = "remote-test"
OP = "f" * 32
INFO = RemoteOperationInfo(
    operation_id=OP,
    lease_id="lease-r",
    epoch=7,
    stage_key="remote-gpu",
    provider=REMOTE,
    deadline_seconds=7200,
    plan_digest="9" * 64,
)
MAX_AGE = 120


@pytest.fixture
def kill_switch(tmp_path: Path) -> str:
    directory = tmp_path / "operator"
    directory.mkdir()
    return str(directory / "stop-agents")


@pytest.fixture(autouse=True)
def provider() -> Iterator[None]:
    register_probe_factory(REMOTE, snapshot_file_probe_factory(None, MAX_AGE))
    try:
        yield
    finally:
        unregister_probe_factory(REMOTE)


def runner_for(
    state: Path,
    channel: FakeChannel,
    clock: Clock,
    kill_switch: str | None,
    docker: FakeDocker | None = None,
    **config: Any,
) -> StageWatchRunner:
    return make_runner(state, channel, docker or FakeDocker(), clock, kill_switch, **config)


def signal(runner: StageWatchRunner, clock: Clock, operation_id: str = OP, **values: Any) -> None:
    path = runner.signals_path(operation_id)
    assert path is not None
    write_signal_snapshot(path, values, clock=clock)


def watcher(runner: StageWatchRunner, operation_id: str = OP):
    return runner._entries[operation_id].watcher


# -- registry --------------------------------------------------------------------------------


def test_registry_reserves_local_docker_and_validates() -> None:
    assert PROVIDER in registered_providers() and REMOTE in registered_providers()
    assert probe_factory(REMOTE) is not None and probe_factory("nowhere") is None
    with pytest.raises(ValueError):
        register_probe_factory(PROVIDER, snapshot_file_probe_factory(None, MAX_AGE))
    with pytest.raises(ValueError):
        register_probe_factory("Bad Provider", snapshot_file_probe_factory(None, MAX_AGE))
    with pytest.raises(ValueError):
        register_probe_factory("ok", "not callable")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dataclasses.replace(INFO, plan_digest="nope")
    with pytest.raises(ValueError):
        dataclasses.replace(INFO, deadline_seconds=0)
    assert {field.name for field in dataclasses.fields(RemoteOperationInfo)} == {
        "operation_id",
        "lease_id",
        "epoch",
        "stage_key",
        "provider",
        "deadline_seconds",
        "plan_digest",
    }


# -- remote observe and report ---------------------------------------------------------------


def test_running_snapshot_reports_heartbeat_and_progress(tmp_path: Path, kill_switch: str) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    runner.on_start_remote(INFO)
    runner.on_start_remote(INFO)  # idempotent
    assert runner.active_operations == (OP,)
    contract = watcher(runner)._contract
    assert (contract.provider, contract.stage_key) == (REMOTE, "remote-gpu")
    assert (contract.lease_id, contract.epoch) == ("lease-r", 7)
    assert contract.deadline_seconds == 7200
    assert contract.max_reports <= MAX_REPORTS_PER_OPERATION
    assert contract.kill_switch_path == kill_switch
    signal(runner, clock, state="running", progress_marker=1)
    runner.tick()
    clock.advance(31)
    signal(runner, clock, state="running", progress_marker=2)
    runner.tick()
    assert channel.kinds() == ["heartbeat", "progress"]
    for seq, (path, body, key, timeout) in enumerate(channel.posts):
        assert path == f"/worker/operations/{OP}/watch-reports"
        assert key == f"watch-{OP}-{seq}"
        assert body["seq"] == seq and body["lease_id"] == "lease-r"
        assert body["expected_epoch"] == 7 and body["kill_switch_engaged"] is False
        config = runner._config
        assert 0 < timeout <= min(config.delivery_timeout_seconds, config.tick_budget_seconds)
    status = runner.status()
    assert (status["watching_remote"], status["watching_local"]) == (1, 0)
    assert runner.errors == 0


def test_flat_marker_is_stuck_once(tmp_path: Path, kill_switch: str) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch, silent_seconds=100)
    runner.on_start_remote(INFO)
    for _ in range(7):
        signal(runner, clock, state="running", progress_marker=5)
        runner.tick()
        clock.advance(50)
    assert channel.kinds().count("stuck") == 1
    assert channel.kinds() == ["heartbeat", "stuck", "heartbeat", "heartbeat"]
    assert not watcher(runner).done


@pytest.mark.parametrize("cause", ["stale", "missing"])
def test_stale_or_missing_snapshot_fails_after_three_unknowns(
    tmp_path: Path, kill_switch: str, cause: str
) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    runner.on_start_remote(INFO)
    if cause == "stale":
        signal(runner, clock, state="running", progress_marker=1)
        runner.tick()
        assert channel.kinds() == ["heartbeat"]
        clock.advance(MAX_AGE + 1)
    for _ in range(2):
        runner.tick()
        clock.advance(1)
    assert "failed" not in channel.kinds()  # a missed read is never a failure
    runner.tick()
    assert channel.kinds()[-1] == "failed" and watcher(runner).outcome == "failed"


def test_exit_code_fails_or_ends(tmp_path: Path, kill_switch: str) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    runner.on_start_remote(INFO)
    signal(runner, clock, state="exited", exit_code=3)
    runner.tick()
    assert channel.kinds() == ["failed"]

    other = dataclasses.replace(INFO, operation_id="a" * 32)
    runner.on_start_remote(other)
    signal(runner, clock, other.operation_id, state="exited", exit_code=0)
    runner.tick()
    assert watcher(runner, other.operation_id).outcome == "exited_ok"
    assert channel.kinds() == ["failed"]  # a clean exit has no report
    runner.on_end(OP)
    runner.on_end(other.operation_id)
    runner.tick()
    assert runner.active_operations == ()


def test_on_end_flushes_until_finished(tmp_path: Path, kill_switch: str) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    runner.on_start_remote(INFO)
    channel.fail_kinds["failed"] = 2
    signal(runner, clock, state="exited", exit_code=1)
    runner.tick()
    runner.on_end(OP)
    assert watcher(runner).done and not watcher(runner).finished
    runner.tick()
    assert runner.active_operations == (OP,)
    runner.tick()
    assert runner.active_operations == ()
    assert channel.kinds() == ["failed"] * 3
    assert {key for _, _, key, _ in channel.posts} == {f"watch-{OP}-0"}


# -- refusals --------------------------------------------------------------------------------


def test_unknown_provider_is_refused_and_counted(tmp_path: Path, kill_switch: str) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    unknown = dataclasses.replace(INFO, provider="nowhere")
    runner.on_start_remote(unknown)
    runner.on_start_remote(unknown)
    # Local Docker is the runner's own path, never a remote one.
    runner.on_start_remote(dataclasses.replace(INFO, operation_id="a" * 32, provider=PROVIDER))
    runner.tick()
    assert runner.active_operations == ()
    assert runner.refused_unknown_provider == 2
    assert runner.status()["refused_unknown_provider"] == 2
    assert runner.errors == 0 and channel.posts == []
    runner.on_start_remote(object())  # type: ignore[arg-type]  # never raises
    assert runner.errors == 1


def test_missing_kill_switch_path_refuses_remote(tmp_path: Path) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, None)
    runner.on_start_remote(INFO)
    runner.on_start_remote(INFO)
    runner.tick()
    assert runner.active_operations == ()
    assert runner.refused_no_kill_switch == 1 and runner.refused_unknown_provider == 0
    assert channel.posts == []
    assert list((tmp_path / WATCH_DIRECTORY).iterdir()) == []


def test_engaged_kill_switch_still_reports_and_grants_nothing(
    tmp_path: Path, kill_switch: str
) -> None:
    Path(kill_switch).write_text("")
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    runner.on_start_remote(INFO)
    signal(runner, clock, state="running", progress_marker=1)
    runner.tick()
    assert channel.kinds() == ["heartbeat"]
    assert channel.posts[0][1]["kill_switch_engaged"] is True
    assert watcher(runner).may_request() is False
    assert runner.status()["kill_switch_engaged"] is True
    for name in ("may_request", "request", "request_retry", "request_resize", "request_reroute"):
        assert not hasattr(runner, name)


def test_factory_or_watcher_exceptions_never_propagate(tmp_path: Path, kill_switch: str) -> None:
    def broken(*_: Any) -> Any:
        raise RuntimeError("factory bug")

    register_probe_factory("broken", broken)
    try:
        clock, channel = Clock(), FakeChannel()
        runner = runner_for(tmp_path, channel, clock, kill_switch)
        runner.on_start_remote(dataclasses.replace(INFO, provider="broken"))
        assert runner.errors == 1 and runner.active_operations == ()
        runner.on_start_remote(dataclasses.replace(INFO, operation_id="a" * 32))

        def explode() -> None:
            raise RuntimeError("watcher bug")

        watcher(runner, "a" * 32).tick = explode  # type: ignore[method-assign]
        runner.tick()
        runner.close()
        assert runner.errors == 2
    finally:
        unregister_probe_factory("broken")


# -- restart ---------------------------------------------------------------------------------


def test_restart_resumes_pending_remote_outboxes(tmp_path: Path, kill_switch: str) -> None:
    clock = Clock()
    down = FakeChannel()
    down.fail_all = True
    first = runner_for(tmp_path, down, clock, kill_switch)
    first.on_start_remote(INFO)
    signal(first, clock, state="running", progress_marker=1)
    first.tick()
    clock.advance(5)
    signal(first, clock, state="exited", exit_code=2)
    first.on_end(OP)
    first.tick()
    assert watcher(first).pending_count == 2
    first.close()
    signals = first.signals_path(OP)
    assert signals is not None
    signals.unlink()  # the provider-side snapshot is gone after the restart

    up = FakeChannel()
    second = runner_for(tmp_path, up, clock, kill_switch)
    assert second.active_operations == (OP,)
    assert second.status()["watching_remote"] == 1
    second.tick()
    assert up.kinds() == ["failed", "heartbeat"]  # closing first, flush only
    assert {key for _, _, key, _ in up.posts} == {f"watch-{OP}-0", f"watch-{OP}-1"}
    assert second.active_operations == ()
    assert runner_for(tmp_path, up, clock, kill_switch).active_operations == ()


def test_recovered_remote_operation_keeps_its_contract(tmp_path: Path, kill_switch: str) -> None:
    clock = Clock()
    down = FakeChannel()
    down.fail_all = True
    first = runner_for(tmp_path, down, clock, kill_switch)
    first.on_start_remote(INFO)
    signal(first, clock, state="running", progress_marker=1)
    first.tick()
    digest = watcher(first).contract_digest
    first.close()

    up = FakeChannel()
    second = runner_for(tmp_path, up, clock, kill_switch, heartbeat_seconds=120)
    assert second._entries[OP].resumed is True
    second.on_start_remote(INFO)
    assert second._entries[OP].resumed is False
    assert watcher(second).contract_digest == digest
    clock.advance(31)
    signal(second, clock, state="running", progress_marker=2)
    second.tick()  # the pending heartbeat is delivered; this sample sets the mark
    assert up.kinds() == ["heartbeat"]
    clock.advance(31)
    signal(second, clock, state="running", progress_marker=3)
    second.tick()  # sampled again through the provider's probe
    assert up.kinds() == ["heartbeat", "progress"]
    assert second.errors == 0

    # Another attempt (epoch) for the same operation is refused, never raised.
    third = runner_for(tmp_path, FakeChannel(), clock, kill_switch)
    third.on_start_remote(dataclasses.replace(INFO, epoch=8))
    assert third.errors == 1 and third.active_operations == ()


# -- the local path and the surface ----------------------------------------------------------


def test_local_path_is_unchanged_beside_a_remote_one(tmp_path: Path, kill_switch: str) -> None:
    clock, channel, docker = Clock(), FakeChannel(), FakeDocker()
    runner = runner_for(tmp_path, channel, clock, kill_switch, docker)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    runner.on_start(OPERATION, lease_for(clock))
    runner.on_start_remote(INFO)
    signal(runner, clock, state="running", progress_marker=1)
    runner.tick()
    local = watcher(runner, OPERATION.operation_id)._contract
    assert (local.provider, local.stage_key) == (PROVIDER, "local-cpu")
    status = runner.status()
    assert (status["watching_local"], status["watching_remote"]) == (1, 1)
    paths = sorted(path for path, _, _, _ in channel.posts)
    assert paths == sorted(
        f"/worker/operations/{op}/watch-reports" for op in (OPERATION.operation_id, OP)
    )
    # The Docker probe only ever inspected the local container.
    assert {call[3] for call in docker.calls} == {OPERATION.container_name}
    # A remote operation is never sampled through the local Docker hook.
    runner.on_start(SimpleNamespace(operation_id=OP), lease_for(clock))
    assert runner._entries[OP].remote and runner.errors == 0


def test_signals_directory_is_owner_only_and_lazy(tmp_path: Path, kill_switch: str) -> None:
    previous = os.umask(0o002)
    try:
        runner = runner_for(tmp_path, FakeChannel(), Clock(), kill_switch)
        directory = tmp_path / WATCH_DIRECTORY / SIGNALS_DIRECTORY
        assert not directory.exists()
        path = runner.signals_path(OP)
    finally:
        os.umask(previous)
    assert path == directory / f"{OP}.signals.json"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert runner.signals_path("../etc") is None
    assert runner.signals_path(OP.upper()) is None
    linked = tmp_path / "linked"
    (linked / WATCH_DIRECTORY).mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (linked / WATCH_DIRECTORY / SIGNALS_DIRECTORY).symlink_to(tmp_path / "elsewhere")
    refused = runner_for(linked, FakeChannel(), Clock(), kill_switch)
    assert refused.signals_path(OP) is None and refused.errors == 1


def test_surface_has_no_stop_cancel_or_credential(tmp_path: Path, kill_switch: str) -> None:
    clock, channel = Clock(), FakeChannel()
    runner = runner_for(tmp_path, channel, clock, kill_switch)
    runner.on_start_remote(INFO)
    forbidden = (
        "stop",
        "cancel",
        "retry",
        "resize",
        "reroute",
        "re_route",
        "restart",
        "remove",
        "credential",
        "token",
        "secret",
        "management",
    )
    public = {name for name in dir(runner) if not name.startswith("_")}
    public |= {name for name in dir(runner_module) if not name.startswith("_")}
    for name in public:
        assert not any(word in name.lower() for word in forbidden), name
    assert runner._channel is channel
    status = json.dumps(runner.status())
    for secret in (OP, "lease-r", kill_switch, str(tmp_path)):
        assert secret not in status
