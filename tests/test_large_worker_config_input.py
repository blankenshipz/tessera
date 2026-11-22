"""Operator output limit configuration and streaming input verification (large artifacts)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_cpu import IMAGE, FakeDocker

from tessera_control import artifact_limits as limits
from tessera_worker import cpu
from tessera_worker.cli import _poll_config, parser
from tessera_worker.cpu import CpuError, CpuOperation, DockerCpuAdapter
from tessera_worker.grant_poller import (
    CONFIG_SCHEMA,
    GrantPollerConfig,
    load_poller_config,
)
from tessera_worker.inputs import materialize_snapshot
from tessera_worker.service import DevelopmentWorkerConfig, DevelopmentWorkerError

MiB = 1024 * 1024


def _poller(**change: Any) -> GrantPollerConfig:
    values: dict[str, Any] = {
        "control_url": "http://127.0.0.1:1",
        "credential_file": Path("/nonexistent/credential"),
        "workspace_root": Path("/nonexistent/workspace"),
        "development_output_root": Path("/nonexistent/outputs"),
        "allowed_images": (IMAGE,),
    }
    values.update(change)
    return GrantPollerConfig(**values)


def _worker(**change: Any) -> DevelopmentWorkerConfig:
    values: dict[str, Any] = {
        "control_url": "http://127.0.0.1:1",
        "grant_id": "grant_1",
        "credential_file": Path("/nonexistent/credential"),
        "workspace_root": Path("/nonexistent/workspace"),
        "development_output_root": Path("/nonexistent/outputs"),
        "allowed_image": IMAGE,
    }
    values.update(change)
    return DevelopmentWorkerConfig(**values)


@pytest.mark.parametrize(
    "value", [None, 16 * MiB, 8 * 1024 * MiB, limits.ABSOLUTE_MAX_ARTIFACT_BYTES]
)
def test_max_output_bytes_accepts_the_documented_range(value: int | None) -> None:
    assert _poller(max_output_bytes=value).max_output_bytes == value
    assert _worker(max_output_bytes=value).max_output_bytes == value


@pytest.mark.parametrize(
    "value",
    [0, 16 * MiB - 1, limits.ABSOLUTE_MAX_ARTIFACT_BYTES + 1, True, "1073741824", 1.5 * MiB * 20],
)
def test_max_output_bytes_outside_the_range_is_refused(value: Any) -> None:
    with pytest.raises(DevelopmentWorkerError):
        _poller(max_output_bytes=value)
    with pytest.raises(DevelopmentWorkerError):
        _worker(max_output_bytes=value)


def _config_file(tmp_path: Path, **extra: Any) -> Path:
    document = {
        "schema": CONFIG_SCHEMA,
        "control_url": "http://127.0.0.1:1",
        "credential_file": str(tmp_path / "credential"),
        "workspace_root": str(tmp_path / "workspace"),
        "development_output_root": str(tmp_path / "outputs"),
        "allowed_images": [IMAGE],
        **extra,
    }
    path = tmp_path / "poller.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


def test_poller_config_file_carries_max_output_bytes(tmp_path: Path) -> None:
    assert load_poller_config(_config_file(tmp_path)).max_output_bytes is None
    config = load_poller_config(_config_file(tmp_path, max_output_bytes=64 * MiB))
    assert config.max_output_bytes == 64 * MiB
    with pytest.raises(DevelopmentWorkerError):
        load_poller_config(_config_file(tmp_path, max_output_bytes=MiB))


def test_cli_flag_reaches_the_poller_config() -> None:
    arguments = parser().parse_args(
        [
            "development-poll",
            "--control-url",
            "http://127.0.0.1:1",
            "--credential-file",
            "/nonexistent/credential",
            "--workspace-root",
            "/nonexistent/workspace",
            "--development-output-root",
            "/nonexistent/outputs",
            "--allowed-image",
            IMAGE,
            "--max-output-bytes",
            str(32 * MiB),
        ]
    )
    assert _poll_config(arguments).max_output_bytes == 32 * MiB
    run = parser().parse_args(
        [
            "development-run",
            "--control-url",
            "http://127.0.0.1:1",
            "--grant-id",
            "grant_1",
            "--credential-file",
            "/nonexistent/credential",
            "--workspace-root",
            "/nonexistent/workspace",
            "--development-output-root",
            "/nonexistent/outputs",
            "--allowed-image",
            IMAGE,
            "--max-output-bytes",
            str(32 * MiB),
        ]
    )
    assert run.max_output_bytes == 32 * MiB


# --- development output backend honours the operator cap -----------------------------------


def test_reservation_above_the_worker_cap_is_refused(tmp_path: Path) -> None:
    from tessera_worker.development_runtime import (
        DevelopmentOutputBackend,
        DevelopmentOutputProfile,
        DevelopmentRuntimeError,
    )

    root = tmp_path / "dev"
    root.mkdir(mode=0o700)
    profile = DevelopmentOutputProfile(root, 10 * 1024 * MiB, 64, worker_max_output_bytes=32 * MiB)
    backend = DevelopmentOutputBackend(profile)
    with pytest.raises(DevelopmentRuntimeError, match="configured maximum"):
        backend.reserve(
            operation_id="a" * 32,
            lease_id="lease_1",
            epoch=1,
            output_paths=("model.bin",),
            max_output_bytes=33 * MiB,
        )
    backend.reserve(
        operation_id="b" * 32,
        lease_id="lease_2",
        epoch=1,
        output_paths=("model.bin",),
        max_output_bytes=32 * MiB,
    )


# --- streaming input verification --------------------------------------------------------


PAYLOAD = bytes(range(256)) * 12 + b"end"  # 3075 bytes


def _staged(parent: Path, content: bytes) -> CpuOperation:
    manifest = {
        "root": "/original/source",
        "entries": [
            {
                "relative_path": "weights/model.bin",
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
                    {
                        "relative_path": "weights/model.bin",
                        "content_base64": base64.b64encode(content).decode(),
                    }
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


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "LEGACY_INLINE_BYTES", 1024)
    monkeypatch.setattr(limits, "CHUNK_BYTES", 256)


def test_multi_chunk_input_beyond_the_legacy_bound_is_mounted_with_bounded_reads(
    tmp_path: Path, tiny: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = _staged(tmp_path, PAYLOAD)
    monkeypatch.setattr(cpu, "MAX_INPUT_BYTES", 1024)  # the old fixed bound no longer applies
    reads: list[int] = []
    real_fdopen = os.fdopen

    class Spy:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> Spy:
            self._handle.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._handle.__exit__(*exc)

        def read(self, size: int = -1) -> bytes:
            data = self._handle.read(size)
            reads.append(len(data))
            return data

        def fileno(self) -> int:
            return self._handle.fileno()

    monkeypatch.setattr(cpu.os, "fdopen", lambda *a, **k: Spy(real_fdopen(*a, **k)))
    adapter = DockerCpuAdapter(
        frozenset({IMAGE}), FakeDocker(), input_root=tmp_path, max_input_bytes=2048 * 8
    )
    args = adapter.create_arguments(op)
    monkeypatch.undo()
    assert f"type=bind,src={op.input_directory},dst=/inputs,readonly" in args
    assert reads and max(reads) <= 256 and sum(reads) == len(PAYLOAD)


def test_tampered_multi_chunk_input_is_still_refused(tmp_path: Path, tiny: None) -> None:
    op = _staged(tmp_path, PAYLOAD)
    target = Path(op.input_directory) / "weights/model.bin"
    target.parent.chmod(0o755)
    target.chmod(0o644)
    flipped = bytearray(PAYLOAD)
    flipped[2000] ^= 1  # same size, a later chunk
    target.write_bytes(bytes(flipped))
    target.chmod(0o444)
    target.parent.chmod(0o555)
    with pytest.raises(CpuError):
        DockerCpuAdapter(
            frozenset({IMAGE}), FakeDocker(), input_root=tmp_path, max_input_bytes=1 << 20
        ).create_arguments(op)


def test_input_bound_is_operator_configurable(tmp_path: Path, tiny: None) -> None:
    op = _staged(tmp_path, PAYLOAD)
    with pytest.raises(CpuError, match="valid frozen snapshot manifest"):
        DockerCpuAdapter(
            frozenset({IMAGE}), FakeDocker(), input_root=tmp_path, max_input_bytes=2048
        ).create_arguments(op)
    for bad in (1023, limits.ABSOLUTE_MAX_ARTIFACT_BYTES + 1, True, "x"):
        with pytest.raises(ValueError, match="input byte bound"):
            DockerCpuAdapter(frozenset({IMAGE}), FakeDocker(), max_input_bytes=bad)  # type: ignore[arg-type]
    assert (
        DockerCpuAdapter(frozenset({IMAGE}), FakeDocker()).max_input_bytes
        == limits.artifact_limits().max_artifact_bytes
    )
