from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera_worker.development_runtime import (
    DEVELOPMENT_OUTPUT_DIRECTORY,
    DevelopmentOutputBackend,
    DevelopmentOutputProfile,
    DevelopmentRuntimeError,
)

OPERATION_ID = "a" * 32


def _backend(tmp_path: Path, *, bytes: int = 100, inodes: int = 10) -> DevelopmentOutputBackend:
    root = tmp_path / "private-development-root"
    root.mkdir(mode=0o700)
    return DevelopmentOutputBackend(DevelopmentOutputProfile(root, bytes, inodes))


def _observation() -> dict[str, object]:
    return {
        "id": "development-observation",
        "lease_id": "development-lease",
        "epoch": 1,
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "development-container",
        "termination_confirmed": True,
        "observation_proof": "trusted_worker_assertion",
        "operational_only": True,
    }


def test_development_workspace_is_private_operation_specific_and_reserved(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    workspace = backend.reserve(
        operation_id=OPERATION_ID,
        lease_id="development-lease",
        epoch=1,
        output_paths=("nested/result.txt",),
        max_output_bytes=50,
    )

    assert workspace == backend.root / "workspaces" / OPERATION_ID
    assert workspace.is_dir()
    assert workspace.stat().st_mode & 0o777 == 0o700
    assert backend.root.name == DEVELOPMENT_OUTPUT_DIRECTORY
    assert (
        backend.reserve(
            operation_id=OPERATION_ID,
            lease_id="development-lease",
            epoch=1,
            output_paths=("nested/result.txt",),
            max_output_bytes=50,
        )
        == workspace
    )
    with pytest.raises(DevelopmentRuntimeError, match="exhausted"):
        backend.reserve(
            operation_id="b" * 32,
            lease_id="another-lease",
            epoch=1,
            output_paths=("result.txt",),
            max_output_bytes=51,
        )


@pytest.mark.parametrize("path", ("../result.txt", "/result.txt", "a//b", "a\\b"))
def test_development_workspace_rejects_unsafe_declarations(tmp_path: Path, path: str) -> None:
    with pytest.raises(DevelopmentRuntimeError, match="reservation is invalid"):
        _backend(tmp_path).reserve(
            operation_id=OPERATION_ID,
            lease_id="development-lease",
            epoch=1,
            output_paths=(path,),
            max_output_bytes=1,
        )


def test_development_workspace_rejects_undeclared_nodes_and_unconfirmed_cleanup(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    workspace = backend.reserve(
        operation_id=OPERATION_ID,
        lease_id="development-lease",
        epoch=1,
        output_paths=("result.txt",),
        max_output_bytes=50,
    )
    (workspace / "result.txt").write_bytes(b"declared")
    (workspace / "flood.txt").write_bytes(b"undeclared")
    with pytest.raises(DevelopmentRuntimeError, match="undeclared"):
        backend.seal(OPERATION_ID, _observation())
    with pytest.raises(DevelopmentRuntimeError, match="Refusing cleanup"):
        backend.cleanup(OPERATION_ID)


def test_development_workspace_reclaims_only_after_quarantine_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path, bytes=50, inodes=3)
    workspace = backend.reserve(
        operation_id=OPERATION_ID,
        lease_id="development-lease",
        epoch=1,
        output_paths=("result.txt",),
        max_output_bytes=50,
    )
    (workspace / "result.txt").write_bytes(b"declared")
    seal_id = backend.seal(OPERATION_ID, _observation())
    monkeypatch.setattr(
        backend.seals,
        "resume",
        lambda channel, seal: {
            "id": seal,
            "status": "quarantined",
            "project_placements_committed": True,
        },
    )
    backend.upload(OPERATION_ID, object(), seal_id)
    backend.cleanup(OPERATION_ID)

    assert not workspace.exists()
    assert backend.reserve(
        operation_id="b" * 32,
        lease_id="another-lease",
        epoch=1,
        output_paths=("result.txt",),
        max_output_bytes=50,
    ).is_dir()


def test_development_workspace_requires_fresh_confirmed_termination_before_reading(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    workspace = backend.reserve(
        operation_id=OPERATION_ID,
        lease_id="development-lease",
        epoch=1,
        output_paths=("result.txt",),
        max_output_bytes=50,
    )
    (workspace / "result.txt").write_bytes(b"declared")
    observation = _observation()
    observation["termination_confirmed"] = False
    with pytest.raises(DevelopmentRuntimeError, match="termination"):
        backend.seal(OPERATION_ID, observation)


def test_normal_runtime_has_no_development_output_wiring() -> None:
    tree = ast.parse(Path("src/tessera_worker/runtime.py").read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "tessera_worker.development_runtime" not in imported
    assert "DevelopmentOutputMount" not in Path("src/tessera_worker/runtime.py").read_text()


def test_uploaded_seal_id_refuses_a_seal_belonging_to_another_operation(tmp_path: Path) -> None:
    import sqlite3

    from tessera_worker.sealed_outputs import _operation_id

    operation_id = _operation_id("development-lease")  # how production derives it
    backend = _backend(tmp_path, bytes=50, inodes=3)
    workspace = backend.reserve(
        operation_id=operation_id,
        lease_id="development-lease",
        epoch=1,
        output_paths=("result.txt",),
        max_output_bytes=50,
    )
    (workspace / "result.txt").write_bytes(b"declared")
    seal_id = backend.seal(operation_id, _observation())
    backend._transition(operation_id, {"sealed"}, "uploaded")
    assert backend.uploaded_seal_id(operation_id) == seal_id

    # The lease's seal is re-attributed to a different operation: it must not replay.
    with sqlite3.connect(backend.seals.path) as connection:
        connection.execute("UPDATE seals SET operation_id=? WHERE id=?", ("b" * 32, seal_id))
    with pytest.raises(DevelopmentRuntimeError, match="no seal"):
        backend.uploaded_seal_id(operation_id)


def test_reused_runtime_never_carries_a_sealed_digest_into_the_next_operations_receipt() -> None:
    from types import SimpleNamespace

    from tessera_worker.development_runtime import ADAPTER_VERSION, DevelopmentCpuRuntime

    first, second = "a" * 32, "b" * 32

    class Outputs:
        def uploaded_seal_id(self, operation_id):
            return None

        def seal(self, operation_id, observation):
            if operation_id == second:
                raise DevelopmentRuntimeError("second seal failed")
            return "seal-1"

        def manifest_digest(self, seal_id):
            return "d1" * 32

        def upload(self, operation_id, channel, seal_id):
            return {"status": "quarantined"}

    posts: list[str] = []
    runtime = object.__new__(DevelopmentCpuRuntime)
    runtime.development_outputs = Outputs()
    runtime.channel = SimpleNamespace(post=lambda path, body, key: posts.append(path))
    runtime.report = lambda result: {}
    runtime._sealed_manifest_digest = None
    runtime._sealed_operation_id = None
    runtime.provider_dispatch = None
    runtime.lease = SimpleNamespace(id="lease", epoch=1)
    runtime.descriptor = {"control": {"package": {"adapter_version": ADAPTER_VERSION}}}
    runtime.adapter = SimpleNamespace(run=lambda arguments: pytest.fail("must not inspect"))
    observation = SimpleNamespace(
        state="exited", termination_confirmed=True, container_id="c" * 64, exit_code=0
    )
    result = SimpleNamespace(phase="terminal", observation=observation)

    runtime.operation = SimpleNamespace(operation_id=first)
    runtime.collect(result)
    assert runtime._sealed_manifest_digest == "d1" * 32

    # Same runtime, next operation: its seal fails, so the first digest is stale.
    runtime.operation = SimpleNamespace(operation_id=second)
    with pytest.raises(DevelopmentRuntimeError):
        runtime.collect(result)
    with pytest.raises(DevelopmentRuntimeError, match="sealed output"):
        runtime.post_run_receipt(result)
    assert posts == []
