from __future__ import annotations

import sqlite3

import pytest

from tessera_control.migrations import MIGRATIONS
from tessera_control.snapshot_releases import create_release_schema
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Store, initialize_state, migrate_state
from tessera_control.workspace import capture_workspace, inventory_digest

AUTHORITY_TABLES = (
    "artifacts",
    "execution_observations",
    "execution_leases",
    "execution_grants",
    "execution_task_epochs",
    "execution_project_limits",
    "snapshot_releases",
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
        for table in AUTHORITY_TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=5")
    return state, actor, snapshot


def test_schema5_requires_migration_and_preserves_snapshot_bytes(tmp_path):
    state, actor, snapshot = old_state(tmp_path)
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 7
    verified = SnapshotStore(state).verify_snapshot(actor, snapshot["id"])
    assert verified["integrity"] == "verified" and verified["digest"] == snapshot["digest"]
    assert migrate_state(state) == 7


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
    assert migrate_state(state) == 7
