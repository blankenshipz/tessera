"""Schema 78: nullable conclusion decision and evidence stance added to existing databases."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tessera_control.migrations import (
    CONCLUSION_DECISION_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    migrate_database,
)
from tessera_control.store import Store, initialize_state

_TABLES = {
    "research_conclusions": "decision",
    "research_conclusion_evidence": "stance",
    "research_admitted_conclusion_evidence": "stance",
}


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _downgrade_to_v77(path: Path, version: int = 77) -> None:
    """Rebuild the three tables without the new columns, as a schema-77 database had them."""
    with closing(sqlite3.connect(path)) as db:
        for table, column in _TABLES.items():
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        db.execute(f"UPDATE schema_meta SET schema_version={version}")
        db.commit()


def test_v77_database_gains_nullable_columns_idempotently(tmp_path: Path) -> None:
    assert CONCLUSION_DECISION_SCHEMA_VERSION == LATEST_SCHEMA_VERSION == 78
    state = tmp_path / "state"
    initialize_state(state)
    path = state / "tessera.sqlite3"
    _downgrade_to_v77(path)
    with closing(sqlite3.connect(path)) as db:
        assert all(column not in _columns(db, t) for t, column in _TABLES.items())
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO organizations VALUES ('o','O','2026-01-01T00:00:00Z')")
        db.commit()
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as db:
        for table, column in _TABLES.items():
            assert column in _columns(db, table)
        assert db.execute("SELECT count(*) FROM organizations WHERE id='o'").fetchone()[0] == 1


def test_existing_rows_get_null_and_check_rejects_invalid_values(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    path = state / "tessera.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO research_conclusions (id,experiment_id,design_revision,statement,status,"
            "revision,created_at,updated_at,created_by_principal_id) "
            "VALUES ('c1','e1',NULL,'old','draft',1,'t','t','p')"
        )
        db.execute(
            "INSERT INTO research_conclusion_evidence VALUES ('c1','a1','art1',NULL)"
            if len(_columns(db, "research_conclusion_evidence")) == 4
            else "INSERT INTO research_conclusion_evidence (conclusion_id,attempt_id,artifact_id,"
            "measurement_id) VALUES ('c1','a1','art1',NULL)"
        )
        db.commit()
    _downgrade_to_v77(path)  # drops the columns again, keeping the legacy rows
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        assert db.execute("SELECT decision FROM research_conclusions").fetchone()[0] is None
        assert db.execute("SELECT stance FROM research_conclusion_evidence").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE research_conclusions SET decision='accept'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE research_conclusion_evidence SET stance='neutral'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO research_admitted_conclusion_evidence "
                "(conclusion_id,attempt_id,artifact_id,measurement_id,stance) "
                "VALUES ('c1','a1','x',NULL,'maybe')"
            )
        db.execute("UPDATE research_conclusions SET decision='continue'")
        db.execute("UPDATE research_conclusion_evidence SET stance='opposes'")
    Store(state)  # the migrated database still opens through the normal store


def _shape(path: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(path)) as db:
        names = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
        ]
        return {
            name: sorted(tuple(r[1:]) for r in db.execute(f"PRAGMA table_info({name})"))
            for name in names
        }


@pytest.mark.parametrize("version", [74, 76, 77])
def test_populated_older_database_migrates_to_78_and_equals_a_fresh_state(
    tmp_path: Path, version: int
) -> None:
    from test_model_spend_store import _seed

    fresh, old = tmp_path / "fresh", tmp_path / "old"
    initialize_state(fresh)
    initialize_state(old)
    path = old / "tessera.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        _seed(db)  # a project and a model-spend ledger row
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO research_conclusions (id,experiment_id,design_revision,statement,status,"
            "revision,created_at,updated_at,created_by_principal_id) "
            "VALUES ('c1','e1',NULL,'old','draft',1,'t','t','p')"
        )
        db.commit()
        before = {
            t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
            for t in ("projects", "principals", "model_spend_calls")
        }
    _downgrade_to_v77(path, version)
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as db:
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert db.execute("SELECT id, decision FROM research_conclusions").fetchall() == [
            ("c1", None)
        ]
    assert _shape(path) == _shape(fresh / "tessera.sqlite3")
    Store(old)
