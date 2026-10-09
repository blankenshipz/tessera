"""Schema 55 invalidates only active backup freshness, not forensic/failure history."""

import sqlite3

import pytest
from migration_fixture_helpers import strip_empty_runtime_v55

from tessera_control.store import SCHEMA_VERSION, initialize_state, migrate_state, utc_now


@pytest.mark.parametrize("held", [False, True])
def test_v55_backup_freshness_and_repeat_preserve_failure_and_held_history(tmp_path, held):
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=54")
        if held:
            connection.execute("UPDATE control_recovery SET state='held_restore'")
        connection.execute(
            """UPDATE operator_backup_health SET last_success_at=?,last_backup_id='v54-backup',
            last_failure_at=?,last_failure_code='backup_failed' WHERE singleton=1""",
            (utc_now(), utc_now()),
        )
        before = connection.execute("SELECT * FROM operator_backup_health").fetchone()
    assert migrate_state(state) == SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        after = connection.execute("SELECT * FROM operator_backup_health").fetchone()
        if held:
            assert tuple(after) == before
        else:
            assert after["last_success_at"] is None
            assert after["last_backup_id"] is None
        assert after["last_failure_code"] == "backup_failed"
        assert after["last_failure_at"] is not None
        connection.execute(
            "UPDATE operator_backup_health SET last_success_at=?,last_backup_id='v55-backup'",
            (utc_now(),),
        )
        repeat_before = connection.execute("SELECT * FROM operator_backup_health").fetchone()
    assert migrate_state(state) == SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT * FROM operator_backup_health").fetchone() == tuple(
            repeat_before
        )
