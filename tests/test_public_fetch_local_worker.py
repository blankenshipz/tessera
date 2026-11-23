"""ADR 0097: the local worker supervisor fetches pinned public files before the container."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from test_runtime import ApiChannel
from test_sandbox_egress_control import BASE_SPEC, _world
from test_supervisor import FakeAdapter

from tessera_control.models import ExecutionSpec
from tessera_control.public_fetch_report import public_fetch_for_operation
from tessera_control.store import _connect
from tessera_worker.cpu import CpuError, CpuOperation, DockerCpuAdapter
from tessera_worker.public_fetch import fetch_public_files
from tessera_worker.runtime import CpuRuntime
from tessera_worker.supervisor import SupervisorError

BODY = b"public weights"
DIGEST = hashlib.sha256(BODY).hexdigest()
PLAN = {
    "hosts": ["example.org"],
    "files": [
        {
            "input": "weights",
            "name": "w.bin",
            "url": "https://example.org/w.bin",
            "sha256": DIGEST,
            "max_bytes": 100,
        }
    ],
    "max_bytes_total": 100,
    "cache": False,
}


class Recorder(FakeAdapter):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    def prepare(self, operation):
        self.log.append("prepare")
        return super().prepare(operation)

    def start(self, operation):
        self.log.append("start")
        return super().start(operation)


def _fetcher(log: list[str], body: bytes = BODY):
    def opener(url):
        return 200, {}, iter([body])

    def fetch(files, **kwargs):
        log.append("fetch")
        assert kwargs["redirect_hosts"] == ("example.org",)
        return fetch_public_files(files, opener=opener, **kwargs)

    return fetch


class RecordingChannel(ApiChannel):
    def __init__(self, state, log):
        super().__init__(state)
        self.log = log

    def post(self, path, body, key):
        self.log.append(path.rsplit("/", 1)[-1])
        return super().post(path, body, key)


def _runtime(tmp_path, state, worker, log, *, body=BODY, with_root=True):
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700, exist_ok=True)
    public = tmp_path / "public"
    public.mkdir(mode=0o700, exist_ok=True)
    adapter = Recorder(log)
    runtime = CpuRuntime(
        tmp_path / "runtime",
        inputs,
        RecordingChannel(state, log),
        adapter,
        worker_id=worker["id"],
        public_root=public if with_root else None,
        public_fetcher=_fetcher(log, body),
    )
    return runtime, adapter, public


def test_prefetch_precedes_create_and_report_reaches_control(tmp_path):
    state, _, worker, _, _, lease, op = _world(tmp_path / "w", {"public_fetch": PLAN})
    assert op["package"]["public_fetch"] == PLAN and op["package"]["network_profile"] == "none"
    log: list[str] = []
    runtime, adapter, public = _runtime(tmp_path, state, worker, log)
    with runtime:
        started = runtime.start(lease["id"], lease["epoch"])
        assert log.index("fetch") < log.index("prepare") < log.index("start")
        assert log.index("fetch") < log.index("inputs")
        assert started.phase == "running"
        operation = runtime.operation
        assert operation.network_profile == "none"
        runtime.report(started)
        runtime.report(started)
        operation_id = operation.operation_id
    assert log.count("public-fetch") == 1
    with _connect(state / "tessera.sqlite3") as connection:
        event = public_fetch_for_operation(connection, operation_id)
    assert event["via_control"] is False
    (row,) = event["files"]
    assert row["input"] == "weights" and row["source_host"] == "example.org"
    assert row["sha256"] == DIGEST and row["bytes"] == len(BODY) and row["cached"] is False
    assert "seconds" in row

    # The adapter mounts exactly the verified files read-only, and the network stays none.
    docker = DockerCpuAdapter(
        frozenset({operation.image}),
        input_root=tmp_path / "inputs",
        public_root=public,
    )
    arguments = docker.create_arguments(operation)
    assert arguments[arguments.index("--network") + 1] == "none"
    mount = (
        f"type=bind,src={operation.public_directory}/weights,dst=/inputs-public/weights,readonly"
    )
    assert mount in arguments
    directory = Path(operation.public_directory) / "weights"
    assert sorted(p.name for p in directory.iterdir()) == ["w.bin"]
    assert (directory / "w.bin").read_bytes() == BODY
    assert oct(os.stat(directory / "w.bin").st_mode & 0o777) == "0o444"
    assert oct(os.stat(directory).st_mode & 0o777) == "0o555"
    (directory.parent).chmod(0o700)
    directory.chmod(0o755)
    (directory / "extra").write_bytes(b"x")
    directory.chmod(0o555)
    with pytest.raises(CpuError):
        docker.create_arguments(operation)


def test_digest_mismatch_fails_closed_before_anything_is_created(tmp_path):
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", {"public_fetch": PLAN})
    log: list[str] = []
    runtime, adapter, public = _runtime(tmp_path, state, worker, log, body=b"tampered")
    with runtime, pytest.raises(SupervisorError, match="digest_mismatch"):
        runtime.start(lease["id"], lease["epoch"])
    assert "prepare" not in log and "start" not in log and "inputs" not in log
    assert list(public.iterdir()) == []
    assert not (tmp_path / "runtime" / "runtime.json").exists()


def test_worker_without_public_root_refuses(tmp_path):
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", {"public_fetch": PLAN})
    log: list[str] = []
    runtime, _, _ = _runtime(tmp_path, state, worker, log, with_root=False)
    with runtime, pytest.raises(SupervisorError, match="no public input root"):
        runtime.start(lease["id"], lease["epoch"])
    assert "prepare" not in log


def test_stage_without_public_fetch_is_unchanged(tmp_path):
    state, _, worker, _, _, lease, op = _world(tmp_path / "w", {})
    assert "public_fetch" not in op["package"]
    log: list[str] = []
    runtime, _, _ = _runtime(tmp_path, state, worker, log)
    with runtime:
        started = runtime.start(lease["id"], lease["epoch"])
        runtime.report(started)
        assert "public_fetch" not in runtime.descriptor
        assert runtime.operation.public_directory is None
    assert "fetch" not in log and "public-fetch" not in log


def test_public_fetch_mismatch_between_package_and_plan_is_refused():
    package = {"public_fetch": PLAN, "network_profile": "none"}
    CpuRuntime._validate_public_fetch(package, {"public_fetch": PLAN})
    with pytest.raises(SupervisorError):
        CpuRuntime._validate_public_fetch(package, {})
    with pytest.raises(SupervisorError):
        CpuRuntime._validate_public_fetch({"network_profile": "none"}, {"public_fetch": PLAN})
    with pytest.raises(SupervisorError):
        CpuRuntime._validate_public_fetch(
            {"public_fetch": PLAN, "network_profile": "allowlist-proxy-v1"},
            {"public_fetch": PLAN},
        )


def test_operation_requires_public_files_with_directory():
    base = dict(
        operation_id="a" * 32,
        image="x@sha256:" + "0" * 64,
        argv=("x",),
        plan_digest="0" * 64,
        max_duration_seconds=1,
    )
    with pytest.raises(ValueError):
        CpuOperation(**base, public_directory="/p")
    with pytest.raises(ValueError):
        CpuOperation(**base, public_files=(("weights", "w.bin", DIGEST, 1),))


def test_control_refuses_reports_that_contradict_the_plan(tmp_path):
    state, _, worker, _, _, lease, op = _world(tmp_path / "w", {"public_fetch": PLAN})
    channel = ApiChannel(state)
    row = {
        "input": "weights",
        "name": "w.bin",
        "source_host": "example.org",
        "sha256": DIGEST,
        "bytes": 5,
        "seconds": 0.1,
        "cached": False,
    }
    path = f"/worker/operations/{op['package']['operation_id']}/public-fetch"

    def body(**changes):
        return {
            "lease_id": lease["id"],
            "expected_epoch": lease["epoch"],
            "files": [row | changes],
        }

    for index, changes in enumerate(
        [{"sha256": "f" * 64}, {"source_host": "evil.example"}, {"bytes": 101}]
    ):
        with pytest.raises(ConnectionError, match="409"):
            channel.post(path, body(**changes), f"bad-{index}")
    assert channel.post(path, body(), "good")["recorded"] is True
    with pytest.raises(ConnectionError, match="409"):
        channel.post(path, body(bytes=6), "second")


def test_spec_without_public_fetch_serializes_as_before_and_is_local_cpu_only():
    plain = ExecutionSpec.model_validate(BASE_SPEC).model_dump(mode="json")
    assert "public_fetch" not in plain
    with_plan = ExecutionSpec.model_validate(BASE_SPEC | {"public_fetch": PLAN})
    assert with_plan.model_dump(mode="json")["public_fetch"] == PLAN
    with pytest.raises(ValueError):
        ExecutionSpec.model_validate(BASE_SPEC | {"public_fetch": PLAN | {"max_bytes_total": 5}})
    with pytest.raises(ValueError):
        ExecutionSpec.model_validate(
            BASE_SPEC | {"public_fetch": PLAN, "profile": "kubernetes-opencode"}
        )
