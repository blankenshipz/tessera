"""The stage watch runner beside the CPU supervisor (ADR 0087, S2 wiring). Fakes only."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tessera_control.stage_watch_limits import MAX_REPORTS_PER_OPERATION
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.cpu import CpuObservation, CpuOperation
from tessera_worker.stage_watch_config import StageWatchConfig
from tessera_worker.stage_watch_runner import (
    PROVIDER,
    WATCH_DIRECTORY,
    StageWatchRunner,
    build_stage_watch_runner,
)
from tessera_worker.supervisor import CpuSupervisor, Lease

IMAGE = "python@sha256:" + "a" * 64
OPERATION = CpuOperation("b" * 32, IMAGE, ("python", "-c", "print(2)"), "c" * 64, 3600)
OTHER = CpuOperation("d" * 32, IMAGE, ("python", "-c", "print(3)"), "e" * 64, 3600)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeDocker:
    """The Docker command runner the adapter and the probe share (`run(args)`)."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, ...]] = []

    def create(self, operation: CpuOperation) -> None:
        self.containers.setdefault(
            operation.container_name,
            {
                "status": "created",
                "exit_code": 0,
                "restarts": 0,
                "labels": {
                    "tessera.operation": operation.operation_id,
                    "tessera.plan": operation.plan_digest,
                },
            },
        )

    def set(self, operation: CpuOperation, **values: Any) -> None:
        self.containers[operation.container_name].update(values)

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if arguments[:3] != ("inspect", "--type", "container"):
            return subprocess.CompletedProcess(arguments, 1, "", "unsupported")
        container = self.containers.get(arguments[3])
        if container is None:
            return subprocess.CompletedProcess(arguments, 1, "", "Error: No such container")
        info = {
            "Config": {"Labels": container["labels"]},
            "State": {
                "Status": container["status"],
                "Running": container["status"] == "running",
                "ExitCode": container["exit_code"],
                "OOMKilled": False,
            },
            "RestartCount": container["restarts"],
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps([info]), "")


class DockerAdapter:
    """A CPU adapter over FakeDocker, with the real adapter's `run` attribute."""

    def __init__(self, docker: FakeDocker) -> None:
        self.run = docker
        self.docker = docker

    def prepare(self, operation: CpuOperation) -> CpuObservation:
        self.docker.create(operation)
        return self.observe(operation)

    def start(self, operation: CpuOperation) -> CpuObservation:
        self.docker.set(operation, status="running")
        return self.observe(operation)

    def observe(self, operation: CpuOperation) -> CpuObservation:
        container = self.docker.containers.get(operation.container_name)
        if container is None:
            return CpuObservation("absent")
        exited = container["status"] == "exited"
        return CpuObservation(
            container["status"], "container", container["exit_code"] if exited else None, exited
        )

    def cancel(self, operation: CpuOperation) -> CpuObservation:
        self.docker.set(operation, status="exited", exit_code=137)
        return self.observe(operation)


class FakeChannel:
    """The worker's control channel: records posts; can fail chosen reports transiently."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any], str, float | None]] = []
        self.fail_kinds: dict[str, int] = {}
        self.fail_all = False

    def post(
        self,
        path: str,
        body: dict[str, Any],
        key: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.posts.append((path, body, key, timeout_seconds))
        if self.fail_all:
            raise ControlChannelError("control transport unavailable")
        if self.fail_kinds.get(body["kind"], 0) > 0:
            self.fail_kinds[body["kind"]] -= 1
            raise ControlChannelError("control rejected request", 503)
        return {"recorded": True}

    def kinds(self) -> list[str]:
        return [body["kind"] for _, body, _, _ in self.posts]


def lease_for(clock: Clock, operation: CpuOperation = OPERATION, **changes: Any) -> Lease:
    values: dict[str, Any] = {
        "id": "lease-1",
        "operation_id": operation.operation_id,
        "plan_digest": operation.plan_digest,
        "epoch": 1,
        "expires_at": clock() + timedelta(hours=2),
        "task_revision": 1,
        "project_id": "project-1",
    }
    values.update(changes)
    return Lease(**values)


@pytest.fixture
def kill_switch(tmp_path: Path) -> str:
    directory = tmp_path / "operator"
    directory.mkdir()
    return str(directory / "stop-agents")


def make_runner(
    state: Path,
    channel: FakeChannel,
    docker: FakeDocker,
    clock: Clock,
    kill_switch: str | None,
    **config: Any,
) -> StageWatchRunner:
    return StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=kill_switch, **config),
        state_dir=state,
        channel=channel,
        docker_run=docker,
        clock=clock,
    )


def make_supervisor(tmp_path: Path, adapter: DockerAdapter, clock: Clock) -> CpuSupervisor:
    return CpuSupervisor(
        tmp_path / "journal", adapter, lambda _: True, now=clock, monotonic=lambda: 0.0
    )


def watcher(runner: StageWatchRunner, operation_id: str = OPERATION.operation_id):
    return runner._entries[operation_id].watcher


# -- off by default ------------------------------------------------------------------------


def test_disabled_by_default_constructs_nothing_and_imports_no_watcher(tmp_path: Path) -> None:
    assert StageWatchConfig().enabled is False
    assert (
        build_stage_watch_runner(
            StageWatchConfig(),
            state_dir=tmp_path,
            channel=FakeChannel(),
            adapter=DockerAdapter(FakeDocker()),
        )
        is None
    )
    assert (
        build_stage_watch_runner(
            None, state_dir=tmp_path, channel=FakeChannel(), adapter=DockerAdapter(FakeDocker())
        )
        is None
    )
    clock = Clock()
    with make_supervisor(tmp_path, DockerAdapter(FakeDocker()), clock) as supervisor:
        assert supervisor.watch_runner is None
    assert not (tmp_path / WATCH_DIRECTORY).exists()
    # The default worker code path never loads the watcher library.
    probe = (
        "import sys, tessera_worker.cli, tessera_worker.service, tessera_worker.supervisor, "
        "tessera_worker.daemon\n"
        "from tessera_worker.cli import parser, _stage_watch\n"
        "arguments = parser().parse_args(['development-run', '--control-url', 'http://x', "
        "'--grant-id', 'g', '--credential-file', '/c', '--workspace-root', '/w', "
        "'--development-output-root', '/o', '--allowed-image', 'i'])\n"
        "assert arguments.stage_watcher is False and _stage_watch(arguments) is None\n"
        "print(sorted(m for m in sys.modules if 'stage_watch' in m))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, env=os.environ
    )
    assert result.stdout.strip() == "[]"


def test_service_attaches_nothing_unless_enabled(tmp_path: Path) -> None:
    from tessera_worker.service import DevelopmentWorkerService

    runtime = SimpleNamespace(supervisor=SimpleNamespace(watch_runner=None))
    adapter = DockerAdapter(FakeDocker())
    for stage_watch in (None, StageWatchConfig()):
        service = SimpleNamespace(
            stage_watch=stage_watch, provider=None, root=tmp_path, channel=FakeChannel()
        )
        DevelopmentWorkerService._attach_stage_watch(service, runtime, adapter)  # type: ignore[arg-type]
        assert runtime.supervisor.watch_runner is None
    remote = SimpleNamespace(
        stage_watch=StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "k")),
        provider=object(),
        root=tmp_path,
        channel=FakeChannel(),
    )
    DevelopmentWorkerService._attach_stage_watch(remote, runtime, adapter)  # type: ignore[arg-type]
    assert runtime.supervisor.watch_runner is None  # a remote provider is not local Docker
    channel = FakeChannel()
    local = SimpleNamespace(
        stage_watch=StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "k")),
        provider=None,
        root=tmp_path,
        channel=channel,
    )
    DevelopmentWorkerService._attach_stage_watch(local, runtime, adapter)  # type: ignore[arg-type]
    attached = runtime.supervisor.watch_runner
    assert isinstance(attached, StageWatchRunner)
    assert attached._channel is channel and attached._docker_run is adapter.run


def test_cli_flags_build_an_enabled_config() -> None:
    from tessera_worker.cli import _stage_watch, parser

    base = [
        "development-run",
        "--control-url",
        "http://x",
        "--grant-id",
        "g",
        "--credential-file",
        "/c",
        "--workspace-root",
        "/w",
        "--development-output-root",
        "/o",
        "--allowed-image",
        "i",
    ]
    arguments = parser().parse_args(
        [*base, "--stage-watcher", "--stage-watcher-kill-switch", "/k/m"]
    )
    config = _stage_watch(arguments)
    assert config == StageWatchConfig(enabled=True, kill_switch_path="/k/m")
    assert config.startable
    unarmed = _stage_watch(parser().parse_args([*base, "--stage-watcher"]))
    assert unarmed is not None and unarmed.enabled and not unarmed.startable
    with pytest.raises(ValueError):
        _stage_watch(
            parser().parse_args([*base, "--stage-watcher", "--stage-watcher-kill-switch", "rel"])
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"kill_switch_path": "relative"},
        {"silent_seconds": 0},
        {"heartbeat_seconds": True},
        {"max_reports": MAX_REPORTS_PER_OPERATION + 1},
        {"max_reports": 1},
        {"delivery_timeout_seconds": 60},
    ],
)
def test_config_is_closed_and_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        StageWatchConfig(**changes)


# -- enabled -------------------------------------------------------------------------------


def test_started_operation_reports_heartbeat_progress_and_failed(
    tmp_path: Path, kill_switch: str
) -> None:
    clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
    adapter = DockerAdapter(docker)
    lease = lease_for(clock)
    with make_supervisor(tmp_path, adapter, clock) as supervisor:
        runner = make_runner(tmp_path, channel, docker, clock, kill_switch)
        supervisor.watch_runner = runner
        assert supervisor.drive_once(OPERATION, lease).phase == "running"
        assert runner.active_operations == (OPERATION.operation_id,)
        contract = watcher(runner)._contract
        assert (contract.lease_id, contract.epoch, contract.provider) == ("lease-1", 1, PROVIDER)
        assert contract.deadline_seconds == OPERATION.max_duration_seconds
        assert contract.max_reports <= MAX_REPORTS_PER_OPERATION
        clock.advance(31)
        docker.set(OPERATION, restarts=1)
        assert supervisor.tick(OPERATION, lease).phase == "running"
        clock.advance(31)
        docker.set(OPERATION, status="exited", exit_code=1)
        assert supervisor.tick(OPERATION, lease).phase == "terminal"
    assert channel.kinds() == ["heartbeat", "progress", "failed"]
    for seq, (path, body, key, timeout) in enumerate(channel.posts):
        assert path == f"/worker/operations/{OPERATION.operation_id}/watch-reports"
        assert key == f"watch-{OPERATION.operation_id}-{seq}"
        assert body["seq"] == seq and body["lease_id"] == "lease-1"
        assert body["expected_epoch"] == 1 and body["kill_switch_engaged"] is False
        # Short: the delivery timeout, further capped by what is left of the tick budget.
        config = StageWatchConfig()
        assert 0 < timeout <= min(config.delivery_timeout_seconds, config.tick_budget_seconds)
    assert runner.active_operations == ()  # finished, then dropped
    assert runner.errors == 0
    # The probe used the adapter's own Docker runner and the adapter's container name only.
    assert {call[:4] for call in docker.calls if call[0] == "inspect"} == {
        ("inspect", "--type", "container", OPERATION.container_name)
    }
    assert not any(
        call[0] in {"kill", "stop", "rm", "start", "update"} for call in docker.calls[1:]
    )


def test_ended_operation_is_flushed_until_finished(tmp_path: Path, kill_switch: str) -> None:
    clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
    lease = lease_for(clock)
    with make_supervisor(tmp_path, DockerAdapter(docker), clock) as supervisor:
        runner = make_runner(tmp_path, channel, docker, clock, kill_switch)
        supervisor.watch_runner = runner
        supervisor.drive_once(OPERATION, lease)
        channel.fail_kinds["failed"] = 2
        clock.advance(5)
        docker.set(OPERATION, status="exited", exit_code=2)
        assert supervisor.tick(OPERATION, lease).phase == "terminal"
        # One attempt per tick, no in-tick retry: the failed report waits for a later tick.
        assert channel.kinds()[-1:] == ["failed"] and channel.kinds().count("failed") == 1
        assert runner.active_operations == (OPERATION.operation_id,)
        assert watcher(runner).done and not watcher(runner).finished
        runner.tick()
        assert channel.kinds().count("failed") == 2
        assert runner.active_operations == (OPERATION.operation_id,)
        runner.tick()
    failed_keys = {key for _, body, key, _ in channel.posts if body["kind"] == "failed"}
    assert failed_keys == {f"watch-{OPERATION.operation_id}-1"}  # same key every retry
    assert runner.active_operations == ()


def test_restart_resumes_pending_reports_without_sampling(tmp_path: Path, kill_switch: str) -> None:
    clock, docker = Clock(), FakeDocker()
    down = FakeChannel()
    down.fail_all = True
    first = make_runner(tmp_path, down, docker, clock, kill_switch)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    first.on_start(OPERATION, lease_for(clock))
    first.tick()
    clock.advance(5)
    docker.set(OPERATION, status="exited", exit_code=3)
    first.on_end(OPERATION.operation_id)
    first.tick()
    assert watcher(first).pending_count == 2  # heartbeat and failed, both undelivered
    first.close()
    assert first.active_operations == ()

    del docker.containers[OPERATION.container_name]  # the container is gone after the restart
    calls = len(docker.calls)
    up = FakeChannel()
    second = make_runner(tmp_path, up, docker, clock, kill_switch)
    assert second.active_operations == (OPERATION.operation_id,)
    second.tick()
    assert sorted(up.kinds()) == ["failed", "heartbeat"]
    assert up.kinds()[0] == "failed"  # closing reports flush first
    assert {key for _, _, key, _ in up.posts} == {
        f"watch-{OPERATION.operation_id}-0",
        f"watch-{OPERATION.operation_id}-1",
    }
    assert second.active_operations == ()
    assert len(docker.calls) == calls  # flush only: a gone container is not sampled
    third = make_runner(tmp_path, up, docker, clock, kill_switch)
    assert third.active_operations == ()  # nothing pending, nothing resumed


def test_recovered_running_operation_keeps_its_contract(tmp_path: Path, kill_switch: str) -> None:
    clock, docker = Clock(), FakeDocker()
    down = FakeChannel()
    down.fail_all = True
    first = make_runner(tmp_path, down, docker, clock, kill_switch)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    first.on_start(OPERATION, lease_for(clock))
    first.tick()
    digest = watcher(first).contract_digest
    first.close()
    # A changed operator setting must not orphan the outbox bound to the original contract.
    up = FakeChannel()
    second = make_runner(tmp_path, up, docker, clock, kill_switch, heartbeat_seconds=120)
    second.on_start(OPERATION, lease_for(clock))
    assert second._entries[OPERATION.operation_id].resumed is False
    assert watcher(second).contract_digest == digest
    second.tick()
    assert up.kinds() == ["heartbeat"]
    assert second.errors == 0


def test_watcher_exception_never_changes_the_supervisor_result(
    tmp_path: Path, kill_switch: str
) -> None:
    def run(directory: Path, attach: str | None) -> list[tuple[str, str | None]]:
        clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
        lease = lease_for(clock)
        results = []
        with make_supervisor(directory, DockerAdapter(docker), clock) as supervisor:
            runner = None
            if attach == "broken-watcher":
                runner = make_runner(directory, channel, docker, clock, kill_switch)
            elif attach == "broken-runner":
                runner = SimpleNamespace(
                    on_start=lambda *_: 1 / 0, on_end=lambda *_: 1 / 0, tick=lambda: 1 / 0
                )
            supervisor.watch_runner = runner
            result = supervisor.drive_once(OPERATION, lease)
            results.append((result.phase, result.reason))
            if attach == "broken-watcher":

                def explode() -> None:
                    raise RuntimeError("watcher bug")

                watcher(runner).tick = explode  # type: ignore[method-assign]
                watcher(runner).flush = explode  # type: ignore[method-assign]
            for _ in range(3):
                clock.advance(10)
                result = supervisor.tick(OPERATION, lease)
                results.append((result.phase, result.reason))
            docker.set(OPERATION, status="exited", exit_code=1)
            result = supervisor.tick(OPERATION, lease)
            results.append((result.phase, result.reason))
            result = supervisor.cancel(OPERATION)
            results.append((result.phase, result.reason))
            if attach == "broken-watcher":
                assert runner.errors >= 4
        return results

    baseline = run(tmp_path / "plain", None)
    assert run(tmp_path / "watcher", "broken-watcher") == baseline
    assert run(tmp_path / "runner", "broken-runner") == baseline


def test_kill_switch_engaged_still_observes_and_reports(tmp_path: Path, kill_switch: str) -> None:
    Path(kill_switch).write_text("")  # marker present: engaged
    clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
    runner = make_runner(tmp_path, channel, docker, clock, kill_switch)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    runner.on_start(OPERATION, lease_for(clock))
    runner.tick()
    assert watcher(runner).may_request() is False
    assert watcher(runner).may_call_model() is False
    assert channel.kinds() == ["heartbeat"]
    assert channel.posts[0][1]["kill_switch_engaged"] is True
    assert runner.status()["kill_switch_engaged"] is True
    # The runner itself offers no request capability at all, engaged or not.
    for name in ("may_request", "request", "request_retry", "request_resize", "request_reroute"):
        assert not hasattr(runner, name)


def test_missing_kill_switch_path_starts_no_watcher(tmp_path: Path) -> None:
    clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
    runner = make_runner(tmp_path, channel, docker, clock, None)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    runner.on_start(OPERATION, lease_for(clock))
    runner.on_start(OPERATION, lease_for(clock))
    runner.tick()
    assert runner.active_operations == ()
    assert runner.refused_no_kill_switch == 1
    assert channel.posts == [] and docker.calls == []
    assert list((tmp_path / WATCH_DIRECTORY).iterdir()) == []


def test_runner_has_no_management_or_stop_path(tmp_path: Path, kill_switch: str) -> None:
    channel = FakeChannel()
    runner = make_runner(tmp_path, channel, FakeDocker(), Clock(), kill_switch)
    for name in (
        "stop",
        "cancel",
        "kill",
        "retry",
        "resize",
        "reroute",
        "re_route",
        "restart",
        "remove",
        "credential",
        "token",
        "management_credential",
        "adapter",
    ):
        assert not hasattr(runner, name), name
    assert {name for name in dir(runner) if not name.startswith("_")} == {
        "active_operations",
        "close",
        "decision_refused",
        "decision_requests",
        "directory",
        "errors",
        "on_end",
        "on_start",
        "on_start_remote",
        "refused_no_kill_switch",
        "refused_unknown_provider",
        "signals_path",
        "status",
        "tick",
    }
    assert runner._channel is channel  # the worker's existing channel, nothing new


def test_outbox_directory_and_files_are_owner_only(tmp_path: Path, kill_switch: str) -> None:
    previous = os.umask(0o002)
    try:
        clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
        runner = make_runner(tmp_path, channel, docker, clock, kill_switch)
        docker.create(OPERATION)
        docker.set(OPERATION, status="running")
        runner.on_start(OPERATION, lease_for(clock))
        runner.tick()
    finally:
        os.umask(previous)
    directory = tmp_path / WATCH_DIRECTORY
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = sorted(directory.iterdir())
    assert {path.name for path in files} >= {
        f"{OPERATION.operation_id}.sqlite3",
        f"{OPERATION.operation_id}.contract.json",
    }
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    stored = json.loads((directory / f"{OPERATION.operation_id}.contract.json").read_text())
    assert set(stored) == {"contract", "plan_digest"}  # no credential, no URL


def test_existing_loose_directory_is_tightened_and_symlink_refused(
    tmp_path: Path, kill_switch: str
) -> None:
    loose = tmp_path / "loose"
    (loose / WATCH_DIRECTORY).mkdir(parents=True, mode=0o755)
    os.chmod(loose / WATCH_DIRECTORY, 0o755)
    runner = make_runner(loose, FakeChannel(), FakeDocker(), Clock(), kill_switch)
    assert stat.S_IMODE((loose / WATCH_DIRECTORY).stat().st_mode) == 0o700
    assert runner.errors == 0
    linked = tmp_path / "linked"
    linked.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (linked / WATCH_DIRECTORY).symlink_to(tmp_path / "elsewhere")
    docker, channel = FakeDocker(), FakeChannel()
    refused = make_runner(linked, channel, docker, Clock(), kill_switch)
    assert refused.directory is None and refused.errors == 1
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    refused.on_start(OPERATION, lease_for(Clock()))
    refused.tick()
    assert refused.active_operations == () and channel.posts == []


def test_two_concurrent_operations_are_independent(tmp_path: Path, kill_switch: str) -> None:
    clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
    runner = make_runner(tmp_path, channel, docker, clock, kill_switch)
    for operation in (OPERATION, OTHER):
        docker.create(operation)
        docker.set(operation, status="running")
    runner.on_start(OPERATION, lease_for(clock))
    runner.on_start(OTHER, lease_for(clock, OTHER, id="lease-2", epoch=4))
    runner.tick()
    assert runner.active_operations == tuple(sorted((OPERATION.operation_id, OTHER.operation_id)))
    clock.advance(5)
    docker.set(OPERATION, status="exited", exit_code=9)
    runner.on_end(OPERATION.operation_id)
    runner.tick()
    assert runner.active_operations == (OTHER.operation_id,)
    by_operation: dict[str, list[tuple[str, str]]] = {}
    for path, body, key, _ in channel.posts:
        operation_id = path.split("/")[3]
        by_operation.setdefault(operation_id, []).append((body["kind"], key))
        assert body["lease_id"] == (
            "lease-1" if operation_id == OPERATION.operation_id else "lease-2"
        )
    assert by_operation[OPERATION.operation_id] == [
        ("heartbeat", f"watch-{OPERATION.operation_id}-0"),
        ("failed", f"watch-{OPERATION.operation_id}-1"),
    ]
    assert by_operation[OTHER.operation_id] == [("heartbeat", f"watch-{OTHER.operation_id}-0")]
    clock.advance(61)
    runner.tick()
    assert channel.posts[-1][2] == f"watch-{OTHER.operation_id}-1"
    assert runner.errors == 0
