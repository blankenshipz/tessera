"""Schema-51 migration preserves held forensic authority and backup metadata."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import assert_empty_runtime_v55, post, strip_runtime_v55

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import initialize_state, migrate_state, utc_now


@pytest.mark.parametrize("recovery_state", ["active", "held_restore"])
@pytest.mark.parametrize("prior_failure", [False, True])
def test_v50_to_v51_invalidates_only_active_backup_freshness(
    tmp_path, recovery_state, prior_failure
):
    source = tmp_path / "source"
    initialize_state(source)
    owner = {"Authorization": (source / "owner.header").read_text().strip().split(": ", 1)[1]}
    post(TestClient(create_app(source)), owner, "/projects", {"name": "retained history"})
    state = source
    if recovery_state == "held_restore":
        backup = tmp_path / "backup"
        backup_state(source, backup)
        state = tmp_path / "held"
        restore_state(backup, state)
    historical_tables = ("control_recovery", "principals", "credentials", "events", "idempotency")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        # Disposable true v50 shape; migration installs only empty additive tables.
        strip_runtime_v55(db)
        for table in (
            "agent_authority_consumptions",
            "agent_authority_grants",
            "agent_authority_requests",
            "agent_provenance",
            "agent_research_promotions",
            "agent_research_records",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE schema_meta SET schema_version=50")
        db.execute(
            "UPDATE operator_backup_health SET last_success_at=?,last_backup_id='pre-v51',"
            "last_failure_at=?,last_failure_code=?",
            (
                utc_now(),
                utc_now() if prior_failure else None,
                "backup_failed" if prior_failure else None,
            ),
        )
        before_health = db.execute("SELECT * FROM operator_backup_health").fetchall()
        before_history = {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in historical_tables}
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        health = dict(db.execute("SELECT * FROM operator_backup_health").fetchone())
        assert health["last_failure_code"] == ("backup_failed" if prior_failure else None)
        assert bool(health["last_failure_at"]) is prior_failure
        if recovery_state == "active":
            assert health["last_success_at"] is None and health["last_backup_id"] is None
        else:
            db.row_factory = None
            assert db.execute("SELECT * FROM operator_backup_health").fetchall() == before_health
        db.row_factory = None
        assert all(
            db.execute(f"SELECT * FROM {t}").fetchall() == before_history[t]
            for t in historical_tables
        ), "Existing recovery authority/history changed"
        assert db.execute("SELECT state FROM control_recovery").fetchone()[0] == recovery_state
        assert db.execute("SELECT COUNT(*) FROM agent_authority_grants").fetchone()[0] == 0
        assert_empty_runtime_v55(db)
        after_health = db.execute("SELECT * FROM operator_backup_health").fetchall()
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT * FROM operator_backup_health").fetchall() == after_health
    client = TestClient(create_app(state))
    if recovery_state == "held_restore":
        held_owner = {
            "Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]
        }
        assert client.get("/recovery", headers=held_owner).json()["recovery_hold"] is True
        post(client, held_owner, "/projects", {"name": "forbidden"}, expected=403)
    else:
        assert client.get("/health/ready").status_code == 200


@pytest.mark.parametrize("held", [False, True])
def test_v54_to_v55_preserves_history_empty_authority_and_held_readiness(tmp_path, held):
    from tessera_control.migrations import migrate_v54_to_v55

    state = tmp_path / "source"
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    post(TestClient(create_app(state)), owner, "/projects", {"name": "retained v54 history"})
    if held:
        backup = tmp_path / "backup"
        backup_state(state, backup)
        restored = tmp_path / "held"
        restore_state(backup, restored)
        state = restored
    history = ("control_recovery", "principals", "credentials", "projects", "events", "idempotency")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        db.execute("UPDATE schema_meta SET schema_version=54")
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in history}
        backup_health = db.execute("SELECT * FROM operator_backup_health").fetchall()
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 54
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_enrollments'").fetchone()
            is None
        )
        db.execute("BEGIN IMMEDIATE")
        migrate_v54_to_v55(db)
        db.rollback()
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 54
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_enrollments'").fetchone()
            is None
        )
    assert migrate_state(state) == 55
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert_empty_runtime_v55(db)
        assert all(
            db.execute(f"SELECT * FROM {t}").fetchall() == rows for t, rows in before.items()
        )
        if held:
            assert db.execute("SELECT * FROM operator_backup_health").fetchall() == backup_health
    client = TestClient(create_app(state))
    assert client.get("/health/ready").status_code == (503 if held else 200)
    if held:
        held_owner = {
            "Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]
        }
        recovery = client.get("/recovery", headers=held_owner)
        assert recovery.status_code == 200
        assert recovery.json()["recovery_hold"] is True
        post(client, held_owner, "/projects", {"name": "still forbidden"}, expected=403)
