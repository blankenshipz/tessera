"""Schema 88: the append-only link from a model-spend call to the grant that pays for it."""

import sqlite3

import pytest

from tessera_control.migrations import (
    GRANT_MODEL_SPEND_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
)
from tessera_control.store import initialize_state, migrate_state

NAMES = (
    "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'model_spend_grant_links%' "
    "ORDER BY name"
)


def test_version_is_registered_as_one_step_after_87():
    assert GRANT_MODEL_SPEND_SCHEMA_VERSION == 88 <= LATEST_SCHEMA_VERSION
    assert MIGRATIONS[87].__name__ == "migrate_grant_model_spend_schema"


def test_previous_version_migrates_and_fresh_state_matches(tmp_path):
    fresh, state = tmp_path / "fresh", tmp_path / "old"
    initialize_state(fresh)
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("DROP TABLE model_spend_grant_links")
        db.execute(f"UPDATE schema_meta SET schema_version={GRANT_MODEL_SPEND_SCHEMA_VERSION - 1}")
        assert db.execute(NAMES).fetchall() == []
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION  # a second run is a no-op
    with (
        sqlite3.connect(state / "tessera.sqlite3") as db,
        sqlite3.connect(fresh / "tessera.sqlite3") as ref,
    ):
        assert db.execute(NAMES).fetchall() == ref.execute(NAMES).fetchall() != []
        assert db.execute("SELECT COUNT(*) FROM model_spend_grant_links").fetchone()[0] == 0


def test_links_are_append_only(tmp_path):
    initialize_state(tmp_path / "s")
    with sqlite3.connect(tmp_path / "s" / "tessera.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO model_spend_grant_links VALUES ('c','o','g','2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE model_spend_grant_links SET grant_id='h'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM model_spend_grant_links")
