"""A real v67 database migrates to the latest schema with the gate tables, converged with a
fresh install (ADR 0045). A change that reserves version 68 must also keep its own tables."""

from __future__ import annotations

import sqlite3

from tessera_control.migrations import LATEST_SCHEMA_VERSION, migrate_database
from tessera_control.store import _connect, initialize_state

GATE_TABLES = (
    "campaign_gate_decisions",
    "campaign_unlock_evaluations",
    "gate_paired_groups",
    "gate_paired_members",
)


def _shape(path) -> dict:
    connection = sqlite3.connect(path)
    try:
        objects = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        columns = {
            name: [tuple(row[1:3]) for row in connection.execute(f"PRAGMA table_info({name})")]
            for (kind, name) in objects
            if kind == "table"
        }
        return {"objects": objects, "columns": columns}
    finally:
        connection.close()


def test_v67_database_migrates_to_latest_and_matches_a_fresh_install(tmp_path):
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    old = tmp_path / "old"
    initialize_state(old)
    path = old / "tessera.sqlite3"
    connection = sqlite3.connect(path)
    for table in GATE_TABLES:
        connection.execute(f"DROP TABLE {table}")
    connection.execute("ALTER TABLE campaign_standing_approvals DROP COLUMN unlock_json")
    connection.execute("UPDATE schema_meta SET schema_version=67")
    connection.commit()
    connection.close()
    assert not {n for k, n in _shape(path)["objects"] if k == "table"} & set(GATE_TABLES)

    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION  # a second run is a no-op
    migrated, expected = _shape(path), _shape(fresh / "tessera.sqlite3")
    assert set(GATE_TABLES) <= {n for k, n in migrated["objects"] if k == "table"}
    assert migrated["objects"] == expected["objects"]
    assert migrated["columns"] == expected["columns"]
