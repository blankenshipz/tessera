"""Control refusals of an executor step become bounded, code-only stage events."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control import stage_rejections as module
from tessera_control.stage_rejections import EVENT, MAX_PER_STAGE, code_for, record
from tessera_control.store import Conflict, Forbidden, NotFound, StoreError

SECRET = "host.example.invalid 10.0.0.9 secret-token"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Campaign is not running.", "campaign_not_running"),
        ("Grant already has a lease.", "lease_already_held"),
        ("Worker authority expires too soon.", "worker_authority_expiring"),
        ("Worker heartbeat is stale.", "stale_heartbeat"),
        ("Lease epoch is stale.", "stale_epoch"),
        ("Lease has expired.", "lease_expired"),
        ("A destination capability is required.", "capability_required"),
    ],
)
def test_known_refusal_texts_map_to_stable_codes(text, code):
    assert code_for(Conflict(text)) == code
    assert code_for(Conflict(f"{text} {SECRET}")) == code


def test_unknown_refusals_get_a_class_code_and_never_carry_text():
    assert code_for(Conflict(SECRET)) == "refused_conflict"
    assert code_for(Forbidden(SECRET)) == "refused_forbidden"
    assert code_for(NotFound(SECRET)) == "refused_not_found"
    assert code_for(StoreError(SECRET)) == "refused"
    for error in (Conflict(SECRET), Forbidden(SECRET), NotFound(SECRET), ValueError(SECRET)):
        code = code_for(error)
        for part in ("host.example.invalid", "10.0.0.9", "secret-token"):
            assert part not in code


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        db.execute(
            "INSERT INTO execution_grants (id, project_id, task_id, task_revision, plan_digest, "
            "worker_principal_id, snapshot_id, snapshot_digest, snapshot_release_id, "
            "snapshot_release_expires_at, pinned_image, currency, max_exposure_cents, "
            "acknowledges_unmeasured_infrastructure, status, revision, epoch, expires_at, "
            "created_at, updated_at, created_by_principal_id, settlement_cents) VALUES "
            "('grant_rej', ?, 't', 1, 'p', ?, 'sn', 'd', 'r', ?, 'img', 'USD', 7, 1, 'active', "
            "1, 3, ?, ?, ?, 'creator', NULL)",
            (env.project["id"], env.worker["id"], now, now, now, now),
        )
        db.execute(
            "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
            "hold_cents, grant_id, revision, created_at, updated_at) "
            "VALUES ('att_rej', ?, 's1', 2, 'reserved', 4, 'grant_rej', 1, ?, ?)",
            (campaign["id"], now, now),
        )
    return env, campaign, org


def _events(env):
    return [
        json.loads(row["payload_json"])
        for row in env.query(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", EVENT
        )
    ]


def test_record_writes_one_code_only_event(world):
    env, campaign, _ = world
    error = Conflict(f"Worker heartbeat is stale. {SECRET}")
    assert record(env.store, env.worker, "att_rej", "lease", error) is True
    assert _events(env) == [
        {
            "campaign_id": campaign["id"],
            "stage_key": "s1",
            "attempt": 2,
            "step": "lease",
            "code": "stale_heartbeat",
        }
    ]
    raw = json.dumps(
        [tuple(r) for r in env.query("SELECT * FROM events WHERE event_type=?", EVENT)]
    )
    for part in ("host.example.invalid", "10.0.0.9", "secret-token"):
        assert part not in raw


def test_record_deduplicates_within_the_window_but_not_other_step_or_code(world):
    env, _, _ = world
    error = Conflict(SECRET)
    assert record(env.store, env.worker, "att_rej", "lease", error) is True
    assert record(env.store, env.worker, "att_rej", "lease", error) is False
    assert len(_events(env)) == 1
    assert record(env.store, env.worker, "att_rej", "package", error) is True
    assert record(env.store, env.worker, "att_rej", "lease", Forbidden(SECRET)) is True
    assert [(e["step"], e["code"]) for e in _events(env)] == [
        ("lease", "refused_conflict"),
        ("package", "refused_conflict"),
        ("lease", "refused_forbidden"),
    ]


def test_record_dedup_expires_after_the_window(world, monkeypatch):
    env, _, _ = world
    error = Conflict(SECRET)
    assert record(env.store, env.worker, "att_rej", "lease", error) is True
    env.execute(
        "UPDATE events SET created_at=? WHERE event_type=?",
        (datetime.now(UTC) - timedelta(seconds=module.DEDUP_SECONDS + 60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        EVENT,
    )
    assert record(env.store, env.worker, "att_rej", "lease", error) is True
    assert len(_events(env)) == 2


def test_record_stops_at_the_per_stage_cap(world):
    env, _, _ = world
    written = 0
    # 7 steps x 3 refusal classes = 21 distinct (step, code) pairs, one more than the cap.
    for step in sorted(module.STEPS):
        for kind in (Conflict, Forbidden, NotFound):
            written += record(env.store, env.worker, "att_rej", step, kind(SECRET))
    assert written == MAX_PER_STAGE
    assert len(_events(env)) == MAX_PER_STAGE
    assert record(env.store, env.worker, "att_rej", "execute", StoreError(SECRET)) is False


def test_record_ignores_what_it_must_not_record(world):
    env, _, _ = world
    store, worker = env.store, env.worker
    assert record(store, worker, "att_rej", "not_a_step", Conflict(SECRET)) is False
    assert record(store, worker, "att_unknown", "lease", Conflict(SECRET)) is False
    assert (
        record(store, {**worker, "id": "other_worker"}, "att_rej", "lease", Conflict(SECRET))
        is False
    )
    assert (
        record(store, {**worker, "org_id": "other_org"}, "att_rej", "lease", Conflict(SECRET))
        is False
    )
    assert _events(env) == []


def test_record_maps_a_non_store_error_to_unexpected_error_without_text(world):
    env, _, _ = world
    assert record(env.store, env.worker, "att_rej", "lease", ValueError(SECRET)) is True
    events = _events(env)
    assert [e["code"] for e in events] == ["unexpected_error"]
    raw = json.dumps(
        [tuple(r) for r in env.query("SELECT * FROM events WHERE event_type=?", EVENT)]
    )
    for part in ("host.example.invalid", "10.0.0.9", "secret-token"):
        assert part not in raw
