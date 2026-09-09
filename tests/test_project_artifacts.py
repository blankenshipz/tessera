from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from test_artifacts import _payload
from test_input_delivery import _delivery

from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.migrations import MIGRATIONS
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.project_artifacts import (
    FakeProjectArtifactBackend,
    FilesystemProjectArtifactBackend,
    ProjectArtifactStorageError,
)
from tessera_control.recovery import backup_state
from tessera_control.store import Conflict, StoreError, canonical_digest, migrate_state


def _backend(tmp_path: Path) -> FilesystemProjectArtifactBackend:
    root = tmp_path / "project-store"
    return FilesystemProjectArtifactBackend(root)


def test_filesystem_backend_replays_exact_bytes_and_rejects_conflicting_identity(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    content = b"durable bytes"
    digest = hashlib.sha256(content).hexdigest()
    stage = backend.stage("org-one", "project-one", "artifact-one", digest, content)
    first = backend.commit(stage)
    # A lost commit response is recovered by a fresh stage/commit using identity.
    replay = backend.commit(
        backend.stage("org-one", "project-one", "artifact-one", digest, content)
    )
    assert replay == first
    with pytest.raises(ProjectArtifactStorageError, match="identity conflicts"):
        backend.stage(
            "org-one",
            "project-one",
            "artifact-one",
            hashlib.sha256(b"changed").hexdigest(),
            b"changed",
        )


def test_backend_detects_truncation_links_and_root_replacement(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    content = b"bytes"
    digest = hashlib.sha256(content).hexdigest()
    placement = backend.commit(backend.stage("tenant", "project", "artifact", digest, content))
    object_path = (
        backend.root
        / placement.tenant_namespace
        / placement.project_namespace
        / placement.object_key
    )
    object_path.write_bytes(b"x")
    with pytest.raises(ProjectArtifactStorageError, match="corrupt"):
        backend.reconcile("tenant", "project", "artifact", digest, len(content))
    object_path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    object_path.symlink_to(outside)
    with pytest.raises(ProjectArtifactStorageError, match="corrupt"):
        backend.reconcile("tenant", "project", "artifact", digest, len(content))
    object_path.unlink()
    os.link(outside, object_path)
    with pytest.raises(ProjectArtifactStorageError, match="corrupt"):
        backend.reconcile("tenant", "project", "artifact", digest, len(content))
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    backend.root.rename(tmp_path / "old-root")
    replacement.rename(backend.root)
    with pytest.raises(ProjectArtifactStorageError, match="replaced"):
        backend.reconcile("tenant", "project", "artifact", digest, len(content))


def test_namespaces_isolate_tenants_and_projects(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    content = b"same bytes"
    digest = hashlib.sha256(content).hexdigest()
    first = backend.commit(backend.stage("tenant-a", "project-a", "same", digest, content))
    second = backend.commit(backend.stage("tenant-b", "project-b", "same", digest, content))
    assert (first.tenant_namespace, first.project_namespace) != (
        second.tenant_namespace,
        second.project_namespace,
    )
    assert backend.reconcile("tenant-a", "project-b", "same", digest, len(content)) is None


def test_fake_fault_reconciles_and_manifest_waits_for_placement(tmp_path: Path) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"first")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    fake = FakeProjectArtifactBackend()
    storage = ProjectArtifactStore(state, fake)
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    fake.fail_commit = True
    with pytest.raises(Conflict):
        storage.commit_project_artifacts(
            worker, lease["id"], "place-fail", canonical_digest(request), request
        )
    # Retry uses the same immutable artifact identity and commits it.
    assert (
        storage.commit_project_artifacts(
            worker, lease["id"], "place", canonical_digest(request), request
        )[0]
        == 201
    )
    manifest = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {
                "id": artifact["id"],
                "relative_path": "result.txt",
                "sha256": artifact["sha256"],
                "size": artifact["size"],
            }
        ],
    }
    assert (
        ArtifactManifestStore(state).create_manifest(
            worker, lease["id"], "manifest", canonical_digest(manifest), manifest
        )[0]
        == 201
    )
    with pytest.raises(StoreError, match="cannot omit"):
        backup_state(state, tmp_path / "backup")


def test_v22_migration_is_empty_for_legacy_blobs_and_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"legacy")
    from tessera_control.artifacts import ArtifactStore

    ArtifactStore(state).upload_artifact(
        worker, lease["id"], "legacy", canonical_digest(payload), payload
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        for table in (
            "project_artifact_storage_operations",
            "project_artifact_placements",
            "project_storage_bindings",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=21")

    def fail(connection):
        from tessera_control.project_artifact_store import create_project_artifact_storage_schema

        create_project_artifact_storage_schema(connection)
        raise RuntimeError("v22 fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 21, fail)
        with pytest.raises(RuntimeError, match="v22 fault"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 21
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='project_storage_bindings'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == 22
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM project_artifact_placements").fetchone()[0]
            == 0
        )
