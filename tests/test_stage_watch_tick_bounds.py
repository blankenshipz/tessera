"""Every stage watch runner tick is bounded (ADR 0087, review findings 1 and 2). Fakes only.

The supervisor calls ``StageWatchRunner.tick()`` on the thread that renews the worker's lease,
so a backlog or a slow control plane must never hold it. Time is a fake monotonic clock that
the fake channels advance: nothing here sleeps.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_stage_watch_runner import IMAGE, Clock, FakeDocker, lease_for
from test_stage_watcher import Harness

from tessera_worker.control_channel import ControlChannelError, HttpControlChannel
from tessera_worker.cpu import CpuOperation
from tessera_worker.stage_watch_config import StageWatchConfig
from tessera_worker.stage_watch_runner import (
    MAX_RESUMED_OUTBOXES,
    OUTBOX_SUFFIX,
    WATCH_DIRECTORY,
    StageWatchRunner,
)
from tessera_worker.stage_watcher import ControlSink, WatchOutbox, WatchReport


class Monotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SlowChannel:
    """The worker's control channel over a slow control plane, on a fake monotonic clock.

    Like ``HttpControlChannel.post`` it refuses a request timeout longer than its own. Each
    request takes ``latency`` seconds; one given a shorter timeout times out at that timeout.
    """

    def __init__(
        self,
        monotonic: Monotonic,
        latency: float = 0.0,
        *,
        timeout_seconds: float = 15.0,
        down: bool = False,
    ) -> None:
        self.monotonic = monotonic
        self.latency = latency
        self.timeout_seconds = timeout_seconds
        self.down = down  # fail at once (no answer), without spending time
        self.posts: list[tuple[str, dict[str, Any], float | None]] = []

    def post(
        self,
        path: str,
        body: dict[str, Any],
        key: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if timeout_seconds is None:
            timeout = self.timeout_seconds
        elif (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= self.timeout_seconds
        ):
            raise ValueError("Request timeout must be positive and no longer than channel timeout.")
        else:
            timeout = timeout_seconds
        self.posts.append((path, body, timeout_seconds))
        if self.down:
            raise ControlChannelError("control transport unavailable")
        if self.latency > timeout:
            self.monotonic.advance(timeout)
            raise ControlChannelError("control response deadline exceeded")
        self.monotonic.advance(self.latency)
        return {"recorded": True}

    def operations(self) -> list[str]:
        return [path.split("/")[3] for path, _, _ in self.posts]

    def kinds(self) -> list[str]:
        return [body["kind"] for _, body, _ in self.posts]


@pytest.fixture
def kill_switch(tmp_path: Path) -> str:
    directory = tmp_path / "operator"
    directory.mkdir()
    return str(directory / "stop-agents")


def make_runner(
    state: Path,
    channel: Any,
    docker: FakeDocker,
    clock: Clock,
    monotonic: Monotonic,
    kill_switch: str,
    **config: Any,
) -> StageWatchRunner:
    return StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=kill_switch, **config),
        state_dir=state,
        channel=channel,
        docker_run=docker,
        clock=clock,
        monotonic=monotonic,
    )


def operation(index: int, max_duration_seconds: int = 3600) -> CpuOperation:
    return CpuOperation(
        f"{index:032x}",
        IMAGE,
        ("python", "-c", "print(1)"),
        f"{index:064x}",
        max_duration_seconds,
    )


# Runs for a day: a 2-hour outage stays inside its watch deadline.
OPERATION = operation(0xB, 86400)


def pending(state: Path, operation_id: str) -> int:
    return WatchOutbox(state / WATCH_DIRECTORY / f"{operation_id}{OUTBOX_SUFFIX}").pending_count()


def backlog(
    state: Path, kill_switch: str, clock: Clock, docker: FakeDocker, count: int = 120
) -> None:
    """A 2-hour control outage: ``count`` heartbeats recorded, none delivered."""
    monotonic = Monotonic()
    down = SlowChannel(monotonic, down=True)
    runner = make_runner(state, down, docker, clock, monotonic, kill_switch)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    runner.on_start(OPERATION, lease_for(clock, OPERATION))
    for _ in range(count):
        runner.tick()
        clock.advance(60)
    assert pending(state, OPERATION.operation_id) == count
    runner.close()


def timed_tick(runner: StageWatchRunner, channel: SlowChannel, monotonic: Monotonic):
    posts, started = len(channel.posts), monotonic()
    runner.tick()
    return len(channel.posts) - posts, monotonic() - started


# -- (a) a backlog behind a slow control plane -----------------------------------------------


@pytest.mark.parametrize(
    ("latency", "config", "posts_per_tick"),
    [
        # A slow but answering control plane: the post budget binds (4 posts, 0.8 s).
        (0.2, {}, 4),
        # 2 s per post: the wall budget binds; the third post gets only the 1 s left.
        (2.0, {"tick_budget_seconds": 5.0}, 3),
    ],
)
def test_backlog_behind_a_slow_sink_is_drained_in_bounded_ticks(
    tmp_path: Path, kill_switch: str, latency: float, config: dict, posts_per_tick: int
) -> None:
    clock, docker = Clock(), FakeDocker()
    backlog(tmp_path, kill_switch, clock, docker)
    monotonic = Monotonic()
    slow = SlowChannel(monotonic, latency)
    runner = make_runner(tmp_path, slow, docker, clock, monotonic, kill_switch, **config)
    budget = runner._config.tick_budget_seconds
    for _ in range(3):  # the resumed backlog and a running watcher behave alike
        posts, elapsed = timed_tick(runner, slow, monotonic)
        assert posts == posts_per_tick <= runner._config.tick_max_posts
        assert elapsed <= budget
    assert all(timeout is not None and timeout <= budget for _, _, timeout in slow.posts)
    ticks = 3
    while pending(tmp_path, OPERATION.operation_id):
        posts, elapsed = timed_tick(runner, slow, monotonic)
        assert 0 < posts <= runner._config.tick_max_posts and elapsed <= budget
        ticks += 1
        assert ticks <= 120  # drains: every tick delivers something
    assert runner.errors == 0
    assert {body["seq"] for _, body, _ in slow.posts} == set(range(120))


def test_running_watcher_backlog_is_bounded_on_the_same_runner(
    tmp_path: Path, kill_switch: str
) -> None:
    """The finding's scenario on one runner: control comes back after an outage."""
    clock, docker, monotonic = Clock(), FakeDocker(), Monotonic()
    channel = SlowChannel(monotonic, 2.0, down=True)
    runner = make_runner(tmp_path, channel, docker, clock, monotonic, kill_switch)
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    runner.on_start(OPERATION, lease_for(clock, OPERATION))
    for _ in range(120):
        runner.tick()
        clock.advance(60)
    assert pending(tmp_path, OPERATION.operation_id) == 120
    channel.down = False
    posts, elapsed = timed_tick(runner, channel, monotonic)
    assert posts == 1  # the 1 s budget caps the post's timeout: it times out at 1 s
    assert elapsed <= runner._config.tick_budget_seconds
    assert channel.posts[-1][2] == pytest.approx(1.0)
    assert runner.active_operations == (OPERATION.operation_id,)  # nothing lost or dropped


# -- (b) 256 rebuilt watchers and a control plane that always times out ----------------------


def test_rebuilt_watchers_are_served_round_robin_within_the_budget(
    tmp_path: Path, kill_switch: str
) -> None:
    clock, docker, monotonic = Clock(), FakeDocker(), Monotonic()
    down = SlowChannel(monotonic, down=True)
    first = make_runner(tmp_path, down, docker, clock, monotonic, kill_switch)
    operations = [operation(index) for index in range(1, MAX_RESUMED_OUTBOXES + 1)]
    for item in operations:
        docker.create(item)
        docker.set(item, status="running")
        first.on_start(item, lease_for(clock, item))
    first.tick()  # every watcher records a heartbeat; control is down
    first.close()

    timing_out = SlowChannel(monotonic, 2.0)
    second = make_runner(tmp_path, timing_out, docker, clock, monotonic, kill_switch)
    assert len(second.active_operations) == len(operations)
    served: list[str] = []
    for _ in range(len(operations)):
        before = len(timing_out.posts)
        posts, elapsed = timed_tick(second, timing_out, monotonic)
        assert posts <= second._config.tick_max_posts
        assert elapsed <= second._config.tick_budget_seconds
        served.extend(timing_out.operations()[before:])
    # Every watcher got its turn, each exactly once: none starved.
    assert sorted(served) == sorted(item.operation_id for item in operations)
    assert second.errors == 0


# -- (c) a pending ``failed`` goes first ----------------------------------------------------


def test_watcher_with_a_pending_failed_is_served_before_routine_ones(
    tmp_path: Path, kill_switch: str
) -> None:
    clock, docker, monotonic = Clock(), FakeDocker(), Monotonic()
    down = SlowChannel(monotonic, down=True)
    first = make_runner(tmp_path, down, docker, clock, monotonic, kill_switch)
    operations = [operation(index) for index in range(1, 6)]
    for item in operations:
        docker.create(item)
        docker.set(item, status="running")
        first.on_start(item, lease_for(clock, item))
    first.tick()
    clock.advance(5)
    last = operations[-1]  # sorts last: served last in plain id order
    docker.set(last, status="exited", exit_code=3)
    first.on_end(last.operation_id)
    first.tick()
    first.close()

    up = SlowChannel(monotonic, 0.1)
    second = make_runner(tmp_path, up, docker, clock, monotonic, kill_switch, tick_max_posts=1)
    second.tick()
    assert up.kinds() == ["failed"]
    assert up.operations() == [last.operation_id]
    # A live runner too: the failing watcher jumps the round-robin queue.
    for _ in range(10):
        second.tick()
    assert pending(tmp_path, last.operation_id) == 0


# -- (d) the delivery timeout fits the channel's own --------------------------------------


class Response:
    def __init__(self) -> None:
        self.headers = {"Content-Type": "application/json"}

    def getcode(self) -> int:
        return 201

    def read(self, _amount: int) -> bytes:
        return b'{"recorded":true}'

    def close(self) -> None:
        pass


class Opener:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def __call__(self, _request: Any, timeout_seconds: float) -> Response:
        self.timeouts.append(timeout_seconds)
        return Response()


def report(seq: int = 0) -> WatchReport:
    return WatchReport(
        operation_id=OPERATION.operation_id,
        lease_id="lease-1",
        epoch=1,
        kind="heartbeat",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        seq=seq,
        counters={"elapsed_s": 1},
        kill_switch_engaged=False,
        contract_digest="0" * 64,
    )


@pytest.mark.parametrize(("channel_timeout", "used"), [(1.0, 1.0), (5.0, 2.0)])
def test_control_sink_timeout_fits_the_channel(channel_timeout: float, used: float) -> None:
    opener = Opener()
    channel = HttpControlChannel(
        "https://control.example", "worker-token", timeout_seconds=channel_timeout, opener=opener
    )
    sink = ControlSink(channel, timeout_seconds=2.0)
    assert sink.deliver(report()).outcome == "delivered"
    assert opener.timeouts == [used]


def test_control_sink_timeout_never_exceeds_the_remaining_budget() -> None:
    channel = SlowChannel(Monotonic())
    left: list[float | None] = [0.5]
    sink = ControlSink(channel, timeout_seconds=2.0, remaining=lambda: left[0])
    for value, expected in ((0.5, 0.5), (0.0, 0.05), (-3.0, 0.05), (9.0, 2.0), (None, 2.0)):
        left[0] = value
        assert sink.deliver(report()).outcome == "delivered"
        assert channel.posts[-1][2] == pytest.approx(expected)


def test_runner_over_a_one_second_channel_delivers_every_report(
    tmp_path: Path, kill_switch: str
) -> None:
    opener = Opener()
    channel = HttpControlChannel(
        "https://control.example", "worker-token", timeout_seconds=1.0, opener=opener
    )
    clock, docker = Clock(), FakeDocker()
    runner = StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=kill_switch),
        state_dir=tmp_path,
        channel=channel,
        docker_run=docker,
        clock=clock,
    )
    docker.create(OPERATION)
    docker.set(OPERATION, status="running")
    runner.on_start(OPERATION, lease_for(clock, OPERATION))
    for _ in range(5):
        runner.tick()
        clock.advance(60)
    assert len(opener.timeouts) == 5  # one request per report
    assert all(0 < timeout <= 1.0 for timeout in opener.timeouts)
    assert pending(tmp_path, OPERATION.operation_id) == 0
    assert runner.errors == 0


# -- (e) configuration ---------------------------------------------------------------------


def test_tick_bounds_have_safe_defaults() -> None:
    config = StageWatchConfig()
    assert config.tick_budget_seconds == 1.0
    assert config.tick_max_posts == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"tick_budget_seconds": 0.1},
        {"tick_budget_seconds": 5.0},
        {"tick_budget_seconds": 2},
        {"tick_max_posts": 1},
        {"tick_max_posts": 32},
    ],
)
def test_tick_bounds_accept_their_range(changes: dict[str, Any]) -> None:
    config = StageWatchConfig(**changes)
    for name, value in changes.items():
        assert getattr(config, name) == value


@pytest.mark.parametrize(
    "changes",
    [
        {"tick_budget_seconds": 0.09},
        {"tick_budget_seconds": 5.01},
        {"tick_budget_seconds": float("nan")},
        {"tick_budget_seconds": float("inf")},
        {"tick_budget_seconds": True},
        {"tick_budget_seconds": "1"},
        {"tick_budget_seconds": None},
        {"tick_max_posts": 0},
        {"tick_max_posts": 33},
        {"tick_max_posts": True},
        {"tick_max_posts": 2.0},
        {"tick_max_posts": None},
    ],
)
def test_tick_bounds_are_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        StageWatchConfig(**changes)


# -- (f) close() is bounded too ----------------------------------------------------------


def test_close_flushes_within_the_same_budget(tmp_path: Path, kill_switch: str) -> None:
    clock, docker = Clock(), FakeDocker()
    backlog(tmp_path, kill_switch, clock, docker)
    monotonic = Monotonic()
    slow = SlowChannel(monotonic, 2.0)
    runner = make_runner(tmp_path, slow, docker, clock, monotonic, kill_switch)
    started = monotonic()
    runner.close()
    assert len(slow.posts) <= runner._config.tick_max_posts
    assert monotonic() - started <= runner._config.tick_budget_seconds
    assert runner.active_operations == ()
    assert pending(tmp_path, OPERATION.operation_id) == 120  # kept for the next runner
    assert runner.errors == 0

    fast = SlowChannel(monotonic, 0.0)
    again = make_runner(tmp_path, fast, docker, clock, monotonic, kill_switch, tick_max_posts=7)
    again.close()
    assert len(fast.posts) == 7  # the post budget binds close() as well


def test_tick_never_raises_when_the_monotonic_clock_fails(tmp_path: Path, kill_switch: str) -> None:
    clock, docker = Clock(), FakeDocker()
    backlog(tmp_path, kill_switch, clock, docker, count=3)

    def broken() -> float:
        raise RuntimeError("clock")

    runner = StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=kill_switch),
        state_dir=tmp_path,
        channel=SlowChannel(Monotonic()),
        docker_run=docker,
        clock=clock,
        monotonic=broken,
    )
    runner.tick()
    runner.close()
    assert runner.errors >= 1 and runner.active_operations == ()
    assert pending(tmp_path, OPERATION.operation_id) == 3


# -- (g) the watcher's bounds are opt-in -------------------------------------------------


def backlogged(tmp_path: Path, count: int, **kwargs: Any) -> Harness:
    harness = Harness(tmp_path, **kwargs)
    harness.sink.fail = count
    for _ in range(count):
        harness.watcher.tick()
        harness.clock.advance(60)
    assert harness.watcher.pending_count == count
    return harness


def test_watcher_without_bounds_flushes_everything(tmp_path: Path) -> None:
    harness = backlogged(tmp_path, 10, max_delivery_attempts=1)
    harness.watcher.flush()
    assert harness.watcher.pending_count == 0
    assert harness.watcher.last_posts == 10 and harness.watcher.last_flush_complete


def test_watcher_flush_stops_at_its_post_budget(tmp_path: Path) -> None:
    harness = backlogged(tmp_path, 10, max_delivery_attempts=1)
    harness.watcher.flush(max_posts=3)
    assert harness.watcher.pending_count == 7
    assert harness.watcher.last_posts == 3 and not harness.watcher.last_flush_complete
    harness.watcher.tick(max_posts=0)  # sample (and record) only
    assert harness.watcher.last_posts == 0
    with pytest.raises(ValueError):
        harness.watcher.flush(max_posts=-1)


def test_watcher_flush_stops_at_its_deadline(tmp_path: Path) -> None:
    monotonic = Monotonic()
    harness = backlogged(tmp_path, 10, max_delivery_attempts=1, monotonic=monotonic)
    deliver = harness.sink.deliver

    def slow(report: WatchReport) -> bool:
        monotonic.advance(0.4)
        return deliver(report)

    harness.sink.deliver = slow  # type: ignore[method-assign]
    harness.watcher.flush(deadline=monotonic() + 1.0)
    assert harness.watcher.last_posts == 3  # starts at 0, 0.4 and 0.8; none at 1.2
    assert harness.watcher.pending_count == 7
    harness.watcher.flush(deadline=monotonic())  # already passed: no attempt at all
    assert harness.watcher.last_posts == 0 and harness.watcher.pending_count == 7


def test_watcher_retries_count_against_the_post_budget(tmp_path: Path) -> None:
    harness = backlogged(tmp_path, 2, max_delivery_attempts=1)
    harness.sink.fail = 10
    retrying = harness.build(max_delivery_attempts=3)
    retrying.flush(max_posts=2)
    assert retrying.last_posts == 2 and len(harness.sleeps) == 1  # no sleep past the budget
    assert retrying.pending_count == 2


def test_pending_terminal_flag_follows_the_outbox(tmp_path: Path) -> None:
    harness = Harness(tmp_path, max_delivery_attempts=1)
    harness.sink.fail = 1
    harness.probe.default = {"state": "exited", "exit_code": 2}
    harness.watcher.tick()
    assert harness.watcher.has_pending_terminal
    rebuilt = harness.build(max_delivery_attempts=1)  # read from the outbox after a restart
    assert rebuilt.has_pending_terminal
    rebuilt.flush()
    assert not rebuilt.has_pending_terminal and rebuilt.pending_count == 0


def test_control_sink_without_channel_timeout_keeps_its_own() -> None:
    class Plain:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        def post(self, path: str, body: dict, key: str, *, timeout_seconds=None) -> dict:
            self.timeouts.append(timeout_seconds)
            return {}

    plain = Plain()
    assert ControlSink(plain).deliver(report()).outcome == "delivered"
    assert ControlSink(plain, timeout_seconds=2.0).deliver(report()).outcome == "delivered"
    assert plain.timeouts == [None, 2.0]
    assert json.dumps(report().to_body())  # the body is unchanged


# -- the watcher's own docker calls are short-bounded (they share the lease-renewal thread) ---


def test_default_docker_runner_is_replaced_by_a_short_bounded_one(tmp_path, monkeypatch):
    import subprocess

    from tessera_worker import cpu, stage_watch_runner
    from tessera_worker.stage_watch_config import StageWatchConfig

    seen = {}

    def fake_run(command, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = type("Adapter", (), {"run": staticmethod(cpu._docker)})()
    runner = stage_watch_runner.build_stage_watch_runner(
        StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "ks")),
        adapter=adapter,
        channel=object(),
        state_dir=tmp_path,
    )
    assert runner is not None
    assert runner._docker_run is stage_watch_runner.bounded_docker
    with pytest.raises(cpu.CpuError):
        runner._docker_run(["inspect", "x"])
    assert seen["timeout"] <= stage_watch_runner.INSPECT_TIMEOUT_SECONDS <= 5


def test_an_injected_docker_runner_is_left_alone(tmp_path):
    from tessera_worker import stage_watch_runner
    from tessera_worker.stage_watch_config import StageWatchConfig

    def mine(arguments):
        raise AssertionError("not called")

    adapter = type("Adapter", (), {"run": staticmethod(mine)})()
    runner = stage_watch_runner.build_stage_watch_runner(
        StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "ks")),
        adapter=adapter,
        channel=object(),
        state_dir=tmp_path,
    )
    assert runner is not None and runner._docker_run is mine
