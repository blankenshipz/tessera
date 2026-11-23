"""GET /campaigns/{id}/stage-watch: recorded watch reports, scoped, bounded, closed columns."""

from __future__ import annotations

from pathlib import Path

import pytest
from campaign_runner_harness import World, stage_spec
from fastapi.testclient import TestClient
from test_stage_watch_reports import _other_project, _watch_event

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.stage_watch_decisions import DECISION_EVENT_TYPE
from tessera_control.store import _connect

DEST = {"X-Agent-Destination": "planner.local"}


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


@pytest.fixture
def world(tmp_path: Path):
    world = World(tmp_path)
    env = world.env
    env.agent(["campaign:read", "campaign:logs"], "reader")
    env.agent(["campaign:read"], "nologs")
    env.human("human")
    spec = env.spec()
    spec["stages"] = [stage_spec(env, "s1")]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    (attempt,) = world.attempts(campaign["id"])
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute(
            "SELECT org_id FROM campaigns WHERE id=?", (campaign["id"],)
        ).fetchone()
        for seq, kind in enumerate(("heartbeat", "progress", "stuck", "failed"), start=1):
            _watch_event(
                connection,
                attempt,
                kind,
                seq,
                f"2026-01-01T00:0{seq}:00Z",
                counters={"silent_s": seq, "mem_bytes": 7, "path": "/srv/secret", "x": "y"},
                worker_text="/home/user/leak",
            )
        # Look-alikes that must not appear: another grant, another project.
        _watch_event(connection, attempt, "failed", 9, "2026-01-01T00:09:00Z", grant_id="x")
        other = _other_project(connection, row["org_id"])
        _watch_event(connection, attempt, "failed", 10, "2026-01-01T00:10:00Z", project_id=other)
        decision = {
            "grant_id": attempt["grant_id"],
            "operation_id": attempt["operation_id"],
            "kind": "resize_request",
            "reason": "oom_killed",
            "decision_seq": 1,
            "requested_memory_bytes": 2048,
            "bounds": {"within_envelope": True, "path": "/srv/secret"},
            "admission": "not_admitted",
            "observed_at": "2026-01-01T00:05:00Z",
            "note": "/home/user/leak",
        }
        grant = connection.execute(
            "SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],)
        ).fetchone()
        ExecutionStore._event(
            connection,
            {"org_id": row["org_id"], "id": grant["worker_principal_id"]},
            grant["project_id"],
            grant["task_id"],
            DECISION_EVENT_TYPE,
            1,
            decision,
        )
        connection.commit()
    finally:
        connection.close()
    return world, campaign, TestClient(create_app(world.env.state))


def _get(client, campaign_id, name="reader", query=""):
    headers = {**_token(name), **DEST}
    return client.get(f"/campaigns/{campaign_id}/stage-watch{query}", headers=headers)


def test_shape_newest_first_counts_and_no_worker_text(world):
    _w, campaign, client = world
    response = _get(client, campaign["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proof"] == "trusted_worker_assertion" and body["claims_unverified"] is True
    assert body["total"] == 5 and (body["offset"], body["limit"]) == (0, 20)
    decision, *reports = body["items"]
    assert decision == {
        "type": "decision",
        "stage_key": "s1",
        "attempt": 1,
        "kind": "resize_request",
        "reason": "oom_killed",
        "decision_seq": 1,
        "requested_memory_bytes": 2048,
        "within_envelope": True,
        "admission": "not_admitted",
        "observed_at": "2026-01-01T00:05:00Z",
    }
    assert [i["kind"] for i in reports] == ["failed", "stuck", "progress", "heartbeat"]
    assert [i["seq"] for i in reports] == [4, 3, 2, 1]
    (attempt,) = body["attempts"]
    assert attempt["decisions_recorded"] == 1
    assert attempt["counts_by_kind"] == {"heartbeat": 1, "progress": 1, "stuck": 1, "failed": 1}
    assert attempt["last_seen"] == "2026-01-01T00:05:00Z" and attempt["stage_key"] == "s1"
    item = reports[0]
    assert set(item) == {
        "type",
        "stage_key",
        "attempt",
        "kind",
        "seq",
        "observed_at",
        "counters",
        "kill_switch_engaged",
    }
    assert item["counters"] == {"mem_bytes": 7, "silent_s": 4}
    text = response.text
    for leak in ("/srv/secret", "/home/user", "worker_text", "lease_id", "grant_id"):
        assert leak not in text


def test_bounds_and_paging(world):
    _w, campaign, client = world
    page = _get(client, campaign["id"], query="?offset=1&limit=2").json()
    assert [i["seq"] for i in page["items"]] == [4, 3] and page["total"] == 5
    assert _get(client, campaign["id"], query="?offset=9").json()["items"] == []
    for bad in ("?limit=0", "?limit=101", "?offset=-1", "?limit=x"):
        assert _get(client, campaign["id"], query=bad).status_code == 422


def test_auth_scope_and_tenant(world):
    _w, campaign, client = world
    url = f"/campaigns/{campaign['id']}/stage-watch"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert _get(client, campaign["id"], "nologs").status_code == 403
    assert _get(client, campaign["id"], "human").status_code == 200
    assert _get(client, "missing").status_code == 404
    assert _get(client, "missing", "nologs").status_code in (403, 404)
