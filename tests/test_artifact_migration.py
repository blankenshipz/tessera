import sqlite3

import pytest
from test_input_delivery import _delivery

from tessera_control.artifacts import create_artifact_schema
from tessera_control.migrations import MIGRATIONS
from tessera_control.store import Store, migrate_state


def _v6(tmp_path):
    state, _, _, _, grant, lease = _delivery(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE execution_result_checkpoints")
        connection.execute("DROP TABLE artifact_dispositions")
        connection.execute("DROP TABLE artifact_manifests")
        connection.execute("DROP TABLE artifact_uploads")
        connection.execute("DROP TABLE artifacts")
        connection.execute("UPDATE schema_meta SET schema_version=6")
    return state, grant, lease


def _assert_authority_preserved(state, grant, lease):
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT status, snapshot_digest FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone() == ("active", grant["snapshot_digest"])
        assert (
            connection.execute(
                "SELECT epoch FROM execution_leases WHERE id=?", (lease["id"],)
            ).fetchone()[0]
            == lease["epoch"]
        )
        assert (
            connection.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0]
            == 60
        )


def test_schema6_migration_preserves_live_authority_and_exposure(tmp_path):
    state, grant, lease = _v6(tmp_path)
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 17
    assert migrate_state(state) == 17
    _assert_authority_preserved(state, grant, lease)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_artifact_migration_failure_rolls_back_ddl_and_preserves_authority(tmp_path, monkeypatch):
    state, grant, lease = _v6(tmp_path)

    def fail(connection):
        create_artifact_schema(connection)
        raise RuntimeError("synthetic artifact migration failure")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 6, fail)
        with pytest.raises(RuntimeError, match="artifact migration failure"):
            migrate_state(state)
    _assert_authority_preserved(state, grant, lease)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 6
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone()
            is None
        )
    assert migrate_state(state) == 17
