"""Recorded agent-loop pause requests (ADR 0089): scope, idempotency, one event, nothing else."""

from __future__ import annotations

import hashlib
import json

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers

from tessera_control.api import create_app
from tessera_control.store import canonical_digest

DEST = {"X-Agent-Destination": "planner.local"}
EVENT = "agent_loop.paused"


def _auth(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


def _agent(name):
    return {**_auth(name), **DEST}


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    env.submit()
    env.agent(["loop:pause"], "pauser")
    env.agent(["context:read", "review:read"], "reviewer")
    env.agent(["context:read"], "plain")
    env.human("person")
    client = TestClient(create_app(env.state))
    return env, client, f"/projects/{env.project['id']}/agent-loop"


def _events(env):
    return env.query("SELECT * FROM events WHERE event_type=? ORDER BY cursor", EVENT)


def _post(client, path, headers, reason="operator asked", key="k1"):
    return client.post(
        path + "/pause", headers={**headers, "Idempotency-Key": key}, json={"reason": reason}
    )


def test_agent_with_scope_records_one_event(world):
    env, client, base = world
    response = _post(client, base, _agent("pauser"))
    assert response.status_code == 201
    body = response.json()
    rows = _events(env)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert body["event"] == EVENT
    assert body["cursor"] == rows[0]["cursor"]
    assert body["principal_id"] == rows[0]["actor_principal_id"]
    assert body["recorded_at"] == rows[0]["created_at"]
    assert "does not prove" in body["boundary"]
    assert payload["project_id"] == env.project["id"]
    assert payload["principal_id"] == body["principal_id"]
    assert payload["credential_id"] == env.agent(None, "pauser")["credential_id"]
    assert payload["credential_kind"] == "agent"
    assert payload["reason"] == "operator asked"
    assert payload["reason_sha256"] == hashlib.sha256(b"operator asked").hexdigest()
    assert payload["scope"] == "local_kill_switch_request"


def test_replay_is_idempotent_and_other_body_conflicts(world):
    env, client, base = world
    first = _post(client, base, _agent("pauser"))
    again = _post(client, base, _agent("pauser"))
    assert again.status_code == first.status_code == 201
    assert again.json() == first.json()
    assert len(_events(env)) == 1
    assert _post(client, base, _agent("pauser"), reason="different").status_code == 409
    assert len(_events(env)) == 1


def test_human_allowed(world):
    env, client, base = world
    assert _post(client, base, _auth("person")).status_code == 201
    assert json.loads(_events(env)[0]["payload_json"])["credential_kind"] == "human_session"


@pytest.mark.parametrize("name", ["plain", "reviewer"])
def test_agents_without_scope_refused(world, name):
    env, client, base = world
    assert _post(client, base, _agent(name)).status_code == 403
    assert client.get(base + "/pauses", headers=_agent(name)).status_code == 403
    assert _events(env) == []


def test_worker_refused(world):
    env, client, base = world
    headers = {"Authorization": "Bearer worker-token", **DEST}
    assert _post(client, base, headers).status_code in (401, 403)
    assert _events(env) == []


def test_wrong_destination_refused(world):
    env, client, base = world
    headers = {**_auth("pauser"), "X-Agent-Destination": "elsewhere.local"}
    assert _post(client, base, headers).status_code == 403
    assert _events(env) == []


def test_other_project_and_tenant_refused(world):
    env, client, _ = world
    _, other = env.plain.create_project(
        env.owner, "other-project", canonical_digest({"name": "Other"}), {"name": "Other"}
    )
    foreign = f"/projects/{other['id']}/agent-loop"
    assert _post(client, foreign, _agent("pauser")).status_code in (403, 404)
    base = f"/projects/{env.project['id']}/agent-loop"
    outsider = _other_tenant_headers(env.state)
    assert _post(client, base, outsider).status_code == 404
    assert client.get(base + "/pauses", headers=outsider).status_code == 404
    assert _events(env) == []


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201])
def test_reason_validated(world, reason):
    env, client, base = world
    assert _post(client, base, _agent("pauser"), reason=reason).status_code == 422
    assert _events(env) == []


def test_idempotency_key_required(world):
    _, client, base = world
    response = client.post(base + "/pause", headers=_agent("pauser"), json={"reason": "r"})
    assert response.status_code in (400, 422)


def test_list_returns_pauses_newest_first_and_is_scope_gated(world):
    env, client, base = world
    _post(client, base, _agent("pauser"), reason="first", key="a")
    _post(client, base, _auth("person"), reason="second", key="b")
    listed = client.get(base + "/pauses", headers=_agent("pauser")).json()["pauses"]
    assert [p["reason"] for p in listed] == ["second", "first"]
    assert all(p["event"] == EVENT for p in listed)
    assert client.get(base + "/pauses", headers=_auth("person")).status_code == 200
    assert (
        client.get(base + "/pauses?limit=1&offset=1", headers=_agent("pauser")).json()["pauses"][0][
            "reason"
        ]
        == "first"
    )


def test_changes_nothing_else(world):
    env, client, base = world
    tables = ("campaigns", "review_jobs", "tasks", "campaign_steering")
    before = [env.query(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables]
    assert _post(client, base, _agent("pauser")).status_code == 201
    assert [env.query(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables] == before
