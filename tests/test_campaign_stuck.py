"""Stuck-campaign detector: every reason code, the receipts, resolve/reopen and the scan pass.

Campaigns come from the real fixture; stuck conditions are set up with direct updates and
synthetic rows in the real tables (foreign keys off for the synthetic grant and lease rows).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control import campaign_stuck as stuck
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.store import _connect, _utc_string

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def ts(seconds_ago: int) -> str:
    return _utc_string(NOW - timedelta(seconds=seconds_ago))


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.db_path = self.env.state / "tessera.sqlite3"
        self.org = self.env.owner["org_id"]
        self.project = self.env.project["id"]
        campaign = self.env.submit()
        self.campaign = campaign["id"]
        self.stage = self.sql("SELECT stage_key FROM campaign_stages")[0]["stage_key"]

    def sql(self, statement, *args):
        connection = _connect(self.db_path)
        try:
            rows = connection.execute(statement, args).fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def raw(self, statement, *args):
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(statement, args)

    def set_status(self, status, stop_reason=None, age=0):
        self.sql(
            "UPDATE campaigns SET status=?, stop_reason=?, updated_at=? WHERE id=?",
            status, stop_reason, ts(age), self.campaign,
        )  # fmt: skip
        self.sql("UPDATE campaign_stages SET updated_at=?", ts(age))

    def attempt(self, status, age=0, attempt=1, grant_id=None):
        ident = f"attempt_{attempt}_{status}"
        self.raw(
            """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
            hold_cents, grant_id, revision, created_at, updated_at) VALUES (?,?,?,?,?,0,?,1,?,?)""",
            ident, self.campaign, self.stage, attempt, status, grant_id, ts(age), ts(age),
        )  # fmt: skip
        return ident

    def grant_and_lease(self, lease_expired_ago, grant_status="active"):
        self.raw(
            """INSERT INTO execution_grants (id, project_id, task_id, task_revision, plan_digest,
            worker_principal_id, snapshot_id, snapshot_digest, snapshot_release_id,
            snapshot_release_expires_at, pinned_image, currency, max_exposure_cents,
            acknowledges_unmeasured_infrastructure, status, revision, epoch, expires_at,
            created_at, updated_at, created_by_principal_id)
            VALUES ('grant_1', ?, 'task_1', 1, ?, 'worker_1', 'snap_1', ?, 'rel_1', ?, 'img', 'USD',
            0, 0, ?, 1, 1, ?, ?, ?, 'principal_1')""",
            self.project, "a" * 64, "b" * 64, ts(-9999), grant_status, ts(-9999), ts(9999),
            ts(9999),
        )  # fmt: skip
        self.raw(
            """INSERT INTO execution_leases (id, grant_id, worker_principal_id, epoch, issued_at,
            expires_at, hard_expires_at, renewed_at)
            VALUES ('lease_1', 'grant_1', 'worker_1', 1, ?, ?, ?, ?)""",
            ts(lease_expired_ago + 600), ts(lease_expired_ago), ts(lease_expired_ago - 60),
            ts(lease_expired_ago + 300),
        )  # fmt: skip

    def scan(self, now=NOW, actor=None):
        connection = _connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            out = stuck.scan_project(
                connection, self.org, self.project, now,
                lambda _c: actor or self.env.owner["id"],
            )  # fmt: skip
            connection.execute("COMMIT")
            return out
        finally:
            connection.close()

    def findings(self, status="all"):
        connection = _connect(self.db_path)
        try:
            return stuck.list_findings(connection, self.org, self.project, status)
        finally:
            connection.close()

    def events(self, kind):
        rows = self.sql("SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", kind)
        return [json.loads(r["payload_json"]) for r in rows]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def codes(world):
    return sorted(f["code"] for f in world.findings("open"))


def test_a_healthy_or_terminal_campaign_has_no_finding(world):
    world.set_status("running", age=30)
    assert world.scan()["opened"] == [] and world.findings() == []
    for status in ("succeeded", "failed", "cancelled", "stopped"):
        world.set_status(status, age=10**6)
        assert world.scan()["opened"] == []


def test_a_blocked_campaign_names_the_stop_reason_and_the_next_action(world):
    world.set_status("blocked", stop_reason="budget_refused:s1", age=120)
    (found,) = world.scan()["opened"]
    assert found["code"] == "campaign_blocked"
    assert found["suggested_action"] == "inspect_stop_reason_then_resume_or_cancel"
    assert found["evidence"] == {"status": "blocked", "stop_reason": "budget_refused:s1",
                                 "age_seconds": 120}  # fmt: skip
    assert found["status"] == "open" and found["occurrences"] == 1


def test_a_lapsed_delegation_is_its_own_code(world):
    world.set_status("paused_delegation_lapsed", stop_reason="delegation_expired", age=60)
    (found,) = world.scan()["opened"]
    assert found["code"] == "delegation_lapsed"
    assert found["suggested_action"] == "renew_delegation_by_reapproval"


def test_an_unknown_attempt_and_a_blocked_unknown_stage(world):
    world.set_status("blocked", stop_reason="attempt_unknown", age=10)
    ident = world.attempt("unknown", age=500)
    world.sql("UPDATE campaign_stages SET status='blocked_unknown' WHERE stage_key=?", world.stage)
    got = {f["code"]: f for f in world.scan()["opened"]}
    unknown = got["attempt_outcome_unknown"]
    assert unknown["suggested_action"] == "resolve_unknown_attempt"
    assert unknown["evidence"]["attempt_id"] == ident and unknown["stage_key"] == world.stage
    # The stage is explained by the attempt: one unknown finding, plus the blocked campaign.
    assert [f["code"] for f in world.findings()].count("attempt_outcome_unknown") == 1
    assert "campaign_blocked" in got


def test_a_stage_blocked_unknown_without_an_attempt_row_is_found(world):
    world.set_status("running", age=5)
    world.sql("UPDATE campaign_stages SET status='blocked_unknown' WHERE stage_key=?", world.stage)
    (found,) = world.scan()["opened"]
    assert found["code"] == "attempt_outcome_unknown"
    assert found["evidence"]["stage_status"] == "blocked_unknown"


def test_a_stale_execution_lease_honours_the_grace(world):
    world.set_status("running", age=5)
    world.grant_and_lease(lease_expired_ago=200)
    world.attempt("running", age=5, grant_id="grant_1")
    assert world.scan()["opened"] == []  # inside the 300 s default grace
    world.sql(
        "INSERT INTO campaign_stuck_settings VALUES (?,?,?,?,1,'p',?)",
        world.org,
        world.project,
        3600,
        60,
        ts(0),
    )  # grace 60 s
    (found,) = world.scan()["opened"]
    assert found["code"] == "stale_execution_lease"
    assert found["suggested_action"] == "reconcile_or_cancel_the_grant"
    assert (
        found["evidence"]["lease_id"] == "lease_1"
        and found["evidence"]["expired_seconds_ago"] == 200
    )


def test_a_reconciled_grant_or_a_finished_attempt_is_not_a_stale_lease(world):
    world.set_status("running", age=5)
    world.grant_and_lease(lease_expired_ago=5000, grant_status="reconciled")
    world.attempt("running", age=5, grant_id="grant_1")
    assert world.scan()["opened"] == []
    world.sql("UPDATE execution_grants SET status='active'")
    world.sql("UPDATE campaign_stage_attempts SET status='succeeded'")
    assert world.scan()["opened"] == []


def test_a_stale_runner_lease_is_one_finding_per_active_campaign(world):
    world.set_status("running", age=5)
    world.sql("DELETE FROM campaign_runner_lease")
    world.sql("INSERT INTO campaign_runner_lease VALUES (1,'runner_a',?)", ts(900))
    (found,) = world.scan()["opened"]
    assert found["code"] == "runner_lease_stale"
    assert found["suggested_action"] == "check_the_campaign_runner_service"
    assert found["evidence"]["runner_lease_expired_seconds_ago"] == 900
    # A live lease clears it.
    world.sql("UPDATE campaign_runner_lease SET expires_at=?", ts(-30))
    assert world.scan()["resolved"] == [found["id"]]


def test_no_progress_uses_the_configured_age_and_covers_stages_and_attempts(world):
    world.set_status("running", age=4000)
    (found,) = world.scan()["opened"]
    assert found["code"] == "no_progress"
    assert found["suggested_action"] == "check_provider_capacity_or_cancel"
    assert found["evidence"] == {"status": "running", "unchanged_seconds": 4000,
                                 "threshold_seconds": 3600}  # fmt: skip
    # A fresh attempt row counts as change.
    world.sql("UPDATE campaign_stuck_findings SET status='resolved'")
    world.attempt("running", age=10)
    assert world.scan()["opened"] == []
    # A longer configured age silences the old one.
    world.sql("DELETE FROM campaign_stage_attempts")
    world.sql(
        "INSERT INTO campaign_stuck_settings VALUES (?,?,?,?,1,'p',?)",
        world.org,
        world.project,
        86400,
        300,
        ts(0),
    )
    assert world.scan()["opened"] == []


def test_a_scan_is_idempotent_and_writes_one_receipt_per_change(world):
    world.set_status("blocked", stop_reason="x", age=100)
    first = world.scan()
    second = world.scan()
    assert len(first["opened"]) == 1 and second["opened"] == [] and second["open_count"] == 1
    (event,) = world.events(stuck.DETECTED)
    (row,) = world.findings()
    assert event["receipt_sha256"] == row["receipt_sha256"]
    assert row["receipt_sha256"] == stuck.receipt_digest(
        row["id"], row["code"], first["opened"][0]["evidence"], 1
    )
    assert set(event) == {"finding_id", "campaign_id", "stage_key", "code", "suggested_action",
                          "evidence", "receipt_sha256"}  # fmt: skip


def test_a_finding_resolves_when_the_condition_clears_and_reopens_when_it_returns(world):
    world.set_status("blocked", stop_reason="x", age=100)
    fid = world.scan()["opened"][0]["id"]
    world.set_status("running", age=1)
    assert world.scan()["resolved"] == [fid]
    assert world.findings("resolved")[0]["id"] == fid
    assert len(world.events(stuck.RESOLVED)) == 1
    world.set_status("blocked", stop_reason="y", age=100)
    (again,) = world.scan()["opened"]
    assert again["id"] == fid and again["occurrences"] == 2 and again["status"] == "open"
    assert again["receipt_sha256"] != world.events(stuck.DETECTED)[0]["receipt_sha256"]
    assert len(world.events(stuck.DETECTED)) == 2


def test_acknowledging_marks_it_seen_without_resolving_it(world):
    world.set_status("blocked", stop_reason="x", age=100)
    fid = world.scan()["opened"][0]["id"]
    connection = _connect(world.db_path)
    connection.execute("BEGIN IMMEDIATE")
    done = stuck.acknowledge(
        connection, world.org, world.project, fid, world.env.owner["id"], "looking", NOW
    )
    again = stuck.acknowledge(
        connection, world.org, world.project, fid, world.env.worker["id"], "other", NOW
    )
    unknown = stuck.acknowledge(
        connection, world.org, world.project, "stuck_none", world.env.owner["id"], None, NOW
    )
    connection.execute("COMMIT")
    connection.close()
    assert done["status"] == "acknowledged" and done["acknowledged_by"] == world.env.owner["id"]
    assert (
        again["acknowledged_by"] == world.env.owner["id"] and unknown is None
    )  # a replay changes nothing
    assert world.scan()["opened"] == [] and world.findings("acknowledged")[0]["id"] == fid
    (event,) = world.events(stuck.ACKNOWLEDGED)
    assert event["note_present"] is True and "looking" not in json.dumps(event)


def test_findings_are_permanent_and_keep_their_identity(world):
    world.set_status("blocked", stop_reason="x", age=100)
    world.scan()
    with pytest.raises(sqlite3.DatabaseError, match="permanent"):
        world.sql("DELETE FROM campaign_stuck_findings")
    with pytest.raises(sqlite3.DatabaseError, match="identity"):
        world.sql("UPDATE campaign_stuck_findings SET code='no_progress'")


def test_no_event_or_row_carries_content(world):
    world.set_status("blocked", stop_reason="budget_refused:s1", age=100)
    world.scan()
    blob = json.dumps([world.events(stuck.DETECTED), world.findings()])
    for forbidden in ("argv", "image", "spec_json", "token", "secret"):
        assert forbidden not in blob


def test_the_periodic_pass_scans_every_project_and_swallows_a_failure(world, monkeypatch):
    world.set_status("blocked", stop_reason="x", age=100)
    assert stuck.scan_all(world.db_path, clock=lambda: NOW) == 1
    assert len(world.findings()) == 1
    monkeypatch.setattr(
        stuck, "scan_project", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    assert stuck.scan_all(world.db_path, clock=lambda: NOW) == 0  # swallowed, nothing raised


def test_the_background_scanner_stops_and_zero_disables_it(world):
    assert stuck.start_background(world.db_path, 0)._thread is None
    handle = stuck.start_background(world.db_path, 3600)
    handle.stop()
    assert not handle._thread.is_alive()


def test_the_migration_adds_the_tables_to_an_existing_72_database(tmp_path):
    from tessera_control.migrations import migrate_database

    world = World(tmp_path)
    world.sql("DROP TABLE campaign_stuck_findings")
    world.sql("DROP TABLE campaign_stuck_settings")
    world.sql("UPDATE schema_meta SET schema_version=72")
    assert migrate_database(world.db_path, _connect) == LATEST_SCHEMA_VERSION
    assert world.sql("SELECT name FROM sqlite_master WHERE name='campaign_stuck_findings'")
    assert (
        migrate_database(world.db_path, _connect) == LATEST_SCHEMA_VERSION
    )  # a second run is a no-op


def test_every_receipt_of_an_occurrence_carries_the_same_evidence_and_digest(world):
    """Detected, acknowledged and resolved events and the row agree: the evidence is frozen at
    detection. Fails if a later scan refreshes the evidence but not the digest."""
    world.set_status("blocked", stop_reason="x", age=100)
    fid = world.scan(NOW)["opened"][0]["id"]
    world.scan(NOW + timedelta(hours=1))  # still detected: only last_seen_at moves
    connection = _connect(world.db_path)
    connection.execute("BEGIN IMMEDIATE")
    stuck.acknowledge(
        connection, world.org, world.project, fid, world.env.owner["id"], "seen",
        NOW + timedelta(hours=2),
    )  # fmt: skip
    connection.execute("COMMIT")
    connection.close()
    world.set_status("running", age=0)
    world.scan(NOW + timedelta(hours=3))  # cleared: resolved
    (row,) = world.findings("resolved")
    payloads = [
        world.events(kind)[0] for kind in (stuck.DETECTED, stuck.ACKNOWLEDGED, stuck.RESOLVED)
    ]
    assert row["last_seen_at"] != row["first_seen_at"]
    for payload in [*payloads, row]:
        recomputed = stuck.receipt_digest(fid, row["code"], payload["evidence"], row["occurrences"])
        assert recomputed == payload["receipt_sha256"] == row["receipt_sha256"]
    assert payloads[0]["evidence"] == payloads[2]["evidence"] == row["evidence"]


def test_a_failing_project_is_recorded_once_then_every_tenth_pass(world, monkeypatch):
    world.set_status("blocked", stop_reason="x", age=100)
    monkeypatch.setattr(
        stuck, "scan_project", lambda *a, **k: (_ for _ in ()).throw(ValueError("secret-detail"))
    )
    failures: dict = {}
    for _ in range(11):
        assert stuck.scan_all(world.db_path, clock=lambda: NOW, failures=failures) == 0
    events = world.events(stuck.STUCK_SCAN_FAILED)
    assert [e["consecutive_failures"] for e in events] == [1, 10]
    assert events[0]["error_class"] == "ValueError" and "secret-detail" not in json.dumps(events)
    assert events[0]["project_id"] == world.project
    monkeypatch.undo()
    assert stuck.scan_all(world.db_path, clock=lambda: NOW, failures=failures) == 1
    assert failures == {}  # a success resets the count
    assert len(world.findings()) == 1  # and the healthy pass scanned normally
