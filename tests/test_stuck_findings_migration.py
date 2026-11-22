"""campaign_stuck_findings code CHECK widened for heavy-stage codes by migration 79."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta

import pytest
from campaign_fixture import Env
from test_campaign_stuck import NOW, World

from tessera_control import campaign_stuck as stuck
from tessera_control.migrations import (
    LATEST_SCHEMA_VERSION,
    STUCK_HEAVY_STAGE_SCHEMA_VERSION,
)
from tessera_control.store import _connect, initialize_state, migrate_state

COLUMNS = (
    "id,org_id,project_id,campaign_id,stage_key,code,subject,suggested_action,status,"
    "occurrences,first_seen_at,last_seen_at,resolved_at,evidence_json,receipt_sha256,"
    "acknowledged_by,acknowledged_at,note"
)


def _insert(db, org, project, campaign, code, ident="stuck_a"):
    db.execute(
        f"INSERT INTO campaign_stuck_findings ({COLUMNS}) VALUES "
        "(?,?,?,?,NULL,?,'subj','act','open',1,'t0','t1',NULL,'{}','sha',NULL,NULL,'n')",
        (ident, org, project, campaign, code),
    )


@pytest.fixture
def old_db(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()["id"]
    path = env.state / "tessera.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        old_sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='campaign_stuck_findings'"
        ).fetchone()[0]
        assert "heavy_stage_on_local" in old_sql
        old_sql = re.sub(r"'no_progress',[^)]*\)", "'no_progress')", old_sql, count=1)
        assert "heavy_stage" not in old_sql
        db.execute("DROP TABLE campaign_stuck_findings")  # also drops index and triggers
        db.execute(old_sql)
        db.execute(
            "CREATE INDEX campaign_stuck_findings_by_project "
            "ON campaign_stuck_findings(project_id, status, last_seen_at)"
        )
        _insert(db, env.owner["org_id"], env.project["id"], campaign, "no_progress")
        db.execute(
            "UPDATE schema_meta SET schema_version=?", (STUCK_HEAVY_STAGE_SCHEMA_VERSION - 1,)
        )
        before = db.execute("SELECT * FROM campaign_stuck_findings").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            _insert(
                db, env.owner["org_id"], env.project["id"], campaign, "heavy_stage_on_local", "x"
            )
    return env, path, campaign, before


def test_migration_preserves_rows_and_admits_heavy_stage_codes(old_db):
    env, path, campaign, before = old_db
    assert migrate_state(env.state) == LATEST_SCHEMA_VERSION >= STUCK_HEAVY_STAGE_SCHEMA_VERSION
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM campaign_stuck_findings").fetchall() == before
        for i, code in enumerate(("heavy_stage_on_local", "heavy_stage_refused_local")):
            _insert(db, env.owner["org_id"], env.project["id"], campaign, code, f"new{i}")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(db, env.owner["org_id"], env.project["id"], campaign, "bogus", "bad")
        assert db.execute("SELECT count(*) FROM campaign_stuck_findings").fetchone() == (3,)
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )


def test_migrated_table_keeps_triggers_and_index_identical_to_a_fresh_database(old_db, tmp_path):
    env, path, campaign, _ = old_db
    migrate_state(env.state)
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    query = (
        "SELECT type, name, sql FROM sqlite_schema WHERE tbl_name='campaign_stuck_findings' "
        "ORDER BY name"
    )
    with sqlite3.connect(path) as a, sqlite3.connect(fresh / "tessera.sqlite3") as b:
        assert a.execute(query).fetchall() == b.execute(query).fetchall()
        with pytest.raises(sqlite3.DatabaseError, match="permanent"):
            a.execute("DELETE FROM campaign_stuck_findings")
        with pytest.raises(sqlite3.DatabaseError, match="identity"):
            a.execute("UPDATE campaign_stuck_findings SET subject='other'")


def test_a_fresh_database_reports_the_latest_version(tmp_path):
    initialize_state(tmp_path / "s")
    with sqlite3.connect(tmp_path / "s" / "tessera.sqlite3") as db:
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )


def _real_findings_world(tmp_path):
    """Detected, acknowledged and resolved findings, each with its events, via the detector."""
    world = World(tmp_path)
    world.set_status("blocked", stop_reason="attempt_unknown", age=10)
    world.attempt("unknown", age=500)
    world.sql("UPDATE campaign_stages SET status='blocked_unknown' WHERE stage_key=?", world.stage)
    opened = {f["code"]: f["id"] for f in world.scan(NOW)["opened"]}
    connection = _connect(world.db_path)
    connection.execute("BEGIN IMMEDIATE")
    stuck.acknowledge(
        connection, world.org, world.project, opened["attempt_outcome_unknown"],
        world.env.owner["id"], "looking", NOW + timedelta(minutes=1),
    )  # fmt: skip
    connection.execute("COMMIT")
    connection.close()
    # The blocked campaign clears (resolved); a stale lease is a new, still-open finding.
    world.set_status("running", age=5)
    world.grant_and_lease(lease_expired_ago=1000)
    world.attempt("running", age=5, attempt=2, grant_id="grant_1")
    world.scan(NOW + timedelta(minutes=2))
    return world


def test_real_findings_survive_the_rebuild_with_every_value_and_the_triggers(tmp_path):
    world = _real_findings_world(tmp_path)
    by_status = {f["status"] for f in world.findings("all")}
    assert {"open", "acknowledged", "resolved"} <= by_status
    kinds = (stuck.DETECTED, stuck.ACKNOWLEDGED, stuck.RESOLVED)
    events_before = [world.events(kind) for kind in kinds]
    assert all(events_before)
    path = world.db_path
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        rows_before = db.execute("SELECT * FROM campaign_stuck_findings ORDER BY id").fetchall()
        assert len(rows_before) == 3
        old_sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='campaign_stuck_findings'"
        ).fetchone()[0]
        old_sql = re.sub(r"'no_progress',[^)]*\)", "'no_progress')", old_sql, count=1)
        assert "heavy_stage" not in old_sql
        db.execute("DROP TABLE campaign_stuck_findings")  # also drops index and triggers
        db.execute(old_sql)
        db.execute(
            "CREATE INDEX campaign_stuck_findings_by_project "
            "ON campaign_stuck_findings(project_id, status, last_seen_at)"
        )
        marks = ",".join("?" * len(rows_before[0]))
        db.executemany(f"INSERT INTO campaign_stuck_findings VALUES ({marks})", rows_before)
        db.execute(
            "UPDATE schema_meta SET schema_version=?", (STUCK_HEAVY_STAGE_SCHEMA_VERSION - 1,)
        )
    assert migrate_state(world.env.state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM campaign_stuck_findings ORDER BY id").fetchall() == (
            rows_before
        )
        assert [r[1] for r in db.execute("PRAGMA table_info(campaign_stuck_findings)")] == (
            COLUMNS.split(",")
        )
        with pytest.raises(sqlite3.DatabaseError, match="permanent"):
            db.execute("DELETE FROM campaign_stuck_findings")
        with pytest.raises(sqlite3.DatabaseError, match="identity"):
            db.execute("UPDATE campaign_stuck_findings SET code='no_progress'")
        for i, code in enumerate(("heavy_stage_on_local", "heavy_stage_refused_local")):
            _insert(db, world.org, world.project, world.campaign, code, f"heavy{i}")
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    query = "SELECT type, name, sql FROM sqlite_schema WHERE tbl_name='campaign_stuck_findings'"
    with sqlite3.connect(path) as a, sqlite3.connect(fresh / "tessera.sqlite3") as b:
        assert sorted(a.execute(query).fetchall()) == sorted(b.execute(query).fetchall())
    # The detector's receipts are untouched by the rebuild.
    assert [world.events(kind) for kind in kinds] == events_before
    assert json.dumps(events_before)
