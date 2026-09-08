from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_input_delivery import _delivery
from test_runtime import ApiChannel

from tessera_worker.development_storage import (
    DEVELOPMENT_STORAGE_DIRECTORY,
    DEVELOPMENT_STORAGE_LABEL,
    DevelopmentArtifactStorage,
    DevelopmentStorageError,
    SyntheticStoppedWorkspace,
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "synthetic-stopped-workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_bytes(b"synthetic bytes")
    return workspace


def _fixture(workspace: Path, lease: dict[str, object]) -> SyntheticStoppedWorkspace:
    return SyntheticStoppedWorkspace(
        path=workspace,
        lease_id=lease["id"],
        epoch=lease["epoch"],
        output_paths=("result.txt",),
        max_output_bytes=1024,
        termination_observation={
            "id": "observation_synthetic",
            "lease_id": lease["id"],
            "epoch": lease["epoch"],
            "observed_state": "exited",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": "synthetic-provider",
            "termination_confirmed": True,
            "observation_proof": "trusted_worker_assertion",
            "operational_only": True,
        },
    )


def test_development_storage_has_isolated_path_and_quarantine_only_manifest(
    tmp_path: Path,
) -> None:
    state, _, _, _, _, lease = _delivery(tmp_path)
    storage = DevelopmentArtifactStorage(tmp_path / "private-development-root")
    fixture = _fixture(_workspace(tmp_path), lease)
    seal_id = storage.seal(fixture)

    class LostManifest(ApiChannel):
        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path.endswith("/artifact-manifest"):
                raise ConnectionError("synthetic lost manifest response")
            return response

    with pytest.raises(ConnectionError, match="lost manifest"):
        storage.upload(LostManifest(state), seal_id)
    restarted = DevelopmentArtifactStorage(tmp_path / "private-development-root")
    receipt = restarted.upload(ApiChannel(state), seal_id)

    assert storage.label == DEVELOPMENT_STORAGE_LABEL == "development-local-quarantine"
    assert storage.root.name == DEVELOPMENT_STORAGE_DIRECTORY
    assert seal_id == restarted.seal(fixture)
    assert receipt["status"] == "quarantined"
    assert receipt["classification"] == "unclassified"


@pytest.mark.parametrize("field", ["synthetic", "stopped"])
def test_development_storage_rejects_unmarked_workspace(field: str, tmp_path: Path) -> None:
    values = {
        "path": _workspace(tmp_path),
        "lease_id": "lease",
        "epoch": 1,
        "output_paths": ("result.txt",),
        "max_output_bytes": 100,
        "termination_observation": {},
        "synthetic": True,
        "stopped": True,
    }
    values[field] = False
    with pytest.raises(DevelopmentStorageError):
        SyntheticStoppedWorkspace(**values)  # type: ignore[arg-type]


def test_development_storage_requires_fresh_termination_before_reading(tmp_path: Path) -> None:
    _, _, _, _, _, lease = _delivery(tmp_path)
    fixture = _fixture(_workspace(tmp_path), lease)
    stale = dict(fixture.termination_observation)
    stale["termination_confirmed"] = False
    fixture = SyntheticStoppedWorkspace(
        path=fixture.path,
        lease_id=fixture.lease_id,
        epoch=fixture.epoch,
        output_paths=fixture.output_paths,
        max_output_bytes=fixture.max_output_bytes,
        termination_observation=stale,
    )
    with pytest.raises(ValueError, match="termination"):
        storage = DevelopmentArtifactStorage(tmp_path / "root")
        storage.seal(fixture)
    with sqlite3.connect(storage.journal.path) as connection:
        assert connection.execute("SELECT count(*) FROM seals").fetchone()[0] == 0


def test_development_storage_restart_resumes_same_seal_without_runtime_import() -> None:
    tree = ast.parse(Path("src/tessera_worker/development_storage.py").read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "tessera_worker.runtime" not in imported
    assert "tessera_worker.cpu" not in imported
