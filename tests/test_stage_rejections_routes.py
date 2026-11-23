"""Worker-route refusals are recorded on the attempt's stage without changing the refusal."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control import stage_rejections as module
from tessera_control.api import create_app
from tessera_control.stage_rejections import EVENT, recording
from tessera_control.store import Conflict

WORKER = {"Authorization": "Bearer worker-token"}


def _stamp(delta=0):
    return (datetime.now(UTC) + timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _grant(db, env, grant_id, worker_id):
    now = _stamp()
    task = env.query("SELECT id, revision, current_plan_digest FROM tasks")[0]
    release = env.query("SELECT expires_at FROM snapshot_releases WHERE id=?", env.release_id)[0]
    db.execute(
        "INSERT INTO execution_grants (id, project_id, task_id, task_revision, plan_digest, "
        "worker_principal_id, snapshot_id, snapshot_digest, snapshot_release_id, "
        "snapshot_release_expires_at, pinned_image, currency, max_exposure_cents, "
        "acknowledges_unmeasured_infrastructure, status, revision, epoch, expires_at, "
        "created_at, updated_at, created_by_principal_id, settlement_cents) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'img', 'USD', 7, 1, 'active', "
        "1, 3, ?, ?, ?, ?, NULL)",
        (
            grant_id,
            env.project["id"],
            task["id"],
            task["revision"],
            task["current_plan_digest"],
            worker_id,
            env.snapshot_id,
            env.snapshot_digest,
            env.release_id,
            release["expires_at"],
            release["expires_at"],
            now,
            now,
            env.owner["id"],
        ),
    )


def _lease(db, grant_id, worker_id, lease_id="lease_rej"):
    db.execute(
        "INSERT INTO execution_leases (id, grant_id, worker_principal_id, epoch, issued_at, "
        "expires_at, hard_expires_at, renewed_at) VALUES (?, ?, ?, 5, ?, ?, ?, ?)",
        (lease_id, grant_id, worker_id, _stamp(), _stamp(600), _stamp(900), _stamp()),
    )


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    now = _stamp()
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        _grant(db, env, "grant_rej", env.worker["id"])
        db.execute(
            "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
            "hold_cents, grant_id, revision, created_at, updated_at) "
            "VALUES ('att_rej', ?, 's1', 2, 'reserved', 4, 'grant_rej', 1, ?, ?)",
            (campaign["id"], now, now),
        )
    client = TestClient(create_app(env.state), raise_server_exceptions=False)
    return env, campaign, client


def _post(client, path, body, headers=WORKER, key="k1"):
    return client.post(path, json=body, headers=headers | {"Idempotency-Key": key})


def _events(env):
    return [
        json.loads(r["payload_json"])
        for r in env.query(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", EVENT
        )
    ]


def _lease_it(env, grant_id="grant_rej"):
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        _lease(db, grant_id, env.worker["id"])


def test_refused_acquire_records_one_event_and_keeps_the_refusal(world):
    env, campaign, client = world
    first = _post(client, "/worker/leases", {"grant_id": "grant_rej"})
    assert first.status_code == 409
    assert "not running" in json.dumps(first.json())
    assert _events(env) == [
        {
            "campaign_id": campaign["id"],
            "stage_key": "s1",
            "attempt": 2,
            "step": "lease",
            "code": "campaign_not_running",
        }
    ]
    again = _post(client, "/worker/leases", {"grant_id": "grant_rej"}, key="k2")
    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert len(_events(env)) == 1  # deduplicated within the window
    assert first.json().keys().isdisjoint({"campaign_id", "stage_key", "attempt"})


def test_already_leased_grant_records_lease_already_held(world):
    env, campaign, client = world
    env.execute("UPDATE campaigns SET status='running' WHERE id=?", campaign["id"])
    _lease_it(env)
    response = _post(client, "/worker/leases", {"grant_id": "grant_rej"})
    assert response.status_code == 409
    assert "already has a lease" in json.dumps(response.json())
    assert [(e["step"], e["code"]) for e in _events(env)] == [("lease", "lease_already_held")]


def test_refusal_matches_a_direct_store_call(world):
    env, _, client = world
    response = _post(client, "/worker/leases", {"grant_id": "grant_rej"})
    with pytest.raises(Conflict) as raised:
        env.execution.acquire_execution_lease(env.worker, "grant_rej", "direct-key", "0" * 64)
    assert response.status_code == 409
    assert str(raised.value) in json.dumps(response.json())


def test_grant_without_a_campaign_attempt_writes_nothing(world):
    env, _, client = world
    env.execute("DELETE FROM campaign_stage_attempts")
    assert _post(client, "/worker/leases", {"grant_id": "grant_rej"}).status_code == 201
    response = _post(client, "/worker/leases", {"grant_id": "grant_rej"}, key="k2")
    assert response.status_code == 409
    assert _events(env) == []


def test_worker_that_does_not_own_the_grant_writes_nothing(world):
    env, _, client = world
    other = env.query("SELECT id FROM principals WHERE id<>?", env.worker["id"])[0]["id"]
    env.execute("UPDATE execution_grants SET worker_principal_id=?", other)
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        _lease(db, "grant_rej", other)
    response = _post(client, "/worker/leases", {"grant_id": "grant_rej"})
    assert response.status_code >= 400
    assert _events(env) == []


@pytest.mark.parametrize(
    ("step", "path", "body"),
    [
        ("renew", "/worker/leases/renew", {"lease_id": "lease_rej", "expected_epoch": 99}),
        (
            "package",
            "/worker/leases/lease_rej/package",
            {"expected_epoch": 99},
        ),
        (
            "observe",
            "/worker/observations",
            {
                "lease_id": "lease_rej",
                "expected_epoch": 99,
                "observed_state": "running",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        ),
    ],
)
def test_lease_steps_record_their_step_with_a_lease(world, step, path, body):
    env, campaign, client = world
    _lease_it(env)
    response = _post(client, path, body)
    assert response.status_code in (404, 409), response.text
    events = _events(env)
    assert len(events) == 1
    assert events[0]["step"] == step
    assert events[0]["campaign_id"] == campaign["id"]
    assert events[0]["stage_key"] == "s1" and events[0]["attempt"] == 2
    assert set(events[0]) == {"campaign_id", "stage_key", "attempt", "step", "code"}
    raw = json.dumps(
        [tuple(r) for r in env.query("SELECT * FROM events WHERE event_type=?", EVENT)]
    )
    detail = json.dumps(response.json())
    for text in ("detail", "message"):
        if isinstance(response.json().get(text), str):
            assert response.json()[text] not in raw
    assert detail  # the refusal itself is still returned


def test_a_failing_recorder_leaves_the_refusal_identical(world, monkeypatch):
    env, _, client = world
    baseline = _post(client, "/worker/leases", {"grant_id": "grant_rej"})
    env.execute("DELETE FROM events WHERE event_type=?", EVENT)

    def boom(*_args, **_kwargs):
        raise RuntimeError("recorder down host.example.invalid")

    monkeypatch.setattr(module, "record", boom)
    response = _post(client, "/worker/leases", {"grant_id": "grant_rej"}, key="k9")
    assert (response.status_code, response.json()) == (baseline.status_code, baseline.json())
    assert _events(env) == []
    assert "host.example.invalid" not in response.text


def test_recording_context_reraises_the_same_exception_and_unexpected_has_no_text(world):
    env, _, _ = world
    error = Conflict("Campaign is not running.")
    with (
        pytest.raises(Conflict) as raised,
        recording(env.store, env.worker, "lease", grant_id="grant_rej"),
    ):
        raise error
    assert raised.value is error
    with (
        pytest.raises(ValueError),
        recording(env.store, env.worker, "observe", grant_id="grant_rej"),
    ):
        raise ValueError("secret host.example.invalid")
    assert [e["code"] for e in _events(env)] == ["campaign_not_running", "unexpected_error"]
    assert "secret" not in json.dumps(_events(env))
