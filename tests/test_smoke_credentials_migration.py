"""The smoke credential grant table is additive: a database one version back migrates to a
schema identical to a fresh one."""

from __future__ import annotations

import sqlite3

from tessera_control.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    SMOKE_CREDENTIALS_SCHEMA_VERSION,
)
from tessera_control.store import initialize_state, migrate_state

TABLE = "smoke_credential_grants"


def _objects(path):
    with sqlite3.connect(path / "tessera.sqlite3") as db:
        return db.execute(
            "SELECT type,name FROM sqlite_master WHERE name LIKE ? ORDER BY name", (f"{TABLE}%",)
        ).fetchall()


def test_previous_version_migrates_and_matches_a_fresh_database(tmp_path) -> None:
    assert SMOKE_CREDENTIALS_SCHEMA_VERSION <= LATEST_SCHEMA_VERSION
    previous = SMOKE_CREDENTIALS_SCHEMA_VERSION - 1
    assert MIGRATIONS[previous].__name__ == "migrate_smoke_credentials_schema"
    fresh, old = tmp_path / "fresh", tmp_path / "old"
    initialize_state(fresh)
    initialize_state(old)
    with sqlite3.connect(old / "tessera.sqlite3") as db:
        for trigger in ("no_delete", "revoke_only"):
            db.execute(f"DROP TRIGGER {TABLE}_{trigger}")
        db.execute(f"DROP TABLE {TABLE}")
        db.execute("UPDATE schema_meta SET schema_version=?", (previous,))
    assert migrate_state(old) == LATEST_SCHEMA_VERSION
    assert migrate_state(old) == LATEST_SCHEMA_VERSION  # idempotent
    assert _objects(old) == _objects(fresh)
    assert {name for kind, name in _objects(old) if kind == "table"} == {TABLE}
