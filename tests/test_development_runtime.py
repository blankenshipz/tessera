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
