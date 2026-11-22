"""Heavy-stage placement guard surfaces (ADR 0107): campaign status and the stuck detector.

The guard's events are inserted directly; this file only reads them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control import campaign_stuck as stuck
from tessera_control.store import _connect, _utc_string

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def ts(seconds_ago: int) -> str:
    return _utc_string(NOW - timedelta(seconds=seconds_ago))


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.db = self.env.state / "tessera.sqlite3"
        self.org = self.env.owner["org_id"]
        self.project = self.env.project["id"]
        self.campaign = self.env.submit()["id"]
        self.stage = self.sql("SELECT stage_key FROM campaign_stages")[0]["stage_key"]
        self.sql("UPDATE campaigns SET status='running', updated_at=?", ts(5))
        self.sql("UPDATE campaign_stages SET updated_at=?", ts(5))

    def sql(self, statement, *args):
        connection = _connect(self.db)
        try:
            rows = connection.execute(statement, args).fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def event(self, kind, payload, age=0):
        self.sql(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, payload_json) VALUES (?,?,NULL,?,1,?,?,?)""",
            self.org, self.project, kind, self.env.owner["id"], ts(age),
            json.dumps({"campaign_id": self.campaign, **payload}),
        )  # fmt: skip

    def warn(self, age=0, attempt=1):
        self.event(
            "campaign.heavy_stage_local_fallback",
            {"stage_key": self.stage, "attempt": attempt, "provider_id": "local-docker",
             "signals": ["heavy_tag"], "reasons": ["no_fitting_profile"],
             "detail": [{"provider_id": "salad", "reason": "not_enrolled"}], "mode": "warn"},
            age,
        )  # fmt: skip

    def refuse(self, age=0, attempt=1):
        self.event(
            "campaign.stage_waiting_placement",
            {"stage_key": self.stage, "attempt": attempt, "signature": "x", "steering": None,
             "skipped": [{"provider_id": "local-docker", "reason": "heavy_local_refused",
                          "detail": "credential_unavailable,remote_skipped"}]},
            age,
        )  # fmt: skip

    def attempt(self, status, attempt=1):
        ident = f"attempt_{attempt}"
        self.sql(
            """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
            hold_cents, revision, created_at, updated_at) VALUES (?,?,?,?,?,0,1,?,?)""",
            ident, self.campaign, self.stage, attempt, status, ts(5), ts(5),
        )  # fmt: skip
        return ident

    def status(self):
        return self.env.store.get_campaign(self.env.owner, self.campaign)

    def mine(self):
        return next(s for s in self.status()["stages"] if s["stage_key"] == self.stage)

    def scan(self):
        connection = _connect(self.db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            out = stuck.scan_project(
                connection, self.org, self.project, NOW, lambda _c: self.env.owner["id"]
            )
            connection.execute("COMMIT")
            return out
        finally:
            connection.close()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_status_has_nothing_for_a_stage_without_events(world):
    assert all("heavy_guard" not in stage for stage in world.status()["stages"])
    assert world.scan()["opened"] == []


def test_status_shows_the_latest_warning(world):
    world.warn(age=300)
    world.warn(age=100, attempt=2)
    guard = world.mine()["heavy_guard"]
    assert guard == {
        "mode": "warn", "reasons": ["no_fitting_profile"], "signals": ["heavy_tag"],
        "provider_id": "local-docker", "waiting": False,
        "recorded_at": ts(100),
    }  # fmt: skip


def test_status_shows_a_refusal_while_waiting_and_not_once_placed(world):
    world.refuse(age=400)
    guard = world.mine()["heavy_guard"]
    assert guard["mode"] == "refuse" and guard["waiting"] is True
    assert guard["reasons"] == ["credential_unavailable", "remote_skipped"]
    assert guard["provider_id"] == "local-docker" and guard["recorded_at"] == ts(400)
    world.event("campaign.stage_placed", {"stage_key": world.stage, "attempt": 1}, 10)
    assert world.mine()["heavy_guard"]["waiting"] is False


def test_a_waiting_event_without_the_refusal_reason_shows_nothing(world):
    world.event(
        "campaign.stage_waiting_placement",
        {"stage_key": world.stage, "attempt": 1,
         "skipped": [{"provider_id": "salad", "reason": "capacity", "detail": ""}]},
    )  # fmt: skip
    assert "heavy_guard" not in world.mine()


def test_a_running_heavy_attempt_with_a_warning_is_stuck_on_local(world):
    world.warn(age=90)
    ident = world.attempt("running")
    (found,) = world.scan()["opened"]
    assert found["code"] == "heavy_stage_on_local"
    assert found["suggested_action"] == "check_salad_credential_or_profile"
    assert found["stage_key"] == world.stage
    assert found["evidence"] == {
        "attempt_id": ident, "attempt": 1, "attempt_status": "running",
        "provider_id": "local-docker", "reasons": ["no_fitting_profile"],
        "signals": ["heavy_tag"], "age_seconds": 90,
    }  # fmt: skip


def test_a_warning_without_a_live_attempt_or_for_another_attempt_is_not_stuck(world):
    world.warn()
    assert world.scan()["opened"] == []
    world.attempt("succeeded")
    assert world.scan()["opened"] == []
    world.sql("DELETE FROM campaign_stage_attempts")
    world.attempt("running", attempt=2)  # the warning was for attempt 1
    assert world.scan()["opened"] == []


def test_a_refused_waiting_stage_is_stuck_only_after_the_grace(world):
    world.refuse(age=200)  # default grace 300 s
    assert world.scan()["opened"] == []
    world.sql("DELETE FROM events WHERE event_type='campaign.stage_waiting_placement'")
    world.refuse(age=500)
    (found,) = world.scan()["opened"]
    assert found["code"] == "heavy_stage_refused_local"
    assert found["suggested_action"] == "restore_remote_capacity_or_allow_local"
    assert found["evidence"] == {
        "stage_key": world.stage, "attempt": 1,
        "reasons": ["credential_unavailable", "remote_skipped"],
        "waiting_seconds": 500, "grace_seconds": 300,
    }  # fmt: skip


def test_a_refused_stage_that_was_placed_since_is_not_stuck(world):
    world.refuse(age=900)
    world.event("campaign.stage_placed", {"stage_key": world.stage, "attempt": 1}, 10)
    assert world.scan()["opened"] == []
