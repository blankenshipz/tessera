"""Upgraded historical databases must converge on the fresh schema shapes."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.recovery import _expected_tables, _table_shapes, _validate_snapshot
from tessera_control.store import migrate_state

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("state_v*.sql"))


def _migrated(tmp_path: Path, fixture: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database = state / "tessera.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.executescript(fixture.read_text())
    database.chmod(0o600)
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    return database


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda path: path.stem)
def test_migrated_fixture_matches_fresh_shapes_and_passes_backup_check(tmp_path, fixture):
    assert FIXTURES, "no historical fixtures found"
    database = _migrated(tmp_path, fixture)
    expected, expected_triggers, allowed = _expected_tables()
    with closing(sqlite3.connect(database)) as db:
        actual, actual_triggers, _ = _table_shapes(db, allowed)
    assert actual.keys() == expected.keys()
    # Mirror _validate_snapshot: early databases may omit nonunique indexes and
    # foreign keys, but never carry extra or changed ones.
    for name, (columns, keys, indexes) in actual.items():
        want_columns, want_keys, want_indexes = expected[name]
        assert columns == want_columns, name
        assert all(key in want_keys for key in keys), name
        assert all(index in want_indexes for index in indexes), name
        assert all(index in indexes for index in want_indexes if index[1]), name
    assert actual_triggers == expected_triggers
    # The recovery module's own validation of a backup file accepts the database.
    snapshot = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as dest:
        source.backup(dest)
    assert _validate_snapshot(snapshot) is not None
    shutil.rmtree(tmp_path / "state")
