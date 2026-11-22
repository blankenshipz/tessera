"""Tick budgets bound decision delivery too (ADR 0087, merge of tick bounds and decisions).

Decision requests are flushed after the reports (closing reports first) and draw on the same
per-flush post budget and deadline; a request that does not fit stays recorded and is
delivered by a later tick under the same key. Fakes only: nothing here sleeps or calls Docker.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_stage_watch_tick_bounds import Monotonic, SlowChannel
from test_stage_watcher_decisions import (
    ENVELOPE,
    OPERATION,
    OPERATION_ID,
    Channel,
    Clock,
    Probe,
    StatsDocker,
    exit_with,
    lease,
    make_contract,
)

from tessera_control.stage_watch_limits import MIN_ROUTINE_INTERVAL_SECONDS
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.stage_watch_config import StageWatchConfig
from tessera_worker.stage_watch_runner import (
    RemoteOperationInfo,
    StageWatchRunner,
    build_stage_watch_runner,
    register_probe_factory,
    unregister_probe_factory,
)
from tessera_worker.stage_watcher import (
    ControlDecisionSink,
    ControlSink,
    DecisionEnvelope,
    KillSwitch,
    ProbeSample,
    StageWatcher,
    WatchOutbox,
)
from tessera_worker.stage_watcher.report import MIN_POST_TIMEOUT_SECONDS

DECISION_KEY = f"watch-decision-{OPERATION_ID}-0"


class GatedChannel(Channel):
    """``Channel`` that fails every post transiently (no status) while ``down``."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def post(
        self, path: str, body: dict[str, Any], key: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if self.down:
            self.posts.append((path, body, key))
            raise ControlChannelError("control transport unavailable")
        return super().post(path, body, key, timeout_seconds=timeout_seconds)

    def kinds(self) -> list[str]:
        return [body["kind"] for _, body, _ in self.posts]


@pytest.fixture
def clear_switch(tmp_path: Path) -> KillSwitch:
    directory = tmp_path / "operator"
    directory.mkdir()
    return KillSwitch(str(directory / "stop-agents"))


def backlogged_watcher(
    tmp_path: Path, switch: KillSwitch, monotonic: Monotonic | None = None
) -> tuple[StageWatcher, GatedChannel, Clock]:
    """A watcher whose outbox holds routine reports, a ``failed`` and a ``retry_request``, all
    pending (control was down while they were recorded)."""
    channel, clock = GatedChannel(), Clock()
    running = [{"state": "running", "output_bytes": 10 * (index + 1)} for index in range(3)]
    probe = Probe(*running, exit_with(75))
    watcher = StageWatcher(
        make_contract(**ENVELOPE),
        probe,
        ControlSink(channel),
        switch,
        clock,
        WatchOutbox(tmp_path / "watch.sqlite3"),
        sleep=lambda _seconds: None,
        max_delivery_attempts=1,
        decision_sink=ControlDecisionSink(channel),
        monotonic=monotonic or Monotonic(),
    )
    channel.down = True
    for _ in range(4):
        watcher.tick()
        clock.advance(MIN_ROUTINE_INTERVAL_SECONDS + 1)
    channel.down = False
    channel.posts.clear()
    return watcher, channel, clock


def test_budget_of_two_posts_terminal_first_and_keeps_the_decision(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    watcher, channel, _clock = backlogged_watcher(tmp_path, clear_switch)
    assert watcher.done and watcher.decision_requests == 1
    routine = [r for r in WatchOutbox(tmp_path / "watch.sqlite3").pending() if r.kind != "failed"]
    assert routine, "the backlog needs routine reports"
    assert watcher.has_pending_terminal
    total = watcher.pending_count
    assert total == len(routine) + 2  # + failed + retry_request

    watcher.flush(max_posts=2)
    assert watcher.last_posts == 2 and len(channel.posts) == 2
    assert channel.kinds()[0] == "failed"  # the closing report goes first
    assert channel.decisions() == []  # the request did not fit: still pending
    assert not watcher.last_flush_complete
    assert not watcher.has_pending_terminal
    assert watcher.pending_count == total - 2 and not watcher.finished

    rounds = 0
    while not watcher.finished:
        rounds += 1
        assert rounds < 10
        before = len(channel.posts)
        watcher.flush(max_posts=2)
        assert len(channel.posts) - before <= 2
    assert watcher.last_flush_complete
    # Reports first, then the request, posted exactly once under its stable key.
    assert channel.kinds()[-1] == "retry_request"
    assert [key for _, _, key in channel.decisions()] == [DECISION_KEY]
    gaps = WatchOutbox(tmp_path / "watch.sqlite3").gaps()
    assert (gaps.decisions_delivered, gaps.decisions_pending) == (1, 0)


def test_decision_is_not_lost_when_the_budget_runs_out(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    watcher = StageWatcher(
        make_contract(**ENVELOPE),
        Probe(exit_with(75)),
        ControlSink(channel),
        clear_switch,
        Clock(),
        WatchOutbox(tmp_path / "watch.sqlite3"),
        sleep=lambda _seconds: None,
        max_delivery_attempts=1,
        decision_sink=ControlDecisionSink(channel),
    )
    watcher.tick(max_posts=1)  # failed + retry_request recorded; room for one post
    assert channel.report_kinds() == ["failed"] and channel.decisions() == []
    assert watcher.last_posts == 1 and not watcher.last_flush_complete
    assert watcher.pending_count == 1 and not watcher.finished

    watcher.flush(max_posts=0)  # sample-free, post-free: nothing moves, nothing is lost
    assert watcher.last_posts == 0 and not watcher.last_flush_complete
    assert watcher.pending_count == 1

    watcher.flush(max_posts=1)
    assert [key for _, _, key in channel.decisions()] == [DECISION_KEY]
    assert watcher.last_flush_complete and watcher.finished

    watcher.flush(max_posts=5)  # nothing pending: no post, complete
    assert watcher.last_posts == 0 and watcher.last_flush_complete


def test_decision_respects_the_flush_deadline(tmp_path: Path, clear_switch: KillSwitch) -> None:
    monotonic = Monotonic()
    watcher, channel, _clock = backlogged_watcher(tmp_path, clear_switch, monotonic)
    watcher.flush(deadline=monotonic.value)  # already over: no attempt starts
    assert channel.posts == [] and watcher.last_posts == 0
    assert not watcher.last_flush_complete
    watcher.flush(deadline=monotonic.value + 1)
    assert watcher.finished and len(channel.decisions()) == 1


def test_transient_decision_failure_keeps_the_key_for_the_next_tick(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    channel.decision_errors = [ControlChannelError("unavailable", 503)]
    watcher = StageWatcher(
        make_contract(**ENVELOPE),
        Probe(exit_with(75)),
        ControlSink(channel),
        clear_switch,
        Clock(),
        WatchOutbox(tmp_path / "watch.sqlite3"),
        sleep=lambda _seconds: None,
        max_delivery_attempts=1,
        decision_sink=ControlDecisionSink(channel),
    )
    watcher.tick(max_posts=4)
    assert watcher.last_posts == 2  # failed + one decision attempt
    assert watcher.last_flush_complete  # stopped on a transient failure, not on the budget
    assert watcher.pending_count == 1
    watcher.flush(max_posts=4)
    assert [key for _, _, key in channel.decisions()] == [DECISION_KEY, DECISION_KEY]
    assert watcher.finished


# -- the decision sink follows the same timeout rule as the report sink -----------------------


def test_decision_sink_timeout_is_the_smallest_of_its_bounds() -> None:
    channel = SlowChannel(Monotonic(), timeout_seconds=1.0)
    assert ControlDecisionSink(channel, timeout_seconds=2.0).effective_timeout() == 1.0
    assert ControlDecisionSink(channel, timeout_seconds=0.5).effective_timeout() == 0.5
    assert ControlDecisionSink(channel, remaining=lambda: 0.3).effective_timeout() == 0.3
    assert ControlDecisionSink(channel, remaining=lambda: None).effective_timeout() == 1.0
    starved = ControlDecisionSink(channel, timeout_seconds=2.0, remaining=lambda: -1.0)
    assert starved.effective_timeout() == MIN_POST_TIMEOUT_SECONDS
    for remaining in (None, lambda: 0.3, lambda: -1.0):
        assert (
            ControlDecisionSink(
                channel, timeout_seconds=2.0, remaining=remaining
            ).effective_timeout()
            == ControlSink(channel, timeout_seconds=2.0, remaining=remaining).effective_timeout()
        )


def test_runner_over_a_one_second_channel_delivers_decisions(tmp_path: Path) -> None:
    clock, monotonic, docker = Clock(), Monotonic(), StatsDocker()
    channel = SlowChannel(monotonic, latency=0.05, timeout_seconds=1.0)
    switch = tmp_path / "operator"
    switch.mkdir()
    runner = StageWatchRunner(
        StageWatchConfig(
            enabled=True, kill_switch_path=str(switch / "stop"), delivery_timeout_seconds=2.0
        ),
        state_dir=tmp_path,
        channel=channel,
        docker_run=docker,
        clock=clock,
        monotonic=monotonic,
    )
    runner.on_start(
        OPERATION,
        lease(clock),
        DecisionEnvelope(retry_attempts_remaining=1, transient_exit_codes=(75,)),
    )
    runner.tick()
    docker.status, docker.exit_code = "exited", 75
    clock.advance(10)
    runner.tick()
    runner.on_end(OPERATION.operation_id)
    runner.tick()
    decisions = [post for post in channel.posts if post[0].endswith("/watch-decisions")]
    assert len(decisions) == 1 and decisions[0][1]["kind"] == "retry_request"
    # Never longer than the channel allows (it would refuse), and capped by the time left.
    assert all(timeout is not None and timeout <= 1.0 for _, _, timeout in channel.posts)
    assert runner.status()["decision_requests"] == 1 and runner.errors == 0
    assert runner.active_operations == ()


def test_runner_budget_of_two_covers_reports_and_decisions(tmp_path: Path) -> None:
    clock, monotonic, docker = Clock(), Monotonic(), StatsDocker()
    channel = GatedChannel()
    switch = tmp_path / "operator"
    switch.mkdir()
    runner = StageWatchRunner(
        StageWatchConfig(
            enabled=True,
            kill_switch_path=str(switch / "stop"),
            heartbeat_seconds=MIN_ROUTINE_INTERVAL_SECONDS,
            tick_max_posts=2,
        ),
        state_dir=tmp_path,
        channel=channel,
        docker_run=docker,
        clock=clock,
        monotonic=monotonic,
    )
    runner.on_start(
        OPERATION,
        lease(clock),
        DecisionEnvelope(retry_attempts_remaining=1, transient_exit_codes=(75,)),
    )
    channel.down = True
    for _ in range(3):  # routine heartbeats pile up behind an outage
        runner.tick()
        clock.advance(MIN_ROUTINE_INTERVAL_SECONDS + 1)
    docker.status, docker.exit_code = "exited", 75
    runner.tick()  # failed + retry_request recorded
    runner.on_end(OPERATION.operation_id)
    watcher = runner._entries[OPERATION.operation_id].watcher
    assert watcher.has_pending_terminal and watcher.pending_count >= 4
    channel.down = False
    channel.posts.clear()

    runner.tick()
    assert len(channel.posts) == 2
    assert channel.kinds()[0] == "failed"
    assert channel.decisions() == []

    rounds = 0
    while runner.active_operations:
        rounds += 1
        assert rounds < 10
        before = len(channel.posts)
        runner.tick()
        assert len(channel.posts) - before <= 2
    assert [key for _, _, key in channel.decisions()] == [
        f"watch-decision-{OPERATION.operation_id}-0"
    ]
    assert channel.kinds()[-1] == "retry_request"
    assert runner.status()["decision_requests"] == 1 and runner.errors == 0


# -- stats and envelope still work under the bounded runner -----------------------------------


def test_stats_flag_yields_mem_bytes_through_the_bounded_docker_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tessera_worker import cpu, stage_watch_runner

    docker = StatsDocker()
    timeouts: list[float] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[0] == "docker"
        timeouts.append(kwargs["timeout"])
        return docker(command[1:])

    monkeypatch.setattr(subprocess, "run", fake_run)
    clock, channel = Clock(), Channel()
    adapter = type("Adapter", (), {"run": staticmethod(cpu._docker)})()
    runner = build_stage_watch_runner(
        StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "ks"), stats=True),
        state_dir=tmp_path,
        channel=channel,
        adapter=adapter,
        clock=clock,
    )
    # Compare against the module the runner itself came from: another test file may have
    # re-imported the watch modules, which would leave two copies of the function.
    runner_module = sys.modules[type(runner).__module__]
    assert runner is not None and runner._docker_run is runner_module.bounded_docker
    runner.on_start(OPERATION, lease(clock))
    runner.tick()
    (body,) = [body for _, body, _ in channel.posts if body["kind"] == "heartbeat"]
    assert body["counters"]["mem_bytes"] == 512 * 1024**2
    assert any(call[0] == "stats" for call in docker.calls)
    assert timeouts and all(t <= stage_watch_runner.INSPECT_TIMEOUT_SECONDS for t in timeouts)


def test_envelope_reaches_the_contract_local_and_remote(tmp_path: Path) -> None:
    clock, channel, docker = Clock(), Channel(), StatsDocker()
    switch = tmp_path / "operator"
    switch.mkdir()
    runner = StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=str(switch / "stop"), tick_max_posts=1),
        state_dir=tmp_path,
        channel=channel,
        docker_run=docker,
        clock=clock,
        monotonic=Monotonic(),
    )
    envelope = DecisionEnvelope(
        retry_attempts_remaining=2,
        transient_exit_codes=(75,),
        memory_bytes_current=1024**3,
        memory_bytes_max=2 * 1024**3,
    )
    runner.on_start(OPERATION, lease(clock), envelope)
    contract = runner._entries[OPERATION.operation_id].watcher._contract
    assert (contract.retry_attempts_remaining, contract.transient_exit_codes) == (2, (75,))
    assert (contract.memory_bytes_current, contract.memory_bytes_max) == (1024**3, 2 * 1024**3)
    stored = json.loads(
        (tmp_path / "stage-watch" / f"{OPERATION.operation_id}.contract.json").read_text()
    )
    assert stored["contract"]["memory_bytes_max"] == 2 * 1024**3

    class RemoteProbe:
        def sample(self, now: Any) -> ProbeSample:
            return ProbeSample("running", now, progress_marker=1)

    register_probe_factory("fake-remote", lambda _contract, _directory: RemoteProbe())
    try:
        info = RemoteOperationInfo(
            operation_id="d" * 32,
            lease_id="lease-r",
            epoch=2,
            stage_key="train",
            provider="fake-remote",
            deadline_seconds=3600,
        )
        runner.on_start_remote(info, envelope)
        remote = runner._entries["d" * 32].watcher._contract
        assert remote.retry_attempts_remaining == 2 and remote.memory_bytes_max == 2 * 1024**3
        runner.tick()
        assert runner.errors == 0
    finally:
        unregister_probe_factory("fake-remote")
