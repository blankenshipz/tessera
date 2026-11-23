"""Owner audit of heavy stages running on a local provider (ADR 0107): which are running now,
and was each fallback explained. Events are inserted directly; the audit only reads them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_stuck as stuck
from tessera_control.store import _connect, _utc_string

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
EXPLAINED = sorted(guard.EXPLAINED_REASONS)[0]
UNEXPLAINED = "not_listed"


def ts(seconds_ago: int) -> str:
    return _utc_string(NOW - timedelta(seconds=seconds_ago))


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.db = self.env.state / "tessera.sqlite3"
        self.org = self.env.owner["org_id"]
        self.project = self.env.project["id"]
        self.first = self.env.submit()["id"]
        self.stage = self.sql("SELECT stage_key FROM campaign_stages")[0]["stage_key"]
        self.sql("UPDATE campaigns SET status='running', updated_at=?", ts(5))

    def second(self):
        campaign = self.env.submit()["id"]
        self.sql("UPDATE campaigns SET status='running', updated_at=? WHERE id=?", ts(5), campaign)
        return campaign

    def sql(self, statement, *args):
        connection = _connect(self.db)
        try:
            rows = connection.execute(statement, args).fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def warn(self, campaign, reasons, attempt=1, provider="garage", detail=None, age=60):
        self.sql(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, payload_json) VALUES (?,?,NULL,?,1,?,?,?)""",
            self.org, self.project, "campaign.heavy_stage_local_fallback", self.env.owner["id"],
            ts(age),
            json.dumps({"campaign_id": campaign, "stage_key": self.stage, "attempt": attempt,
                        "provider_id": provider, "signals": ["heavy_tag"], "reasons": reasons,
                        "detail": detail, "mode": "warn"}),
        )  # fmt: skip

    def attempt(self, campaign, status, attempt=1):
        ident = f"{campaign}_a{attempt}"
        self.sql(
            """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
            hold_cents, revision, created_at, updated_at) VALUES (?,?,?,?,?,0,1,?,?)""",
            ident, campaign, self.stage, attempt, status, ts(5), ts(5),
        )  # fmt: skip
        return ident

    def audit(self):
        connection = _connect(self.db)
        try:
            return stuck.heavy_local_audit(connection, self.org, self.project, NOW)
        finally:
            connection.close()

    def scan_all(self):
        assert stuck.scan_all(self.db, lambda: NOW) == 1
        connection = _connect(self.db)
        try:
            return stuck.list_findings(connection, self.org, self.project, "open")
        finally:
            connection.close()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_running_local_attempt_with_an_explained_reason_is_listed_explained(world):
    detail = [{"provider_id": "salad", "reason": "over_cap"}]
    world.warn(world.first, [EXPLAINED], detail=detail)
    ident = world.attempt(world.first, "running")
    (item,) = world.audit()
    assert item["campaign_id"] == world.first and item["attempt_id"] == ident
    assert item["provider_id"] == "garage" and item["reasons"] == [EXPLAINED]
    assert item["explained"] is True and item["detail"] == detail
    (finding,) = world.scan_all()
    assert finding["code"] == "heavy_stage_on_local"
    assert finding["evidence"]["explained"] is True and finding["evidence"]["detail"] == detail


def test_an_unexplained_fallback_is_listed_unexplained(world):
    world.warn(world.first, [UNEXPLAINED])
    world.attempt(world.first, "running")
    (item,) = world.audit()
    assert item["explained"] is False
    (finding,) = world.scan_all()
    assert finding["evidence"]["explained"] is False


def test_scan_all_lists_heavy_stages_of_two_campaigns(world):
    other = world.second()
    for campaign in (world.first, other):
        world.warn(campaign, [EXPLAINED])
        world.attempt(campaign, "running")
    assert {i["campaign_id"] for i in world.audit()} == {world.first, other}
    findings = world.scan_all()
    assert {f["campaign_id"] for f in findings} == {world.first, other}
    assert {f["code"] for f in findings} == {"heavy_stage_on_local"}


def test_a_finished_attempt_is_not_listed(world):
    world.warn(world.first, [EXPLAINED])
    world.attempt(world.first, "succeeded")
    assert world.audit() == []
    assert world.scan_all() == []


def test_a_light_stage_on_local_is_not_listed(world):
    world.attempt(world.first, "running")  # no heavy-stage fallback event recorded
    assert world.audit() == []
    assert world.scan_all() == []


def test_no_remote_profile_is_audited_and_fires_as_a_config_gap(world):
    world.warn(world.first, ["no_remote_profile"])
    world.attempt(world.first, "running")
    (item,) = world.audit()
    assert item["reasons"] == ["no_remote_profile"]
    assert item["explained"] is guard.explained(["no_remote_profile"], None)
    (finding,) = world.scan_all()
    assert finding["code"] == "heavy_stage_on_local"
    assert finding["evidence"]["config_gap"] == "no_remote_profile"
