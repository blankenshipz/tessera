"""Schema v61 -> v62: campaign tables and the private_derived release class."""

from __future__ import annotations

import sqlite3

import pytest
from campaign_fixture import Env
from test_execution import _grant_payload, _task

from tessera_control import recovery
from tessera_control.migrations import LATEST_SCHEMA_VERSION, migrate_database
from tessera_control.store import _connect, canonical_digest, validate_database

CAMPAIGN_TABLES = (
    "campaign_stage_io",
    "campaign_salad_attempts",
    "campaign_salad_stages",
    "campaign_step_intents",
    "campaign_stage_attempts",
    "campaign_stages",
    "campaign_approvals",
    "campaign_receipts",
    "campaign_runner_lease",
    "campaigns",
)
OLD_RELEASES = """CREATE TABLE snapshot_releases (
    id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
    worker_principal_id TEXT NOT NULL REFERENCES principals(id),
    actor_principal_id TEXT NOT NULL REFERENCES principals(id),
    digest TEXT NOT NULL, classification TEXT NOT NULL CHECK(classification='public'),
    rationale TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    revision INTEGER NOT NULL, revoked_at TEXT)"""


def _downgrade_to_v61(env: Env) -> list[tuple]:
    """Rewrite a fresh database into the v61 shape (disposable test data only)."""
    path = env.state / "tessera.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        rows = connection.execute("SELECT * FROM snapshot_releases").fetchall()
        connection.execute("DROP TABLE snapshot_releases")
        connection.execute(OLD_RELEASES)
        connection.executemany(
            "INSERT INTO snapshot_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        connection.execute(
            "CREATE UNIQUE INDEX active_snapshot_release "
            "ON snapshot_releases(snapshot_id, worker_principal_id) WHERE status='active'"
        )
        for table in CAMPAIGN_TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=61")
        connection.execute(
            "UPDATE operator_backup_health SET last_success_at='2026-01-01T00:00:00Z'"
        )
        return rows
    finally:
        connection.close()


def test_migration_rebuilds_releases_keeps_rows_and_references(tmp_path):
    env = Env(tmp_path)
    task = _task(env.state, env.owner, env.project["id"], env.snapshot_digest, "migrate")
    payload = _grant_payload(task, env.worker, {"id": env.snapshot_id})
    _, grant = env.execution.issue_execution_grant(
        env.owner, env.project["id"], "grant", canonical_digest(payload), payload
    )
    old_rows = _downgrade_to_v61(env)
    path = env.state / "tessera.sqlite3"
    with pytest.raises(Exception, match="newer|unsupported|migrat|older"):
        validate_database(path)
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    validate_database(path)
    connection = _connect(path)
    try:
        assert [tuple(r) for r in connection.execute("SELECT * FROM snapshot_releases")] == [
            tuple(r) for r in old_rows
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT snapshot_release_id FROM execution_grants WHERE id=?", (grant["id"],)
            ).fetchone()[0]
            == env.release_id
        )
        # The widened CHECK admits the new class and still refuses others.
        connection.execute("UPDATE snapshot_releases SET classification='private_derived'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE snapshot_releases SET classification='secret'")
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name='active_snapshot_release'"
            ).fetchone()[0]
            == 1
        )
        for table in CAMPAIGN_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            connection.execute("SELECT last_success_at FROM operator_backup_health").fetchone()[0]
            is None
        )
        # Receipts are append-only.
        connection.execute("INSERT INTO campaign_runner_lease VALUES (1, 'h', 'x')")
    finally:
        connection.close()
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION  # already current: no-op


def test_migrated_shapes_match_a_fresh_database_for_backup_validation(tmp_path):
    env = Env(tmp_path)
    _downgrade_to_v61(env)
    path = env.state / "tessera.sqlite3"
    migrate_database(path, _connect)
    expected, expected_triggers, allowed = recovery._expected_tables()
    connection = sqlite3.connect(path)
    try:
        actual, actual_triggers, _ = recovery._table_shapes(connection, allowed)
    finally:
        connection.close()
    for table in ("snapshot_releases", *CAMPAIGN_TABLES, "campaign_approvals"):
        assert actual[table] == expected[table], table
    assert actual_triggers == expected_triggers


def test_receipts_are_append_only(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    now = "2026-01-01T00:00:00Z"
    env.execute(
        "INSERT INTO campaign_receipts VALUES (?, '{}', ?, ?)", campaign["id"], "0" * 64, now
    )
    for sql in ("UPDATE campaign_receipts SET receipt_json='x'", "DELETE FROM campaign_receipts"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            env.execute(sql)


def test_one_active_approval_per_campaign(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    with pytest.raises(sqlite3.IntegrityError):
        env.execute(
            "INSERT INTO campaign_approvals (id, campaign_id, spec_digest, approving_principal_id, "
            "decision_class, automation_label, approved_at, expires_at, status, ended_at) VALUES "
            "('dup', ?, ?, ?, 'x', NULL, 'a', 'b', 'active', NULL)",
            campaign["id"],
            campaign["spec_digest"],
            approval["approving_principal_id"],
        )


def test_v62_without_data_classes_converges_at_v65(tmp_path):
    """A dev database stamped 62 by an early campaign build gets ADR 0039's table."""
    env = Env(tmp_path)
    _downgrade_to_v61(env)
    path = env.state / "tessera.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("DROP TABLE snapshot_data_classes")
        raw.execute("UPDATE schema_meta SET schema_version=62")
    finally:
        raw.close()
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    validate_database(path)
    connection = _connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM snapshot_data_classes").fetchone()[0] >= 1
        assert (
            connection.execute(
                "SELECT data_class FROM snapshot_data_classes WHERE snapshot_id=?",
                (env.snapshot_id,),
            ).fetchone()[0]
            == "private_derived"
        )
    finally:
        connection.close()


def _drop_code_approvals(path) -> None:
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        for suffix in ("no_update", "no_delete", "no_replace"):
            raw.execute(f"DROP TRIGGER IF EXISTS snapshot_code_approvals_{suffix}")
        raw.execute("DROP TABLE snapshot_code_approvals")
    finally:
        raw.close()


def test_code_approval_v63_migrates_to_v65(tmp_path):
    """A main database at v63 (code approval, no campaigns) gains the campaign tables."""
    env = Env(tmp_path)
    old_rows = _downgrade_to_v61(env)
    path = env.state / "tessera.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("UPDATE schema_meta SET schema_version=63")
    finally:
        raw.close()
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    validate_database(path)
    connection = _connect(path)
    try:
        assert [tuple(r) for r in connection.execute("SELECT * FROM snapshot_releases")] == [
            tuple(r) for r in old_rows
        ]
        assert connection.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0
    finally:
        connection.close()
    assert (
        migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    )  # convergent: a second run is a no-op


def test_campaign_v63_without_code_approvals_converges_at_v65(tmp_path):
    """A dev database stamped 63 by an earlier campaign build gains the approval table."""
    env = Env(tmp_path)
    path = env.state / "tessera.sqlite3"
    _drop_code_approvals(path)
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("UPDATE schema_meta SET schema_version=63")
    finally:
        raw.close()
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    validate_database(path)
    connection = _connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM snapshot_code_approvals").fetchone()[0] == 0
    finally:
        connection.close()


STANDING_TABLES = (
    "campaign_standing_triggers",
    "campaign_standing_starts",
    "campaign_standing_approvals",
)


def _downgrade_to_v66(path):
    connection = sqlite3.connect(path)
    try:
        for trigger in ("update", "delete"):
            connection.execute(f"DROP TRIGGER campaign_standing_starts_no_{trigger}")
        for table in STANDING_TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE campaign_approvals DROP COLUMN standing_approval_id")
        connection.execute("UPDATE schema_meta SET schema_version=66")
        connection.execute(
            "UPDATE operator_backup_health SET last_success_at='synthetic',last_backup_id='old'"
        )
        connection.commit()
    finally:
        connection.close()


def test_v66_to_v67_adds_standing_tables_and_column(tmp_path):
    env = Env(tmp_path)
    path = env.state / "tessera.sqlite3"
    fresh = {
        name: sql
        for name, sql in sqlite3.connect(path).execute(
            "SELECT name, sql FROM sqlite_schema WHERE name LIKE 'campaign_standing%' "
            "OR name='campaign_approvals'"
        )
    }
    _downgrade_to_v66(path)
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION  # a second run is a no-op
    validate_database(path)
    connection = _connect(path)
    try:
        migrated = {
            name: sql
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE name LIKE 'campaign_standing%' "
                "OR name='campaign_approvals'"
            )
        }
        assert migrated.keys() == fresh.keys()
        for table in STANDING_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        columns = [r[1] for r in connection.execute("PRAGMA table_info(campaign_approvals)")]
        assert columns[-1] == "standing_approval_id"
        # The chain through v68 also keeps (or creates) the ADR 0044 review pre-approval
        # tables, identical to a fresh install, and v69 adds the ADR 0045 gate tables.
        preapproval = (
            "SELECT name, sql FROM sqlite_schema "
            "WHERE name LIKE 'campaign_review_preapproval%' ORDER BY name"
        )
        fresh_dir = tmp_path / "fresh"
        fresh_dir.mkdir(mode=0o700)
        expected = (
            sqlite3.connect(Env(fresh_dir).state / "tessera.sqlite3")
            .execute(preapproval)
            .fetchall()
        )
        assert {name for name, _ in expected} >= {
            "campaign_review_preapprovals",
            "campaign_review_preapproval_uses",
        }
        assert [tuple(row) for row in connection.execute(preapproval)] == expected
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("SELECT last_success_at FROM operator_backup_health").fetchone()[0]
            is None
        )
    finally:
        connection.close()


def test_standing_starts_append_only(tmp_path):
    from test_campaign_standing import Sched

    sched = Sched(tmp_path)
    standing = sched.create()
    sched.tick()
    (start,) = sched.starts(standing["id"])
    connection = _connect(sched.env.state / "tessera.sqlite3")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE campaign_standing_starts SET start_index=9")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM campaign_standing_starts")
        assert start["campaign_id"]
    finally:
        connection.close()
