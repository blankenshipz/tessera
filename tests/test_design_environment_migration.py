"""Schema 83: nullable environment digest and metric added to existing databases."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tessera_control.migrations import (
    DESIGN_ENVIRONMENT_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    migrate_database,
)
from tessera_control.recovery import _table_shapes
from tessera_control.store import initialize_state

VARIANTS = "research_experiment_variants"
DESIGNS = "research_experiment_designs"


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _downgrade_to_v82(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"ALTER TABLE {VARIANTS} DROP COLUMN environment_digest")
        db.execute(f"ALTER TABLE {DESIGNS} DROP COLUMN metric")
        db.execute("UPDATE schema_meta SET schema_version=82")
        db.commit()


def test_version_is_registered_as_one_step_after_82() -> None:
    assert DESIGN_ENVIRONMENT_SCHEMA_VERSION == 83 <= LATEST_SCHEMA_VERSION
    assert MIGRATIONS[82].__name__ == "migrate_design_environment_schema"


def test_v82_database_gains_columns_idempotently_with_null_rows(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    path = state / "tessera.sqlite3"
    _downgrade_to_v82(path)
    with closing(sqlite3.connect(path)) as db:
        assert "environment_digest" not in _columns(db, VARIANTS)
        assert "metric" not in _columns(db, DESIGNS)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            f"INSERT INTO {DESIGNS} (experiment_id,revision,goal,success_criterion,protocol,"
            "created_at,created_by_principal_id) VALUES ('e1',1,'g','s','p','t','p')"
        )
        db.execute(
            f"INSERT INTO {VARIANTS} (id,experiment_id,design_revision,kind,name,method_digest,"
            "input_digest,metadata_json,revision,created_at,created_by_principal_id) "
            f"VALUES ('v1','e1',1,'baseline','b','{'a' * 64}','{'b' * 64}','{{}}',1,'t','p')"
        )
        db.commit()
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as db:
        assert "environment_digest" in _columns(db, VARIANTS)
        assert "metric" in _columns(db, DESIGNS)
        assert db.execute(f"SELECT environment_digest FROM {VARIANTS}").fetchone()[0] is None
        assert db.execute(f"SELECT metric FROM {DESIGNS}").fetchone()[0] is None


@pytest.mark.parametrize("migrated", [False, True])
def test_check_rejects_a_bad_environment_digest(tmp_path: Path, migrated: bool) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    path = state / "tessera.sqlite3"
    if migrated:
        _downgrade_to_v82(path)
        migrate_database(path, _connect)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        insert = (
            f"INSERT INTO {VARIANTS} (id,experiment_id,design_revision,kind,name,method_digest,"
            "input_digest,environment_digest,metadata_json,revision,created_at,"
            "created_by_principal_id) VALUES (?,?,1,'baseline','b','a','b',?,'{}',1,'t','p')"
        )
        db.execute(insert, ("ok", "ok", "0123456789abcdef" * 4))
        db.execute(insert, ("null", "null", None))
        for index, bad in enumerate(("A" * 64, "a" * 63, "g" * 64, "")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(insert, (f"bad{index}", f"bad{index}", bad))


def test_migrated_database_has_the_same_shapes_as_a_fresh_one(tmp_path: Path) -> None:
    fresh, old = tmp_path / "fresh", tmp_path / "old"
    initialize_state(fresh)
    initialize_state(old)
    _downgrade_to_v82(old / "tessera.sqlite3")
    migrate_database(old / "tessera.sqlite3", _connect)
    with (
        closing(sqlite3.connect(fresh / "tessera.sqlite3")) as a,
        closing(sqlite3.connect(old / "tessera.sqlite3")) as b,
    ):
        assert _table_shapes(a) == _table_shapes(b)
