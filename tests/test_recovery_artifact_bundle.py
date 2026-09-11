"""Filesystem project-object backup bundles remain held and fail closed."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_artifacts import _payload
from test_input_delivery import _delivery

import tessera_control.recovery as recovery
from tessera_control.artifacts import ArtifactStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.store import StoreError, canonical_digest


def _placed_object(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
    state, _client, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"bundle object bytes")
    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "bundle-upload", canonical_digest(payload), payload
    )[1]
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "bundle-placement", canonical_digest(request), request
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT artifact_id,tenant_namespace,project_namespace,object_key,sha256,size "
            "FROM project_artifact_placements"
        ).fetchone()
    keys = ("artifact_id", "tenant", "project", "key", "sha256", "size")
    return state, artifact, dict(zip(keys, row, strict=True))


def _object_path(root: Path, placement: dict[str, object]) -> Path:
    return root / str(placement["tenant"]) / str(placement["project"]) / str(placement["key"])


def test_backup_restore_includes_local_project_object_and_holds_its_custody(tmp_path: Path) -> None:
    state, artifact, placement = _placed_object(tmp_path)
    backup = tmp_path / "backup"
    manifest = recovery.backup_state(state, backup)
    assert manifest["format_version"] == 2
    assert manifest["objects"] == [
        {
            "artifact_id": artifact["id"],
            "tenant_namespace": placement["tenant"],
            "project_namespace": placement["project"],
            "object_key": placement["key"],
            "sha256": artifact["sha256"],
            "size": artifact["size"],
            "availability": "present",
        }
    ]
    assert _object_path(backup / "objects", placement).read_bytes() == b"bundle object bytes"

    restored = tmp_path / "restored"
    recovery.restore_state(backup, restored)
    assert (
        _object_path(restored / "project-artifacts", placement).read_bytes()
        == b"bundle object bytes"
    )
    with sqlite3.connect(restored / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT retention_class FROM project_artifact_placements"
        ).fetchone() == ("held",)
        assert connection.execute(
            "SELECT retention_class,state FROM project_artifact_retention"
        ).fetchone() == ("held", "active")


@pytest.mark.parametrize("fault", ("corrupt", "symlink", "hardlink", "fifo"))
def test_restore_rejects_unsafe_or_corrupt_bundle_object(tmp_path: Path, fault: str) -> None:
    state, _artifact, placement = _placed_object(tmp_path)
    backup = tmp_path / "backup"
    recovery.backup_state(state, backup)
    object_path = _object_path(backup / "objects", placement)
    if fault == "corrupt":
        object_path.write_bytes(b"corrupt")
    else:
        held = tmp_path / "held-object"
        object_path.rename(held)
        if fault == "symlink":
            object_path.symlink_to(held)
        elif fault == "hardlink":
            os.link(held, object_path)
        else:
            os.mkfifo(object_path)
    restored = tmp_path / "restored"
    with pytest.raises((StoreError, OSError)):
        recovery.restore_state(backup, restored)
    assert not (restored / recovery.RESTORE_MARKER).exists()


def test_backup_fails_closed_for_missing_source_object_and_bundle_copy_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _artifact, placement = _placed_object(tmp_path)
    _object_path(state / "project-artifacts", placement).unlink()
    with pytest.raises(StoreError, match="bounded regular|size|checksum|unsafe"):
        recovery.backup_state(state, tmp_path / "missing")

    fault_root = tmp_path / "fault"
    fault_root.mkdir()
    state, _artifact, _placement = _placed_object(fault_root)
    original = recovery._copy_regular

    def fail(source: Path, destination: Path, size: int, start: float) -> str:
        if destination.parts[-4] == "objects":
            raise OSError("injected object-copy failure")
        return original(source, destination, size, start)

    monkeypatch.setattr(recovery, "_copy_regular", fail)
    output = tmp_path / "copy-fault"
    with pytest.raises(StoreError, match="missing or unsafe"):
        recovery.backup_state(state, output)
    assert not (output / recovery.BACKUP_MANIFEST).exists()


def test_deleted_object_is_declared_unavailable_not_fabricated(tmp_path: Path) -> None:
    state, _artifact, placement = _placed_object(tmp_path)
    _object_path(state / "project-artifacts", placement).unlink()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE project_artifact_retention SET state='deleted'")
    backup = tmp_path / "backup"
    recovery.backup_state(state, backup)
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["objects"][0]["availability"] == "deleted"
    restored = tmp_path / "restored"
    recovery.restore_state(backup, restored)
    assert not (restored / "project-artifacts").exists()


def test_backup_rejects_project_object_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, artifact, _placement = _placed_object(tmp_path)
    monkeypatch.setattr(recovery, "MAX_PROJECT_OBJECT_BYTES", int(artifact["size"]) - 1)
    with pytest.raises(StoreError, match="placement metadata"):
        recovery.backup_state(state, tmp_path / "bounded")
