from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tessera_worker.cpu import CpuError, CpuOperation, DevelopmentOutputMount, DockerCpuAdapter
from tessera_worker.inputs import materialize_snapshot
from tessera_worker.supervisor import CpuSupervisor

IMAGE = "python@sha256:" + "a" * 64


def operation() -> CpuOperation:
    return CpuOperation("b" * 32, IMAGE, ("python", "-c", "print(2 + 2)"), "c" * 64, 10)


def staged_operation(parent: Path) -> CpuOperation:
    content = b"verified input"
    manifest = {
        "root": "/original/source",
        "entries": [
            {
                "relative_path": "data/input.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "classification": "unclassified",
                "provenance": "imported_unverified",
            }
        ],
        "exclusions": [],
        "total_bytes": len(content),
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = materialize_snapshot(
        {
            "receipt": {
                "lease_id": "lease-fixture",
                "epoch": 1,
                "worker_principal_id": "worker-fixture",
                "snapshot_id": "snapshot-fixture",
                "snapshot_digest": digest,
                "delivery_confirmed": False,
            },
            "snapshot": {
                "id": "snapshot-fixture",
                "digest": digest,
                "manifest": manifest,
                "files": [
                    {"relative_path": "data/input.txt", "content_base64": "dmVyaWZpZWQgaW5wdXQ="}
                ],
            },
        },
        parent,
        expected_snapshot_digest=digest,
        expected_lease_id="lease-fixture",
        expected_epoch=1,
        expected_worker_id="worker-fixture",
    )
    return CpuOperation(
        "b" * 32,
        IMAGE,
        ("python", "-c", "print(2 + 2)"),
        "c" * 64,
        10,
        input_directory=str(snapshot.directory),
        input_manifest_json=snapshot.manifest_json,
    )


class FakeDocker:
    def __init__(self) -> None:
        self.state = "absent"
        self.calls: list[list[str]] = []
        self.lose_create_reply = False
        self.unreachable = False
        self.wrong_owner = False
        self.unknown_kill = False

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.unreachable:
            return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to Docker daemon")
        if argv[0] == "inspect":
            if self.state == "absent":
                return subprocess.CompletedProcess(argv, 1, "", "No such container")
            op = operation()
            response = [
                {
                    "Id": "container-1",
                    "Config": {
                        "Labels": {
                            "tessera.operation": "wrong" if self.wrong_owner else op.operation_id,
                            "tessera.plan": op.plan_digest,
                        }
                    },
                    "State": {
                        "Status": self.state,
                        "Running": self.state == "running",
                        "ExitCode": 0,
                    },
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")
        if argv[0] == "create":
            self.state = "created"
            if self.lose_create_reply:
                raise CpuError("lost reply")
        if argv[0] == "start":
            self.state = "running"
        if argv[0] == "kill":
            if self.unknown_kill:
                self.unreachable = True
                raise CpuError("lost kill")
            self.state = "exited"
        return subprocess.CompletedProcess(argv, 0, "container-1", "")


def test_lost_create_is_reconciled_and_terminal_job_is_not_restarted() -> None:
    docker = FakeDocker()
    docker.lose_create_reply = True
    adapter = DockerCpuAdapter(frozenset({IMAGE}), docker)
    op = operation()
    assert adapter.prepare(op).state == "created"
    assert adapter.prepare(op).state == "created"
    assert len([call for call in docker.calls if call[0] == "create"]) == 1
    assert adapter.start(op).state == "running"
    assert adapter.start(op).state == "running"
    docker.state = "exited"
    assert adapter.start(op).state == "exited"
    assert len([call for call in docker.calls if call[0] == "start"]) == 1


def test_daemon_failure_is_unknown_and_never_causes_a_new_submission() -> None:
    docker = FakeDocker()
    docker.unreachable = True
    adapter = DockerCpuAdapter(frozenset({IMAGE}), docker)
    assert adapter.prepare(operation()).state == "unknown"
    assert all(call[0] == "inspect" for call in docker.calls)


def test_foreign_container_cannot_be_started_or_cancelled() -> None:
    docker = FakeDocker()
    docker.state = "running"
    docker.wrong_owner = True
    adapter = DockerCpuAdapter(frozenset({IMAGE}), docker)
    with pytest.raises(CpuError, match="ownership"):
        adapter.cancel(operation())
    assert all(call[0] == "inspect" for call in docker.calls)


def test_stop_confirmation_requires_observed_provider_state() -> None:
    docker = FakeDocker()
    adapter = DockerCpuAdapter(frozenset({IMAGE}), docker)
    op = operation()
    adapter.prepare(op)
    adapter.start(op)
    docker.unknown_kill = True
    assert adapter.cancel(op).termination_confirmed is False
    docker.unreachable = False
    docker.unknown_kill = False
    assert adapter.cancel(op).termination_confirmed is True


def test_profile_has_no_host_mounts_network_or_mutable_image_tags() -> None:
    adapter = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker())
    args = adapter.create_arguments(operation())
    for flag, value in (
        ("--network", "none"),
        ("--cpus", "1"),
        ("--pids-limit", "64"),
        ("--cap-drop", "ALL"),
        ("--user", "65532:65532"),
        ("--security-opt", "no-new-privileges=true"),
    ):
        assert args[args.index(flag) + 1] == value
    assert "--read-only" in args
    assert not set(args).intersection({"--privileged", "--volume", "-v", "--mount", "--env", "-e"})
    assert args[-4:] == ["python", IMAGE, "-c", "print(2 + 2)"]
    with pytest.raises(ValueError):
        DockerCpuAdapter(frozenset({"python:latest"}))
    assert adapter.cost_observation()["infrastructure_cost_cents"] is None


def test_verified_snapshot_gets_exactly_one_read_only_inputs_mount(tmp_path: Path) -> None:
    op = staged_operation(tmp_path)
    adapter = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), input_root=tmp_path)
    args = adapter.create_arguments(op)
    mounts = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--mount"]
    assert mounts == [f"type=bind,src={op.input_directory},dst=/inputs,readonly"]
    assert "--volume" not in args and "-v" not in args


@pytest.mark.parametrize("change", ["bytes", "symlink", "extra"])
def test_changed_or_extra_snapshot_is_rejected_before_create(tmp_path: Path, change: str) -> None:
    op = staged_operation(tmp_path)
    directory = Path(op.input_directory)
    target = directory / "data/input.txt"
    target.parent.chmod(0o755)
    if change == "bytes":
        target.chmod(0o644)
        target.write_bytes(b"tampered bytes")
        target.chmod(0o444)
    elif change == "symlink":
        target.unlink()
        target.symlink_to("/etc/hosts")
    else:
        directory.chmod(0o755)
        extra = directory / "extra"
        extra.mkdir()
        extra.chmod(0o555)
        directory.chmod(0o555)
    target.parent.chmod(0o555)
    docker = FakeDocker()
    with pytest.raises(CpuError):
        DockerCpuAdapter(frozenset({IMAGE}), docker, input_root=tmp_path).prepare(op)
    assert not any(call[0] == "create" for call in docker.calls)


def test_input_path_escape_or_unconfigured_root_is_denied(tmp_path: Path) -> None:
    op = staged_operation(tmp_path)
    with pytest.raises(CpuError, match="not configured"):
        DockerCpuAdapter(frozenset({IMAGE}), FakeDocker()).create_arguments(op)
    escaped = CpuOperation(
        op.operation_id,
        op.image,
        op.argv,
        op.plan_digest,
        op.max_duration_seconds,
        input_directory=str(tmp_path.parent),
        input_manifest_json=op.input_manifest_json,
    )
    with pytest.raises(CpuError, match="escapes"):
        DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), input_root=tmp_path).create_arguments(
            escaped
        )
    manifest_digest = hashlib.sha256(op.input_manifest_json.encode()).hexdigest()
    link = tmp_path / f"tessera-input-{manifest_digest}-alias"
    link.symlink_to(op.input_directory, target_is_directory=True)
    relinked = CpuOperation(
        op.operation_id,
        op.image,
        op.argv,
        op.plan_digest,
        op.max_duration_seconds,
        input_directory=str(link),
        input_manifest_json=op.input_manifest_json,
    )
    with pytest.raises(CpuError, match="escapes"):
        DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), input_root=tmp_path).create_arguments(
            relinked
        )


def test_start_revalidates_the_snapshot_immediately_before_provider_start(tmp_path: Path) -> None:
    op = staged_operation(tmp_path)
    docker = FakeDocker()
    adapter = DockerCpuAdapter(frozenset({IMAGE}), docker, input_root=tmp_path)
    assert adapter.prepare(op).state == "created"
    target = Path(op.input_directory) / "data/input.txt"
    target.parent.chmod(0o755)
    target.chmod(0o644)
    target.write_bytes(b"tampered bytes")
    target.chmod(0o444)
    target.parent.chmod(0o555)
    with pytest.raises(CpuError):
        adapter.start(op)
    assert not any(call[0] == "start" for call in docker.calls)


def test_oversized_staged_file_is_rejected_before_open(tmp_path, monkeypatch):
    import os

    op = staged_operation(tmp_path)
    target = Path(op.input_directory) / "data/input.txt"
    target.chmod(0o644)
    with target.open("wb") as output:
        output.truncate(16 * 1024 * 1024 + 1)
    target.chmod(0o444)
    original = os.open

    def bounded_open(path, *args, **kwargs):
        if Path(path) == target:
            pytest.fail("Oversized snapshot file must be rejected before reading.")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", bounded_open)
    with pytest.raises(CpuError, match="manifest"):
        DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), input_root=tmp_path).prepare(op)


def test_input_identity_is_journaled_without_changing_legacy_no_input_payload(
    tmp_path: Path,
) -> None:
    legacy_payload = json.loads(CpuSupervisor._payload(operation()))
    assert "input_directory" not in legacy_payload
    assert "input_manifest_json" not in legacy_payload
    mounted_payload = json.loads(CpuSupervisor._payload(staged_operation(tmp_path)))
    assert mounted_payload["input_directory"].startswith(str(tmp_path))
    assert mounted_payload["input_manifest_json"]


def test_explicit_development_mount_is_fenced_and_normal_cpu_has_none(tmp_path: Path) -> None:
    root = tmp_path / "development-workspaces"
    root.mkdir(mode=0o700)
    workspace = root / operation().operation_id
    workspace.mkdir(mode=0o700)
    mounted = CpuOperation(
        operation().operation_id,
        IMAGE,
        operation().argv,
        operation().plan_digest,
        operation().max_duration_seconds,
        development_output=DevelopmentOutputMount(str(workspace)),
    )
    adapter = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), development_output_root=root)

    assert "--mount" not in adapter.create_arguments(operation())
    assert f"type=bind,src={workspace},dst=/outputs" in adapter.create_arguments(mounted)
    assert CpuSupervisor._payload(mounted) != CpuSupervisor._payload(operation())
    workspace.rmdir()
    workspace.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CpuError, match="escapes"):
        adapter.create_arguments(mounted)


def test_operator_resource_limits_override_defaults_within_bounds() -> None:
    default = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker()).create_arguments(operation())
    assert default[default.index("--memory") + 1] == str(256 * 1024 * 1024)
    assert f"size={16 * 1024 * 1024}" in default[default.index("--tmpfs") + 1]
    big_adapter = DockerCpuAdapter(
        frozenset({IMAGE}),
        FakeDocker(),
        memory_bytes=1024 * 1024 * 1024,
        scratch_bytes=64 * 1024 * 1024,
    )
    big = big_adapter.create_arguments(
        dataclasses.replace(operation(), **big_adapter.operation_limits())
    )
    assert big[big.index("--memory") + 1] == str(1024 * 1024 * 1024)
    assert big[big.index("--memory-swap") + 1] == str(1024 * 1024 * 1024)
    assert f"size={64 * 1024 * 1024}" in big[big.index("--tmpfs") + 1]
    # ADR 0059 raised the ceilings from 1 GiB / 64 MiB to 64 GiB; the lower bounds stand.
    for bad in (
        {"memory_bytes": 64 * 1024**3 + 1},
        {"memory_bytes": 1},
        {"scratch_bytes": 64 * 1024**3 + 1},
        {"scratch_bytes": 1024 * 1024 - 1},
    ):
        with pytest.raises(ValueError):
            DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), **bad)


def test_adapter_refuses_an_operation_whose_limits_differ_from_the_operator_limits() -> None:
    adapter = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), memory_bytes=1024 * 1024 * 1024)
    assert adapter.operation_limits() == {"memory_bytes": 1024 * 1024 * 1024}
    with pytest.raises(CpuError, match="limits differ"):
        adapter.create_arguments(operation())  # built without the runtime's limits
    assert DockerCpuAdapter(frozenset({IMAGE}), FakeDocker()).operation_limits() == {}


def test_container_user_defaults_to_legacy_and_is_operator_owned() -> None:
    args = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker()).create_arguments(operation())
    assert args[args.index("--user") + 1] == "65532:65532"
    adapter = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), container_user="1234:5678")
    assert adapter.operation_limits() == {"container_user": "1234:5678"}
    op = dataclasses.replace(operation(), **adapter.operation_limits())
    args = adapter.create_arguments(op)
    assert args[args.index("--user") + 1] == "1234:5678"
    assert args.count("--user") == 1
    with pytest.raises(CpuError, match="limits differ"):
        adapter.create_arguments(operation())  # a plan-built operation cannot pick its own user
    assert CpuSupervisor._payload(op) != CpuSupervisor._payload(operation())
    assert '"container_user":"1234:5678"' in CpuSupervisor._payload(op)


@pytest.mark.parametrize(
    "bad",
    ["1234", "root", "1234:", ":5", "a:b", "1:2:3", "-1:5", "01:5", " 1:5", "1:5\n", "4294967295:1"]
    + ["0:5", "5:0", "0:0", 1234, None],
)
def test_container_user_refusals(bad) -> None:
    if bad is None:
        assert DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), container_user=None)
        return
    with pytest.raises(ValueError):
        DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), container_user=bad)


def test_root_container_user_needs_the_explicit_override() -> None:
    adapter = DockerCpuAdapter(
        frozenset({IMAGE}),
        FakeDocker(),
        container_user="0:0",
        allow_root_container_user=True,
    )
    op = dataclasses.replace(operation(), **adapter.operation_limits())
    args = adapter.create_arguments(op)
    assert args[args.index("--user") + 1] == "0:0"
    # An adapter without the override refuses a root operation even if one is constructed.
    strict = DockerCpuAdapter(frozenset({IMAGE}), FakeDocker())
    with pytest.raises(CpuError, match="not permitted"):
        strict.create_arguments(dataclasses.replace(operation(), container_user="0:0"))
    assert 4294967294 == int(
        DockerCpuAdapter(
            frozenset({IMAGE}), FakeDocker(), container_user="4294967294:1"
        ).container_user.split(":")[0]
    )


def test_default_container_user_is_the_poller_identity_on_linux(monkeypatch) -> None:
    from tessera_worker import cpu

    monkeypatch.setattr(cpu.sys, "platform", "linux")
    monkeypatch.setattr(cpu.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(cpu.os, "getegid", lambda: 1002)
    assert cpu.default_container_user() == "1001:1002"
    monkeypatch.setattr(cpu.os, "geteuid", lambda: 0)
    assert cpu.default_container_user() is None  # a root poller must choose explicitly
    monkeypatch.setattr(cpu.sys, "platform", "darwin")
    monkeypatch.setattr(cpu.os, "geteuid", lambda: 501)
    assert cpu.default_container_user() is None
