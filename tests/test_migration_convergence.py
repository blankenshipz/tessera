"""Current-schema convergence for fresh and historical local state."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from migration_fixture_helpers import (
    CHUNK_V60_TABLES,
    RUNTIME_V55_TABLES,
    assert_empty_runtime_v55,
    drop_empty_chunk_schema,
    strip_empty_runtime_v55,
)

from tessera_control.api import create_app
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import initialize_state, migrate_state


def _owner_headers(state: Path) -> dict[str, str]:
    name, value = state.joinpath("owner.header").read_text().strip().split(": ", 1)
    return {name: value}


def _schema_version(state: Path) -> int:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        return connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0]


def test_experiment_budget_v56_empty_migration_matches_fresh_and_stales_backup(tmp_path):
    fresh, migrated = tmp_path / "budget-fresh", tmp_path / "budget-migrated"
    initialize_state(fresh)
    initialize_state(migrated)
    names = (
        "experiment_budget_entries",
        "experiment_budget_reservations",
        "experiment_budgets",
        *CHUNK_V60_TABLES,
    )
    with sqlite3.connect(migrated / "tessera.sqlite3") as db:
        for name in names:
            db.execute(f"DROP TABLE {name}")
        db.execute("UPDATE schema_meta SET schema_version=56")
        db.execute(
            "UPDATE operator_backup_health SET last_success_at='synthetic',last_backup_id='old'"
        )
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(fresh / "tessera.sqlite3") as expected:
        with sqlite3.connect(migrated / "tessera.sqlite3") as actual:
            for name in names:
                observed = actual.execute(
                    "SELECT sql FROM sqlite_schema WHERE name=?", (name,)
                ).fetchone()
                assert observed == (
                    expected.execute(
                        "SELECT sql FROM sqlite_schema WHERE name=?", (name,)
                    ).fetchone()
                )
                assert actual.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
            assert actual.execute(
                "SELECT last_success_at,last_backup_id FROM operator_backup_health"
            ).fetchone() == (None, None)


def test_fresh_and_v46_migrated_state_converge_and_held_restore_stays_read_only(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    assert _schema_version(fresh) == LATEST_SCHEMA_VERSION
    assert migrate_state(fresh) == LATEST_SCHEMA_VERSION

    migrated = tmp_path / "migrated"
    initialize_state(migrated)
    with sqlite3.connect(migrated / "tessera.sqlite3") as connection:
        strip_empty_runtime_v55(connection)
        drop_empty_chunk_schema(connection)
        connection.execute("DROP TABLE operator_audit_events")
        connection.execute("DROP TABLE operator_backup_health")
        connection.execute("ALTER TABLE events DROP COLUMN correlation_id")
        connection.execute("ALTER TABLE events DROP COLUMN causation_id")
        connection.execute("UPDATE schema_meta SET schema_version=46 WHERE singleton=1")
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION
    assert _schema_version(migrated) == _schema_version(fresh) == LATEST_SCHEMA_VERSION

    backup = tmp_path / "backup"
    assert backup_state(migrated, backup)["schema_version"] == LATEST_SCHEMA_VERSION
    held = tmp_path / "held"
    restore_state(backup, held)
    client = TestClient(create_app(held))
    headers = _owner_headers(held)
    assert client.get("/operator/health", headers=headers).status_code == 200
    assert (
        client.post(
            "/projects",
            headers=headers | {"Idempotency-Key": "held-write"},
            json={"name": "must remain held"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("table", RUNTIME_V55_TABLES)
def test_runtime_fixture_cleanup_refuses_populated_authority_before_any_ddl(tmp_path, table):
    """Synthetic sentinel in each ledger: no DDL may precede the empty check."""
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        # Synthetic sentinels only, not authenticated enrollment or financial authority.
        columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        values = {
            column[1]: 1 if column[2] == "INTEGER" else "fixture-sentinel" for column in columns
        }
        values.update(state="allocated", disposition="uncertain", kind="policy")
        names = [column[1] for column in columns]
        connection.execute(
            f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            [
                values[name]
                if name != "state" or table != "runtime_native_root_fences"
                else "claimed"
                for name in names
            ],
        )
        schema = connection.execute("SELECT * FROM sqlite_schema ORDER BY name").fetchall()
        statements = []
        connection.set_trace_callback(statements.append)
        with pytest.raises(AssertionError, match="Refusing to remove populated"):
            strip_empty_runtime_v55(connection)
        assert not any(statement.lstrip().upper().startswith("DROP") for statement in statements)
        assert connection.execute("SELECT * FROM sqlite_schema ORDER BY name").fetchall() == schema
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_runtime_fixture_cleanup_preserves_history_and_rolls_back(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    assert (
        client.post(
            "/projects",
            headers=_owner_headers(state) | {"Idempotency-Key": "prior"},
            json={"name": "Retained history"},
        ).status_code
        == 201
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        schema = connection.execute("SELECT * FROM sqlite_schema ORDER BY name").fetchall()
        prior_tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if row[0] not in RUNTIME_V55_TABLES + CHUNK_V60_TABLES
        ]

        def history_digest():
            # Hash comparisons avoid emitting retained credential/history contents on failure.
            return hashlib.sha256(
                repr(
                    {
                        table: connection.execute(f"SELECT * FROM {table}").fetchall()
                        for table in prior_tables
                    }
                ).encode()
            ).hexdigest()

        before = history_digest()
        connection.execute("BEGIN")
        strip_empty_runtime_v55(connection)
        drop_empty_chunk_schema(connection)
        assert history_digest() == before
        assert (
            connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        connection.rollback()
        assert connection.execute("SELECT * FROM sqlite_schema ORDER BY name").fetchall() == schema
        assert_empty_runtime_v55(connection)
        assert history_digest() == before
        connection.execute("BEGIN")
        strip_empty_runtime_v55(connection)
        drop_empty_chunk_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=54")
        connection.commit()
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert_empty_runtime_v55(connection)
        assert history_digest() == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_data_class_v61_migration_marks_existing_snapshots_private_derived(tmp_path):
    """ADR 0039: v61 -> v62 converges with a fresh schema and infers nothing public."""
    from test_execution import _owner

    from tessera_control.snapshots import SnapshotStore
    from tessera_control.store import Store, canonical_digest
    from tessera_control.workspace import capture_workspace, inventory_digest

    fresh, migrated = tmp_path / "class-fresh", tmp_path / "class-migrated"
    initialize_state(fresh)
    initialize_state(migrated)
    owner = _owner(migrated)
    _, project = Store(migrated).create_project(
        owner, "project", canonical_digest({"name": "Classes"}), {"name": "Classes"}
    )
    root = tmp_path / "input"
    root.mkdir()
    (root / "data.txt").write_bytes(b"pre-v62 input")
    capture = capture_workspace(str(root))
    _, snapshot = SnapshotStore(migrated).adopt_snapshot(
        owner, project["id"], "adopt", inventory_digest(capture.inventory), capture
    )
    with sqlite3.connect(migrated / "tessera.sqlite3") as db:
        for trigger in ("no_update", "no_delete", "no_replace"):
            db.execute(f"DROP TRIGGER snapshot_data_classes_{trigger}")
        db.execute("DROP TABLE snapshot_data_classes")
        db.execute("UPDATE schema_meta SET schema_version=61")
        db.execute(
            "UPDATE operator_backup_health SET last_success_at='synthetic',last_backup_id='old'"
        )
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION == 70
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(fresh / "tessera.sqlite3") as expected:
        with sqlite3.connect(migrated / "tessera.sqlite3") as actual:
            for name in (
                "snapshot_data_classes",
                "snapshot_data_classes_no_update",
                "snapshot_data_classes_no_delete",
                "snapshot_data_classes_no_replace",
            ):
                query = "SELECT type, sql FROM sqlite_schema WHERE name=?"
                assert actual.execute(query, (name,)).fetchone() == (
                    expected.execute(query, (name,)).fetchone()
                )
            assert actual.execute(
                "SELECT snapshot_id, data_class, origin, license, source, actor_principal_id "
                "FROM snapshot_data_classes"
            ).fetchall() == [(snapshot["id"], "private_derived", "migrated", None, None, None)]
            assert actual.execute(
                "SELECT last_success_at,last_backup_id FROM operator_backup_health"
            ).fetchone() == (None, None)
    assert SnapshotStore(migrated).get_snapshot(owner, snapshot["id"])["data_class"][
        "data_class"
    ] == ("private_derived")
