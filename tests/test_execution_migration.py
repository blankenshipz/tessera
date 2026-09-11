from __future__ import annotations

import sqlite3

import pytest
from test_input_delivery import _delivery

from tessera_control.migrations import MIGRATIONS
from tessera_control.snapshot_releases import create_release_schema
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Store, initialize_state, migrate_state
from tessera_control.workspace import capture_workspace, inventory_digest

AUTHORITY_TABLES = (
    "artifacts",
    "artifact_manifests",
    "artifact_uploads",
    "execution_observations",
    "execution_leases",
    "execution_grants",
    "execution_task_epochs",
    "execution_project_limits",
    "execution_result_checkpoints",
    "external_operations",
    "snapshot_releases",
    "execution_grant_skills",
    "skill_releases",
    "skill_snapshots",
    "research_conclusion_amendments",
    "research_conclusion_lineage",
    "research_conclusion_evidence",
    "research_conclusions",
    "research_attempt_measurements",
    "research_attempt_artifacts",
    "research_experiment_admitted_attempt_states",
    "research_experiment_admitted_attempts",
    "research_experiment_attempts",
    "research_experiment_variants",
    "research_experiment_designs",
    "research_experiments",
)


def old_state(tmp_path):
    state, source = tmp_path / "state", tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("Retain exact bytes")
    initialize_state(state)
    store = SnapshotStore(state)
    token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    actor = store.authenticate(token)
    _, project = store.create_project(actor, "project", "project-digest", {"name": "Preserved"})
    capture = capture_workspace(source)
    _, snapshot = store.adopt_snapshot(
        actor, project["id"], "snapshot", inventory_digest(capture.inventory), capture
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE artifact_dispositions")
        for table in AUTHORITY_TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=5")
    return state, actor, snapshot


def test_schema5_requires_migration_and_preserves_snapshot_bytes(tmp_path):
    state, actor, snapshot = old_state(tmp_path)
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 33
    verified = SnapshotStore(state).verify_snapshot(actor, snapshot["id"])
    assert verified["integrity"] == "verified" and verified["digest"] == snapshot["digest"]
    assert migrate_state(state) == 33


def test_v13_worker_registry_migration_preserves_existing_records(tmp_path):
    state, actor, snapshot = old_state(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        from tessera_control.artifact_dispositions import create_artifact_disposition_schema
        from tessera_control.artifact_manifests import create_artifact_manifest_schema
        from tessera_control.artifacts import create_artifact_schema, create_artifact_upload_schema
        from tessera_control.execution import create_execution_schema

        create_execution_schema(connection)
        create_artifact_schema(connection)
        create_artifact_upload_schema(connection)
        create_artifact_manifest_schema(connection)
        create_artifact_disposition_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=13")
    assert migrate_state(state) == 33
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM worker_registry").fetchone()[0] == 0
    verified = SnapshotStore(state).verify_snapshot(actor, snapshot["id"])
    assert verified["integrity"] == "verified" and verified["digest"] == snapshot["digest"]


def test_execution_schema_rollback_preserves_old_state(tmp_path, monkeypatch):
    state, _, snapshot = old_state(tmp_path)

    def fail(connection):
        create_release_schema(connection)
        raise RuntimeError("injected admission migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 5, fail)
        with pytest.raises(RuntimeError, match="injected"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not set(AUTHORITY_TABLES) & tables
        assert (
            connection.execute(
                "SELECT content FROM snapshot_files WHERE snapshot_id=?", (snapshot["id"],)
            ).fetchone()[0]
            == b"Retain exact bytes"
        )
    assert migrate_state(state) == 33


def test_schema7_upload_migration_is_transactional_and_repeatable(tmp_path, monkeypatch):
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE artifact_dispositions")
        connection.execute("DROP TABLE artifact_manifests")
        connection.execute("DROP TABLE artifact_uploads")
        connection.execute("DROP TABLE execution_result_checkpoints")
        connection.execute("DROP TABLE external_operations")
        connection.execute("DROP TABLE execution_grant_skills")
        connection.execute("DROP TABLE skill_releases")
        connection.execute("DROP TABLE skill_snapshots")
        for table in AUTHORITY_TABLES[14:]:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=7")

    def fail(connection):
        from tessera_control.artifacts import create_artifact_upload_schema

        create_artifact_upload_schema(connection)
        raise RuntimeError("injected upload migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 7, fail)
        with pytest.raises(RuntimeError, match="injected upload"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_uploads'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == 33
    assert migrate_state(state) == 33


def test_v17_operation_migration_never_backfills_legacy_leases_or_observations(tmp_path):
    state, _, _, _, _, lease = _delivery(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE external_operations")
        for table in AUTHORITY_TABLES[14:]:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=17")
    assert migrate_state(state) == 33
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_leases WHERE id=?", (lease["id"],)
            ).fetchone()[0]
            == 1
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(execution_observations)")
        }
    assert "operation_id" in columns
