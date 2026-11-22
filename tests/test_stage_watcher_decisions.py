"""Stage watcher decision requests and the stats flag (ADR 0087 section 4). Fakes only.

The watcher only REQUESTS a retry or resize: it records the request in its outbox, then posts
it to the watch-decisions route. It never performs one.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tessera_worker.control_channel import ControlChannelError
from tessera_worker.cpu import CpuOperation
from tessera_worker.stage_watch_config import StageWatchConfig
from tessera_worker.stage_watch_runner import StageWatchRunner
from tessera_worker.stage_watcher import (
    ControlDecisionSink,
    ControlSink,
    DecisionEnvelope,
    DecisionRequest,
    KillSwitch,
    ProbeSample,
    StageContract,
    StageWatcher,
    WatchOutbox,
    decide,
    decision_idempotency_key,
    decision_settles,
    watch_decisions_path,
)
from tessera_worker.supervisor import Lease

OPERATION_ID = "0123456789abcdef0123456789abcdef"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DECISIONS_PATH = f"/worker/operations/{OPERATION_ID}/watch-decisions"
GIB = 1024**3


def make_contract(**overrides: Any) -> StageContract:
    fields: dict[str, Any] = {
        "operation_id": OPERATION_ID,
        "lease_id": "lease-1",
        "epoch": 3,
        "stage_key": "train",
        "provider": "local",
        "deadline_seconds": 3600,
        "silent_seconds": 300,
        "heartbeat_seconds": 60,
        "max_reports": 100,
        "kill_switch_path": None,
    }
    fields.update(overrides)
    return StageContract(**fields)


ENVELOPE: dict[str, Any] = {
    "retry_attempts_remaining": 2,
    "transient_exit_codes": (75, 137),
    "memory_bytes_current": 2 * GIB,
    "memory_bytes_max": 3 * GIB,
}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Probe:
    def __init__(self, *script: dict[str, Any]) -> None:
        self.script = list(script)

    def sample(self, now: datetime) -> ProbeSample:
        fields = self.script.pop(0) if self.script else {"state": "running"}
        return ProbeSample(observed_at=now, **fields)


class Channel:
    """The worker control channel: records every post; fails chosen posts."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any], str]] = []
        self.decision_errors: list[ControlChannelError] = []

    def post(
        self, path: str, body: dict[str, Any], key: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.posts.append((path, body, key))
        if path.endswith("/watch-decisions") and self.decision_errors:
            raise self.decision_errors.pop(0)
        return {"recorded": True}

    def decisions(self) -> list[tuple[str, dict[str, Any], str]]:
        return [post for post in self.posts if post[0].endswith("/watch-decisions")]

    def report_kinds(self) -> list[str]:
        return [body["kind"] for path, body, _ in self.posts if path.endswith("/watch-reports")]


@pytest.fixture
def clear_switch(tmp_path: Path) -> KillSwitch:
    directory = tmp_path / "operator"
    directory.mkdir()
    return KillSwitch(str(directory / "stop-agents"))


def build(
    tmp_path: Path,
    contract: StageContract,
    probe: Probe,
    channel: Channel,
    kill_switch: KillSwitch,
    clock: Clock | None = None,
) -> StageWatcher:
    return StageWatcher(
        contract,
        probe,
        ControlSink(channel),
        kill_switch,
        clock or Clock(),
        WatchOutbox(tmp_path / "watch.sqlite3"),
        sleep=lambda _seconds: None,
        max_delivery_attempts=1,
        decision_sink=ControlDecisionSink(channel),
    )


def exit_with(code: int, oom: bool = False) -> dict[str, Any]:
    return {"state": "exited", "exit_code": code, "oom_killed": oom}


# -- contract envelope ------------------------------------------------------------------------


def test_envelope_is_optional_and_keeps_existing_digests() -> None:
    plain = make_contract()
    assert not plain.has_envelope
    assert "retry_attempts_remaining" not in json.loads(plain.canonical_json())
    # A pinned digest of the pre-envelope canonical form: unchanged by the new fields.
    legacy = {
        key: value
        for key, value in json.loads(plain.canonical_json()).items()
        if key not in ENVELOPE
    }
    assert plain.canonical_json() == json.dumps(
        legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    enveloped = make_contract(**ENVELOPE)
    assert enveloped.has_envelope and enveloped.digest() != plain.digest()
    assert json.loads(enveloped.canonical_json())["transient_exit_codes"] == [75, 137]
    # Canonical order: the same code set gives the same digest.
    assert make_contract(**{**ENVELOPE, "transient_exit_codes": (137, 75)}).digest() == (
        enveloped.digest()
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"retry_attempts_remaining": 17},
        {"retry_attempts_remaining": -1},
        {"retry_attempts_remaining": True},
        {"transient_exit_codes": tuple(range(1, 10))},
        {"transient_exit_codes": (0,)},
        {"transient_exit_codes": (256,)},
        {"transient_exit_codes": (3, 3)},
        {"memory_bytes_current": 0},
        {"memory_bytes_current": 4 * GIB, "memory_bytes_max": 3 * GIB},
    ],
)
def test_envelope_is_bounded(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_contract(**bad)


# -- the closed request -----------------------------------------------------------------------


def test_request_body_key_and_path_are_closed() -> None:
    digest = make_contract().digest()
    retry = DecisionRequest(
        OPERATION_ID, "lease-1", 3, "retry_request", "transient_exit", T0, 0, None, digest
    )
    assert retry.to_body() == {
        "lease_id": "lease-1",
        "expected_epoch": 3,
        "kind": "retry_request",
        "reason": "transient_exit",
        "observed_at": T0.isoformat(),
        "decision_seq": 0,
        "contract_digest": digest,
    }
    assert retry.idempotency_key == f"watch-decision-{OPERATION_ID}-0"
    assert len(decision_idempotency_key(OPERATION_ID, 2**53)) <= 128
    assert watch_decisions_path(OPERATION_ID) == DECISIONS_PATH
    resize = DecisionRequest(
        OPERATION_ID, "lease-1", 3, "resize_request", "oom_killed", T0, 1, 4 * GIB, digest
    )
    assert resize.to_body()["requested_memory_bytes"] == 4 * GIB
    assert DecisionRequest.from_body(OPERATION_ID, resize.to_body()) == resize
    for kind, reason, memory in (
        ("retry_request", "oom_killed", None),  # pairing is closed
        ("resize_request", "oom_killed", None),  # a resize names its size
        ("retry_request", "transient_exit", 5),  # only a resize carries a size
        ("reroute_request", "transient_exit", None),
    ):
        with pytest.raises(ValueError):
            DecisionRequest(OPERATION_ID, "lease-1", 3, kind, reason, T0, 0, memory, digest)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DecisionRequest(
            OPERATION_ID, "lease-1", 3, "retry_request", "transient_exit", T0, 0, None, digest, True
        )
    with pytest.raises(ValueError):
        DecisionRequest.from_body(OPERATION_ID, {**retry.to_body(), "note": "x"})


def test_decide_is_deterministic_and_bounded_by_the_envelope() -> None:
    def sample(**fields: Any) -> ProbeSample:
        return ProbeSample(observed_at=T0, **fields)

    full = make_contract(**ENVELOPE)
    assert decide(make_contract(), sample(**exit_with(75))) == ()
    assert decide(make_contract(), sample(**exit_with(1, oom=True))) == ()
    (retry,) = decide(full, sample(**exit_with(75)))
    assert (retry.kind, retry.reason, retry.requested_memory_bytes) == (
        "retry_request",
        "transient_exit",
        None,
    )
    assert decide(full, sample(**exit_with(1))) == ()  # not a transient code
    assert decide(full, sample(**exit_with(0))) == ()
    assert decide(full, sample(state="running")) == ()
    assert decide(full, sample(state="unknown")) == ()  # probe_lost is never produced
    no_attempts = make_contract(**{**ENVELOPE, "retry_attempts_remaining": 0})
    assert decide(no_attempts, sample(**exit_with(75))) == ()
    (resize,) = decide(full, sample(**exit_with(137, oom=True)))  # OOM is never a retry
    assert (resize.kind, resize.reason, resize.requested_memory_bytes) == (
        "resize_request",
        "oom_killed",
        3 * GIB,  # min(2 x 2 GiB, 3 GiB)
    )
    roomy = make_contract(**{**ENVELOPE, "memory_bytes_max": 16 * GIB})
    assert decide(roomy, sample(**exit_with(1, oom=True)))[0].requested_memory_bytes == 4 * GIB
    at_max = make_contract(**{**ENVELOPE, "memory_bytes_current": 3 * GIB})
    assert decide(at_max, sample(**exit_with(1, oom=True))) == ()
    no_max = make_contract(**{**ENVELOPE, "memory_bytes_max": None})
    assert decide(no_max, sample(**exit_with(1, oom=True))) == ()


# -- the watcher ------------------------------------------------------------------------------


def test_no_envelope_means_no_decisions_ever(tmp_path: Path, clear_switch: KillSwitch) -> None:
    channel = Channel()
    watcher = build(
        tmp_path, make_contract(), Probe(exit_with(75, oom=True)), channel, clear_switch
    )
    watcher.tick()
    assert channel.report_kinds() == ["failed"]
    assert channel.decisions() == [] and watcher.decision_requests == 0
    assert WatchOutbox(tmp_path / "watch.sqlite3").recorded_decisions() == []


def test_transient_exit_files_exactly_one_retry_request(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    contract = make_contract(**ENVELOPE)
    watcher = build(tmp_path, contract, Probe(exit_with(75)), channel, clear_switch)
    watcher.tick()
    watcher.tick()
    assert channel.report_kinds() == ["failed"]  # the failed report still goes out
    assert channel.decisions() == [
        (
            DECISIONS_PATH,
            {
                "lease_id": "lease-1",
                "expected_epoch": 3,
                "kind": "retry_request",
                "reason": "transient_exit",
                "observed_at": T0.isoformat(),
                "decision_seq": 0,
                "contract_digest": contract.digest(),
            },
            f"watch-decision-{OPERATION_ID}-0",
        )
    ]
    assert watcher.decision_requests == 1 and watcher.finished
    gaps = WatchOutbox(tmp_path / "watch.sqlite3").gaps()
    assert (gaps.decisions_delivered, gaps.decisions_pending) == (1, 0)


@pytest.mark.parametrize(
    ("envelope", "sample"),
    [
        ({**ENVELOPE, "retry_attempts_remaining": 0}, exit_with(75)),
        (ENVELOPE, exit_with(2)),
        ({**ENVELOPE, "memory_bytes_max": None}, exit_with(75, oom=True)),
    ],
)
def test_no_retry_request_outside_the_envelope(
    tmp_path: Path, clear_switch: KillSwitch, envelope: dict[str, Any], sample: dict[str, Any]
) -> None:
    channel = Channel()
    watcher = build(tmp_path, make_contract(**envelope), Probe(sample), channel, clear_switch)
    watcher.tick()
    assert channel.report_kinds() == ["failed"]
    assert channel.decisions() == []


def test_oom_files_one_resize_request(tmp_path: Path, clear_switch: KillSwitch) -> None:
    channel = Channel()
    watcher = build(
        tmp_path, make_contract(**ENVELOPE), Probe(exit_with(137, oom=True)), channel, clear_switch
    )
    watcher.tick()
    ((path, body, key),) = channel.decisions()
    assert path == DECISIONS_PATH and key == f"watch-decision-{OPERATION_ID}-0"
    assert (body["kind"], body["reason"], body["requested_memory_bytes"]) == (
        "resize_request",
        "oom_killed",
        3 * GIB,
    )


def test_oom_at_max_files_nothing(tmp_path: Path, clear_switch: KillSwitch) -> None:
    channel = Channel()
    contract = make_contract(**{**ENVELOPE, "memory_bytes_current": 3 * GIB})
    watcher = build(tmp_path, contract, Probe(exit_with(137, oom=True)), channel, clear_switch)
    watcher.tick()
    assert channel.report_kinds() == ["failed"] and channel.decisions() == []


def test_kill_switch_engaged_files_nothing_but_still_reports(tmp_path: Path) -> None:
    channel = Channel()
    engaged = KillSwitch(None)  # no marker path reads as engaged (fail closed)
    watcher = build(tmp_path, make_contract(**ENVELOPE), Probe(exit_with(75)), channel, engaged)
    watcher.tick()
    assert channel.report_kinds() == ["failed"]
    assert channel.posts[0][1]["kill_switch_engaged"] is True
    assert channel.decisions() == [] and watcher.decision_requests == 0


def test_no_decision_sink_means_no_requests(tmp_path: Path, clear_switch: KillSwitch) -> None:
    channel = Channel()
    watcher = StageWatcher(
        make_contract(**ENVELOPE),
        Probe(exit_with(75)),
        ControlSink(channel),
        clear_switch,
        Clock(),
        WatchOutbox(tmp_path / "watch.sqlite3"),
    )
    watcher.tick()
    assert channel.report_kinds() == ["failed"] and channel.decisions() == []


def test_request_is_recorded_before_it_is_posted(tmp_path: Path, clear_switch: KillSwitch) -> None:
    seen: list[int] = []
    outbox_path = tmp_path / "watch.sqlite3"

    class CheckingSink:
        def deliver_decision(self, request: DecisionRequest):  # noqa: ANN202
            seen.append(len(WatchOutbox(outbox_path).pending_decisions()))
            return ControlDecisionSink(Channel()).deliver_decision(request)

    watcher = StageWatcher(
        make_contract(**ENVELOPE),
        Probe(exit_with(75)),
        ControlSink(Channel()),
        clear_switch,
        Clock(),
        WatchOutbox(outbox_path),
        decision_sink=CheckingSink(),
    )
    watcher.tick()
    assert seen == [1]


def test_restart_does_not_resend_and_the_sequence_continues(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    contract = make_contract(**ENVELOPE)
    first = build(tmp_path, contract, Probe(exit_with(75)), channel, clear_switch)
    first.tick()
    assert len(channel.decisions()) == 1
    # A restarted watcher over the same outbox: the watch is already done, nothing is re-sent.
    second = build(tmp_path, contract, Probe(exit_with(75)), channel, clear_switch)
    second.tick()
    second.flush()
    assert len(channel.decisions()) == 1 and second.finished
    assert second._decision_seq == 1  # persisted: the next key is never a reused one
    outbox = WatchOutbox(tmp_path / "watch.sqlite3")
    assert outbox.next_decision_seq() == 1
    # At most one request per kind per epoch, enforced by the outbox itself.
    duplicate = DecisionRequest(
        OPERATION_ID,
        "lease-1",
        3,
        "retry_request",
        "transient_exit",
        T0,
        7,
        None,
        contract.digest(),
    )
    with pytest.raises(ValueError):
        outbox.record_decision(duplicate)


def test_transient_failure_retries_under_the_same_key(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    channel.decision_errors = [
        ControlChannelError("control rejected request", 503),
        ControlChannelError("control rejected request", 409),  # no permanent code: transient
        ControlChannelError("unavailable"),
    ]
    clock = Clock()
    watcher = build(
        tmp_path, make_contract(**ENVELOPE), Probe(exit_with(75)), channel, clear_switch, clock
    )
    watcher.tick()
    assert not watcher.finished
    for _ in range(3):
        clock.advance(5)
        watcher.tick()
    keys = [key for _, _, key in channel.decisions()]
    assert keys == [f"watch-decision-{OPERATION_ID}-0"] * 4
    assert len({json.dumps(body, sort_keys=True) for _, body, _ in channel.decisions()}) == 1
    assert watcher.finished and watcher.decision_refused == 0


def test_permanent_refusal_settles_and_is_never_resent(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    channel.decision_errors = [
        ControlChannelError("refused", 409, refusal_reason="stale_epoch"),
    ]
    clock = Clock()
    watcher = build(
        tmp_path, make_contract(**ENVELOPE), Probe(exit_with(75)), channel, clear_switch, clock
    )
    watcher.tick()
    clock.advance(60)
    watcher.tick()
    assert len(channel.decisions()) == 1
    assert watcher.decision_refused == 1 and watcher.finished
    outbox = WatchOutbox(tmp_path / "watch.sqlite3")
    assert outbox.decision_state(0) == "refused" and outbox.gaps().decisions_refused == 1
    assert decision_settles(422) and decision_settles(409, "stale_epoch")
    assert not decision_settles(409) and not decision_settles(401) and not decision_settles(503)


def test_undelivered_request_is_abandoned_after_24h_and_counted(
    tmp_path: Path, clear_switch: KillSwitch
) -> None:
    channel = Channel()
    channel.decision_errors = [ControlChannelError("down", 503)] * 2
    clock = Clock()
    watcher = build(
        tmp_path, make_contract(**ENVELOPE), Probe(exit_with(75)), channel, clear_switch, clock
    )
    watcher.tick()
    clock.advance(25 * 3600)
    watcher.tick()
    assert watcher.finished
    gaps = WatchOutbox(tmp_path / "watch.sqlite3").gaps()
    assert (gaps.decisions_abandoned, gaps.decisions_pending) == (1, 0)


FORBIDDEN = (
    "perform",
    "retry",
    "resize",
    "reroute",
    "re_route",
    "stop",
    "cancel",
    "restart",
    "admit",
)


def test_watcher_and_runner_expose_no_action(tmp_path: Path, clear_switch: KillSwitch) -> None:
    watcher = build(tmp_path, make_contract(**ENVELOPE), Probe(), Channel(), clear_switch)
    runner = StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=clear_switch.path),
        state_dir=tmp_path,
        channel=Channel(),
        docker_run=lambda _arguments: subprocess.CompletedProcess([], 1, "", ""),
    )
    for subject in (watcher, runner, StageWatcher, StageWatchRunner):
        for name in dir(subject):
            lowered = name.lower()
            assert not any(
                lowered.startswith(word) or lowered.startswith("_" + word) for word in FORBIDDEN
            ), name
        for word in FORBIDDEN:
            assert not hasattr(subject, word)
            assert not hasattr(subject, f"{word}_request")


# -- the runner: stats and decision counters ---------------------------------------------------

IMAGE = "python@sha256:" + "a" * 64
OPERATION = CpuOperation("b" * 32, IMAGE, ("python", "-c", "print(2)"), "c" * 64, 3600)


class StatsDocker:
    """A Docker command runner that answers inspect and stats for one container."""

    def __init__(self) -> None:
        self.status = "running"
        self.exit_code = 0
        self.oom = False
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if arguments[:1] == ("stats",):
            payload = {"MemUsage": "512MiB / 2GiB", "CPUPerc": "12.5%"}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        info = {
            "Config": {
                "Labels": {
                    "tessera.operation": OPERATION.operation_id,
                    "tessera.plan": OPERATION.plan_digest,
                }
            },
            "State": {
                "Status": self.status,
                "Running": self.status == "running",
                "ExitCode": self.exit_code,
                "OOMKilled": self.oom,
            },
            "RestartCount": 0,
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps([info]), "")


def lease(clock: Clock) -> Lease:
    return Lease(
        id="lease-1",
        operation_id=OPERATION.operation_id,
        plan_digest=OPERATION.plan_digest,
        epoch=1,
        expires_at=clock() + timedelta(hours=2),
        task_revision=1,
        project_id="project-1",
    )


def runner_for(tmp_path: Path, docker: StatsDocker, channel: Channel, clock: Clock, **config: Any):  # noqa: ANN201
    switch = tmp_path / "operator"
    switch.mkdir(parents=True, exist_ok=True)
    return StageWatchRunner(
        StageWatchConfig(enabled=True, kill_switch_path=str(switch / "stop"), **config),
        state_dir=tmp_path,
        channel=channel,
        docker_run=docker,
        clock=clock,
    )


def test_stats_flag_puts_mem_bytes_in_heartbeat_counters(tmp_path: Path) -> None:
    assert StageWatchConfig().stats is False
    with pytest.raises(ValueError):
        StageWatchConfig(stats=1)  # type: ignore[arg-type]
    clock, channel, docker = Clock(), Channel(), StatsDocker()
    runner = runner_for(tmp_path / "on", docker, channel, clock, stats=True)
    runner.on_start(OPERATION, lease(clock))
    runner.tick()
    (body,) = [body for path, body, _ in channel.posts if body["kind"] == "heartbeat"]
    assert body["counters"]["mem_bytes"] == 512 * 1024**2
    assert any(call[0] == "stats" for call in docker.calls)

    off_channel, off_docker = Channel(), StatsDocker()
    off = runner_for(tmp_path / "off", off_docker, off_channel, clock)
    off.on_start(OPERATION, lease(clock))
    off.tick()
    (body,) = [body for path, body, _ in off_channel.posts if body["kind"] == "heartbeat"]
    assert "mem_bytes" not in body["counters"]
    assert not any(call[0] == "stats" for call in off_docker.calls)


def test_cli_stats_flag_reaches_the_config() -> None:
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
        "--stage-watcher",
        "--stage-watcher-kill-switch",
        "/run/stop",
    ]
    assert _stage_watch(parser().parse_args(base)).stats is False
    assert _stage_watch(parser().parse_args([*base, "--stage-watcher-stats"])).stats is True


def test_runner_counts_requests_and_refusals_without_ids(tmp_path: Path) -> None:
    clock, channel, docker = Clock(), Channel(), StatsDocker()
    runner = runner_for(tmp_path, docker, channel, clock)
    envelope = DecisionEnvelope(retry_attempts_remaining=1, transient_exit_codes=(75,))
    runner.on_start(OPERATION, lease(clock), envelope)
    runner.tick()
    docker.status, docker.exit_code = "exited", 75
    channel.decision_errors = [ControlChannelError("refused", 422)]
    clock.advance(10)
    runner.tick()
    status = runner.status()
    assert (status["decision_requests"], status["decision_refused"]) == (1, 1)
    assert OPERATION.operation_id not in json.dumps(status)
    ((path, body, key),) = channel.decisions()
    assert path == f"/worker/operations/{OPERATION.operation_id}/watch-decisions"
    assert (body["kind"], body["expected_epoch"]) == ("retry_request", 1)
    assert key == f"watch-decision-{OPERATION.operation_id}-0"
    # The stored contract carries the envelope, so a restarted runner keeps it.
    stored = json.loads(
        (tmp_path / "stage-watch" / f"{OPERATION.operation_id}.contract.json").read_text()
    )
    assert stored["contract"]["transient_exit_codes"] == [75]
    again = runner_for(tmp_path, docker, channel, clock)
    again.on_start(OPERATION, lease(clock))
    assert again._entries[OPERATION.operation_id].watcher._contract.retry_attempts_remaining == 1


def test_runner_without_envelope_files_nothing(tmp_path: Path) -> None:
    clock, channel, docker = Clock(), Channel(), StatsDocker()
    runner = runner_for(tmp_path, docker, channel, clock)
    runner.on_start(OPERATION, lease(clock))
    docker.status, docker.exit_code, docker.oom = "exited", 137, True
    runner.tick()
    assert channel.report_kinds() == ["failed"] and channel.decisions() == []
    assert runner.status()["decision_requests"] == 0
