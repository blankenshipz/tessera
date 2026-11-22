from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from tessera_worker.stage_watcher import DockerInspectProbe, ProbeSample
from tessera_worker.stage_watcher.probe import parse_memory_usage

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OPERATION_ID = "0123456789abcdef0123456789abcdef"
LABELS = {"tessera.operation": OPERATION_ID}


def inspect_json(
    status: str = "running",
    *,
    running: bool | None = None,
    exit_code: object = 0,
    oom: object = False,
    restarts: object = 0,
    labels: dict | None = None,
) -> str:
    return json.dumps(
        [
            {
                "Id": "abc",
                "RestartCount": restarts,
                "Config": {"Labels": LABELS if labels is None else labels},
                "State": {
                    "Status": status,
                    "Running": status == "running" if running is None else running,
                    "ExitCode": exit_code,
                    "OOMKilled": oom,
                },
            }
        ]
    )


class FakeRun:
    def __init__(self, replies: dict[str, subprocess.CompletedProcess[str] | Exception]):
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(arguments))
        reply = self.replies[arguments[0]]
        if isinstance(reply, Exception):
            raise reply
        return reply


def done(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


def probe_for(inspect, stats_reply=None, **kwargs) -> tuple[DockerInspectProbe, FakeRun]:
    replies = {"inspect": inspect}
    if stats_reply is not None:
        replies["stats"] = stats_reply
    run = FakeRun(replies)
    kwargs.setdefault("expected_labels", LABELS)
    return DockerInspectProbe(run, "tessera-op", **kwargs), run


def test_running_container():
    probe, run = probe_for(done(inspect_json("running", restarts=2)))
    sample = probe.sample(NOW)
    assert sample == ProbeSample("running", NOW, restarts=2)
    assert run.calls == [["inspect", "--type", "container", "tessera-op"]]


def test_running_with_stats_and_output_size():
    stats = done(json.dumps({"MemUsage": "12.5MiB / 1.944GiB", "CPUPerc": "3.2%"}))
    probe, run = probe_for(done(inspect_json()), stats, stats=True, output_size=lambda: 4096)
    sample = probe.sample(NOW)
    assert sample.mem_bytes == int(12.5 * 1024**2)
    assert sample.output_bytes == 4096
    assert sample.cpu_ms is None
    assert run.calls[1] == ["stats", "--no-stream", "--format", "{{json .}}", "tessera-op"]


def test_stats_failure_never_fails_the_sample():
    for stats in (done("garbage"), done("", 1, "daemon gone"), RuntimeError("boom")):
        probe, _ = probe_for(done(inspect_json()), stats, stats=True)
        sample = probe.sample(NOW)
        assert sample.state == "running" and sample.mem_bytes is None


def test_output_size_failure_reads_as_none():
    def broken() -> int:
        raise OSError("gone")

    for size in (broken, lambda: -1, lambda: True, lambda: "12"):
        probe, _ = probe_for(done(inspect_json()), output_size=size)
        assert probe.sample(NOW).output_bytes is None


def test_exited_container():
    probe, _ = probe_for(done(inspect_json("exited", exit_code=3)))
    sample = probe.sample(NOW)
    assert (sample.state, sample.exit_code, sample.oom_killed) == ("exited", 3, False)


def test_oom_killed_container():
    probe, _ = probe_for(done(inspect_json("exited", exit_code=137, oom=True)))
    sample = probe.sample(NOW)
    assert (sample.state, sample.exit_code, sample.oom_killed) == ("exited", 137, True)


def test_absent_container():
    probe, _ = probe_for(done("[]", 1, "Error: No such container: tessera-op"))
    assert probe.sample(NOW).state == "absent"


def test_daemon_failure_is_unknown_not_absent():
    probe, _ = probe_for(done("", 1, "Cannot connect to the Docker daemon"))
    assert probe.sample(NOW).state == "unknown"


def test_run_raising_is_unknown():
    probe, _ = probe_for(OSError("docker missing"))
    assert probe.sample(NOW).state == "unknown"


@pytest.mark.parametrize(
    "stdout",
    [
        "garbage",
        "{}",
        "[]",
        "[1]",
        json.dumps([{}, {}]),
        json.dumps([{"State": {}}]),
        json.dumps([{"State": "running"}]),
        inspect_json("exited", exit_code="1"),
        inspect_json("exited", exit_code=True),
        inspect_json("exited", exit_code=None),
        inspect_json("running", oom="yes"),
        inspect_json("running", restarts=-1),
        inspect_json("running", restarts=True),
        inspect_json("created"),
        inspect_json("exited", running=True),
        json.dumps([{"Config": {"Labels": "x"}, "State": {"Status": "running"}}]),
    ],
)
def test_garbage_is_unknown(stdout):
    probe, _ = probe_for(done(stdout))
    assert probe.sample(NOW).state == "unknown"


@pytest.mark.parametrize(
    "labels",
    [{}, {"tessera.operation": "f" * 32}, {"other": OPERATION_ID}],
)
def test_foreign_label_is_unknown(labels):
    probe, _ = probe_for(done(inspect_json(labels=labels)))
    assert probe.sample(NOW).state == "unknown"


def test_no_expected_labels_accepts_any_owner():
    probe, _ = probe_for(done(inspect_json(labels={})), expected_labels=None)
    assert probe.sample(NOW).state == "running"


@pytest.mark.parametrize("name", ["", "-rm", "--help", "a b", "x;y", None])
def test_container_name_validated(name):
    with pytest.raises(ValueError):
        DockerInspectProbe(FakeRun({}), name)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("512B / 1GiB", 512),
        ("1.5KiB / 1GiB", 1536),
        ("2MB / 1GB", 2_000_000),
        ("1GiB", 1024**3),
        ("lots", None),
        ("5 parsecs / 1GiB", None),
        (None, None),
    ],
)
def test_parse_memory_usage(value, expected):
    assert parse_memory_usage(value) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "creating"},
        {"observed_at": datetime(2026, 1, 1)},
        {"exit_code": True},
        {"oom_killed": 1},
        {"output_bytes": -1},
        {"cpu_ms": 1.5},
        {"mem_bytes": True},
        {"restarts": -1},
    ],
)
def test_probe_sample_validation(overrides):
    fields = {"state": "running", "observed_at": NOW}
    fields.update(overrides)
    with pytest.raises(ValueError):
        ProbeSample(**fields)
