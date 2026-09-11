from __future__ import annotations

# ruff: noqa: E501, I001

import hashlib
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from test_artifacts import _payload
from test_input_delivery import _delivery

from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.migrations import MIGRATIONS
from tessera_control.project_artifact_retention import ProjectArtifactRetentionStore
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


def test_filesystem_backend_recovers_a_stale_publication_hard_link(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    content = b"crash between link and unlink"
    digest = hashlib.sha256(content).hexdigest()
    stage = backend.stage("tenant", "project", "artifact", digest, content)
    tenant, project, object_key = backend._names("tenant", "project", "artifact")
    namespace = backend._namespace(tenant, project)
    os.link(namespace / stage.token, namespace / object_key)
    assert (namespace / object_key).stat().st_nlink == 2
    placement = backend.reconcile("tenant", "project", "artifact", digest, len(content))
    assert placement is not None
    assert (namespace / object_key).stat().st_nlink == 1
    assert not (namespace / stage.token).exists()


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
        ArtifactManifestStore(state, fake).create_manifest(
            worker, lease["id"], "manifest", canonical_digest(manifest), manifest
        )[0]
        == 201
    )
    with pytest.raises(StoreError, match="placement metadata"):
        backup_state(state, tmp_path / "backup")


def test_placement_recovers_lost_backend_response_and_records_reconcile_state(
    tmp_path: Path,
) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"recover")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]

    class LostResponseBackend(FakeProjectArtifactBackend):
        lose_response = True

        def commit(self, stage):
            placement = super().commit(stage)
            if self.lose_response:
                self.lose_response = False
                raise ProjectArtifactStorageError("lost response after publication")
            return placement

    backend = LostResponseBackend()
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    store = ProjectArtifactStore(state, backend)
    with pytest.raises(Conflict, match="could not commit"):
        store.commit_project_artifacts(
            worker, lease["id"], "lost", canonical_digest(request), request
        )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT state FROM project_artifact_storage_operations").fetchone()[
                0
            ]
            == "reconciling"
        )
        assert (
            connection.execute("SELECT count(*) FROM project_artifact_placements").fetchone()[0]
            == 0
        )
    result = store.commit_project_artifacts(
        worker, lease["id"], "lost", canonical_digest(request), request
    )
    assert result[0] == 201


def test_placement_concurrency_and_explicit_zero_outputs(tmp_path: Path) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"concurrent")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    backend = FakeProjectArtifactBackend()
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    results: list[int] = []

    def place(key: str) -> None:
        results.append(
            ProjectArtifactStore(state, backend).commit_project_artifacts(
                worker, lease["id"], key, canonical_digest(request), request
            )[0]
        )

    threads = [threading.Thread(target=place, args=(f"race-{number}",)) for number in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [200, 201]
    zero = request | {"artifacts": []}
    status, response = ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "zero", canonical_digest(zero), zero
    )
    assert status == 201 and response["placements"][0]["artifact_id"] is None
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT state,artifact_id,size FROM project_artifact_storage_operations "
            "WHERE artifact_id IS NULL"
        ).fetchone() == ("committed", None, 0)


def test_manifest_rechecks_filesystem_placement_integrity(tmp_path: Path) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"integrity")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(request), request
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT tenant_namespace,project_namespace,object_key FROM project_artifact_placements"
        ).fetchone()
    (state / "project-artifacts" / row[0] / row[1] / row[2]).write_bytes(b"corrupt")
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
    with pytest.raises(Conflict, match="storage integrity"):
        ArtifactManifestStore(state).create_manifest(
            worker, lease["id"], "manifest", canonical_digest(manifest), manifest
        )


def test_v23_migration_preserves_v22_staged_at_operations_and_placements(tmp_path: Path) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"v22 placement")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(request), request
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        old = connection.execute(
            """SELECT artifact_id,intent_at,committed_at,reconciled_at
            FROM project_artifact_storage_operations"""
        ).fetchone()
        connection.execute(
            "ALTER TABLE project_artifact_storage_operations RENAME TO v23_operations"
        )
        connection.execute(
            """CREATE TABLE project_artifact_storage_operations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            backend_kind TEXT NOT NULL,
            operation_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK (state IN ('staged', 'committed')),
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size >= 0),
            retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
            staged_at TEXT NOT NULL,
            committed_at TEXT,
            reconciled_at TEXT
            )"""
        )
        connection.execute(
            """INSERT INTO project_artifact_storage_operations
            (id,artifact_id,project_id,backend_kind,operation_key,state,sha256,size,
             retention_class,staged_at,committed_at,reconciled_at)
            SELECT id,artifact_id,project_id,backend_kind,operation_key,'committed',sha256,size,
            retention_class,intent_at,committed_at,reconciled_at FROM v23_operations"""
        )
        connection.execute("DROP TABLE v23_operations")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=22")

    assert migrate_state(state) == 33
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        migrated = connection.execute(
            """SELECT artifact_id,state,intent_at,reconcile_started_at,committed_at,reconciled_at
            FROM project_artifact_storage_operations"""
        ).fetchone()
        assert migrated == (
            old[0],
            "committed",
            old[1],
            old[1],
            old[2],
            old[3],
        )
        assert (
            connection.execute("SELECT count(*) FROM project_artifact_placements").fetchone()[0]
            == 1
        )


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
            "project_artifact_retention",
            "project_artifact_storage_operations",
            "project_artifact_placements",
            "project_storage_bindings",
        ):
            connection.execute(f"DROP TABLE {table}")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
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
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("BEGIN")
        MIGRATIONS[21](connection)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='project_artifact_retention'"
            ).fetchone()
            is None
        )
        connection.execute("ROLLBACK")
    assert migrate_state(state) == 33
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM project_artifact_placements").fetchone()[0]
            == 0
        )


# fmt: off
def test_retention_tombstone_refuses_held_and_referenced_objects(tmp_path: Path) -> None:
    state, _, owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"retention")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    backend = FakeProjectArtifactBackend()
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(request), request
    )
    retention = ProjectArtifactRetentionStore(state, backend)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE project_artifact_retention SET retention_class='held'")
    with pytest.raises(Conflict, match="Held"):
        retention.tombstone(owner, _grant["project_id"], artifact["id"], "held", "held")
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE project_artifact_retention SET retention_class='project_default'")
        connection.execute(
            "INSERT INTO artifact_manifests (id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,operation_id,plan_digest,artifacts_json,total_bytes,manifest_digest,created_at,status,classification,late) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("manifest-reference", _grant["project_id"], _grant["task_id"], _grant["id"], lease["id"], worker["id"], lease["epoch"], payload["operation_id"], "0" * 64, '[{"id":"' + artifact["id"] + '"}]', artifact["size"], "1" * 64, "now", "quarantined", "unclassified", 0),
        )
    with pytest.raises(Conflict, match="Referenced"):
        retention.tombstone(owner, _grant["project_id"], artifact["id"], "ref", "ref")


def test_retention_delete_intent_is_retry_safe_and_missing_is_degraded(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"deletion")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    backend = FakeProjectArtifactBackend()
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(request), request
    )
    retention = ProjectArtifactRetentionStore(state, backend)
    assert retention.tombstone(owner, grant["project_id"], artifact["id"], "tomb", "tomb")[1][
        "state"
    ] == "tombstoned"
    assert retention.reconcile_delete(owner, grant["project_id"], artifact["id"], "delete", "delete")[1][
        "state"
    ] == "deleted"
    assert retention.reconcile_delete(owner, grant["project_id"], artifact["id"], "retry", "retry")[1][
        "state"
    ] == "deleted"
    assert retention.inspect(owner, grant["project_id"], artifact["id"])["state"] == "deleted"


def test_delete_intent_rejects_manifest_admission_and_republication(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"delete-intent")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    manifest = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{
            "id": artifact["id"], "relative_path": "result.txt",
            "sha256": artifact["sha256"], "size": artifact["size"],
        }],
    }

    class AdmissionDuringDeleteBackend(FakeProjectArtifactBackend):
        def delete(self, tenant_id, project_id, artifact_id, object_placement):
            with pytest.raises(Conflict, match="reserved for deletion"):
                ArtifactManifestStore(state, self).create_manifest(
                    worker, lease["id"], "manifest", canonical_digest(manifest), manifest
                )
            with pytest.raises(Conflict, match="reserved for deletion"):
                ProjectArtifactStore(state, self).commit_project_artifacts(
                    worker, lease["id"], "republication", canonical_digest(placement), placement
                )
            return super().delete(tenant_id, project_id, artifact_id, object_placement)

    backend = AdmissionDuringDeleteBackend()
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    retention = ProjectArtifactRetentionStore(state, backend)
    retention.tombstone(owner, grant["project_id"], artifact["id"], "tomb", "tomb")
    assert retention.reconcile_delete(
        owner, grant["project_id"], artifact["id"], "delete", "delete"
    )[1]["state"] == "deleted"

    # The original placement command must not replay its cached intent into the
    # deterministic object key after deletion either.
    with pytest.raises(Conflict, match="reserved for deletion"):
        ProjectArtifactStore(state, backend).commit_project_artifacts(
            worker, lease["id"], "place", canonical_digest(placement), placement
        )


def test_same_key_delete_replay_is_terminal_and_concurrent_failure_cannot_regress(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"delete-replay")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    class RacingDeleteBackend(FakeProjectArtifactBackend):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.calls = 0

        def delete(self, tenant_id, project_id, artifact_id, object_placement):
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                assert self.release_first.wait(5)
            return super().delete(tenant_id, project_id, artifact_id, object_placement)

    backend = RacingDeleteBackend()
    placement = {
        "expected_epoch": lease["epoch"], "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    retention = ProjectArtifactRetentionStore(state, backend)
    retention.tombstone(owner, grant["project_id"], artifact["id"], "tomb", "tomb")
    results: list[tuple[int, dict]] = []
    errors: list[Exception] = []

    def delete(key: str) -> None:
        try:
            results.append(retention.reconcile_delete(owner, grant["project_id"], artifact["id"], key, key))
        except Exception as error:  # pragma: no cover - assertion reports this deterministically
            errors.append(error)

    first = threading.Thread(target=delete, args=("delete",))
    first.start()
    assert backend.first_started.wait(5)
    second = threading.Thread(target=delete, args=("concurrent",))
    second.start()
    second.join(5)
    backend.release_first.set()
    first.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert all(response[1]["state"] == "deleted" for response in results)
    assert retention.reconcile_delete(
        owner, grant["project_id"], artifact["id"], "delete", "delete"
    ) == (200, {"state": "deleted"})
    assert retention.inspect(owner, grant["project_id"], artifact["id"])["state"] == "deleted"


def test_reference_before_deletion_reservation_prevents_delete(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"reference-first")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    backend = FakeProjectArtifactBackend()
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    manifest = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{
            "id": artifact["id"], "relative_path": "result.txt",
            "sha256": artifact["sha256"], "size": artifact["size"],
        }],
    }
    ArtifactManifestStore(state, backend).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(manifest), manifest
    )
    with pytest.raises(Conflict, match="Referenced"):
        ProjectArtifactRetentionStore(state, backend).tombstone(
            owner, grant["project_id"], artifact["id"], "tomb", "tomb"
        )


def test_delete_retry_rechecks_intent_reference_state(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"retry-reference")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    class InterruptedDeleteBackend(FakeProjectArtifactBackend):
        def delete(self, tenant_id, project_id, artifact_id, object_placement):
            raise RuntimeError("interrupted after delete intent")

    backend = InterruptedDeleteBackend()
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    retention = ProjectArtifactRetentionStore(state, backend)
    retention.tombstone(owner, grant["project_id"], artifact["id"], "tomb", "tomb")
    with pytest.raises(RuntimeError, match="interrupted"):
        retention.reconcile_delete(owner, grant["project_id"], artifact["id"], "retry", "retry")
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT INTO artifact_manifests (id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,operation_id,plan_digest,artifacts_json,total_bytes,manifest_digest,created_at,status,classification,late) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("retry-reference", grant["project_id"], grant["task_id"], grant["id"], lease["id"], worker["id"], lease["epoch"], payload["operation_id"], "0" * 64, '[{"id":"' + artifact["id"] + '"}]', artifact["size"], "1" * 64, "now", "quarantined", "unclassified", 0),
        )
    with pytest.raises(Conflict, match="Referenced"):
        retention.reconcile_delete(owner, grant["project_id"], artifact["id"], "retry", "retry")
    assert backend.objects


def test_retention_inspection_does_not_overwrite_concurrent_deleted_state(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"inspection-race")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]

    class ConcurrentDeleteBackend(FakeProjectArtifactBackend):
        def inspect(self, tenant_id, project_id, artifact_id, sha256, size):
            with sqlite3.connect(state / "tessera.sqlite3") as connection:
                connection.execute(
                    "UPDATE project_artifact_retention SET state='deleted' WHERE artifact_id=?",
                    (artifact_id,),
                )
            return None

    backend = ConcurrentDeleteBackend()
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state, backend).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(request), request
    )
    result = ProjectArtifactRetentionStore(state, backend).inspect(
        owner, grant["project_id"], artifact["id"]
    )
    assert result["state"] == "deleted"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT state,incident_reason FROM project_artifact_retention WHERE artifact_id=?",
            (artifact["id"],),
        ).fetchone() == ("deleted", None)


def test_v24_retention_migration_preserves_placement_and_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    state, _, _owner, worker, _grant, lease = _delivery(tmp_path)
    payload = _payload(lease, b"v24")
    from tessera_control.artifacts import ArtifactStore

    artifact = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "upload", canonical_digest(payload), payload
    )[1]
    request = {
        "expected_epoch": lease["epoch"],
        "operation_id": payload["operation_id"],
        "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(request), request
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE project_artifact_retention")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=23")
    assert migrate_state(state) == 33
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT retention_class,state FROM project_artifact_retention"
        ).fetchone() == ("project_default", "active")

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE project_artifact_retention")
        connection.execute("UPDATE schema_meta SET schema_version=23")

    def fail(connection):
        from tessera_control.project_artifact_retention import create_project_artifact_retention_schema
        create_project_artifact_retention_schema(connection)
        raise RuntimeError("v24 fault")
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 23, fail)
        with pytest.raises(RuntimeError, match="v24 fault"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 23
        assert connection.execute("SELECT count(*) FROM project_artifact_placements").fetchone()[0] == 1
# fmt: on
