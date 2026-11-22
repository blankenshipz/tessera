"""Schema 82 adds the attempt result assessment table additively and idempotently."""

from __future__ import annotations

import sqlite3

from tessera_control.migrations import (
    ATTEMPT_ASSESSMENT_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
)
from tessera_control.store import initialize_state, migrate_state

TABLE = "research_attempt_result_assessments"


def _objects(path):
    with sqlite3.connect(path / "tessera.sqlite3") as db:
        return db.execute(
            "SELECT type,name FROM sqlite_master WHERE name LIKE ? ORDER BY name", (f"{TABLE}%",)
        ).fetchall()


def test_the_previous_version_migrates_to_the_assessment_schema_and_matches_a_fresh_database(
    tmp_path,
) -> None:
    assert LATEST_SCHEMA_VERSION == ATTEMPT_ASSESSMENT_SCHEMA_VERSION == 82
    previous = ATTEMPT_ASSESSMENT_SCHEMA_VERSION - 1
    assert previous in MIGRATIONS  # this migration is registered at its own version
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    old = tmp_path / "old"
    initialize_state(old)
    with sqlite3.connect(old / "tessera.sqlite3") as db:
        for event in ("update", "delete"):
            db.execute(f"DROP TRIGGER {TABLE}_no_{event}")
        db.execute(f"DROP TABLE {TABLE}")
        db.execute("UPDATE schema_meta SET schema_version=?", (previous,))
        before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert migrate_state(old) == ATTEMPT_ASSESSMENT_SCHEMA_VERSION
    assert migrate_state(old) == ATTEMPT_ASSESSMENT_SCHEMA_VERSION  # idempotent
    assert _objects(old) == _objects(fresh)
    assert {name for kind, name in _objects(old) if kind == "table"} == {TABLE}
    with sqlite3.connect(old / "tessera.sqlite3") as db:
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == (
            ATTEMPT_ASSESSMENT_SCHEMA_VERSION
        )
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0


def test_the_migration_registers_exactly_one_step_and_no_shim() -> None:
    """Versions between main's latest and this one are not stood in for: the chain is real."""
    version = ATTEMPT_ASSESSMENT_SCHEMA_VERSION
    assert MIGRATIONS[version - 1].__name__ == "migrate_attempt_assessment_schema"
    assert not any(getattr(step, "__name__", "") == "advance" for step in MIGRATIONS.values())
