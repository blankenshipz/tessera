"""StoreControl.record_rejection: the executor's delegate writes bounded, code-only events."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from campaign_fixture import Env

from tessera_control.campaign_salad_executor import StoreControl
from tessera_control.stage_rejections import EVENT
from tessera_control.store import Conflict

SECRET = "host.example.invalid 10.0.0.9 secret-token"


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        db.execute(
            "INSERT INTO execution_grants (id, project_id, task_id, task_revision, plan_digest, "
            "worker_principal_id, snapshot_id, snapshot_digest, snapshot_release_id, "
            "snapshot_release_expires_at, pinned_image, currency, max_exposure_cents, "
            "acknowledges_unmeasured_infrastructure, status, revision, epoch, expires_at, "
            "created_at, updated_at, created_by_principal_id, settlement_cents) VALUES "
            "('grant_sc', ?, 't', 1, 'p', ?, 'sn', 'd', 'r', ?, 'img', 'USD', 7, 1, 'active', "
            "1, 3, ?, ?, ?, 'creator', NULL)",
            (env.project["id"], env.worker["id"], now, now, now, now),
        )
        db.execute(
            "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
            "hold_cents, grant_id, revision, created_at, updated_at) "
            "VALUES ('att_sc', ?, 's1', 2, 'reserved', 4, 'grant_sc', 1, ?, ?)",
            (campaign["id"], now, now),
        )
    return env, campaign


def _events(env):
    return [
        json.loads(row["payload_json"])
        for row in env.query(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", EVENT
        )
    ]


def test_record_rejection_writes_one_code_only_event_and_deduplicates(world):
    env, campaign = world
    control = StoreControl(env.state, env.worker)
    error = Conflict(f"Worker heartbeat is stale. {SECRET}")
    control.record_rejection("att_sc", "lease", error)
    control.record_rejection("att_sc", "lease", error)
    assert _events(env) == [
        {
            "campaign_id": campaign["id"],
            "stage_key": "s1",
            "attempt": 2,
            "step": "lease",
            "code": "stale_heartbeat",
        }
    ]


def test_record_rejection_by_another_worker_or_org_writes_nothing(world):
    env, _ = world
    for worker in ({**env.worker, "id": "other_worker"}, {**env.worker, "org_id": "other_org"}):
        StoreControl(env.state, worker).record_rejection("att_sc", "lease", Conflict(SECRET))
    assert _events(env) == []


def test_record_rejection_of_a_non_store_error_records_only_the_fact(world):
    env, _ = world
    StoreControl(env.state, env.worker).record_rejection("att_sc", "package", RuntimeError(SECRET))
    events = _events(env)
    assert len(events) == 1
    assert events[0]["code"] == "unexpected_error"
    assert events[0]["step"] == "package"
    assert set(events[0]) == {"campaign_id", "stage_key", "attempt", "step", "code"}
    raw = json.dumps(
        [tuple(r) for r in env.query("SELECT * FROM events WHERE event_type=?", EVENT)]
    )
    for part in ("host.example.invalid", "10.0.0.9", "secret-token", "RuntimeError"):
        assert part not in raw
