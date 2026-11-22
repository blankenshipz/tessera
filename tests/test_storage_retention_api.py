"""Retention plan/apply routes and tools: human and scoped-agent auth, digests, no paths."""

from __future__ import annotations

import asyncio
import json

import httpx
from campaign_fixture import Env
from fastapi.testclient import TestClient
from mcp_required import require_mcp
from test_storage_retention_backups import make_bundle

from tessera_control.api import create_app
from tessera_control.storage_retention_models import POLICY_FILE, RECEIPT_DIR

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def _world(tmp_path, policy=True):
    env = Env(tmp_path)
    root = tmp_path / "backups"
    root.mkdir()
    for n, age in ((1, 90), (2, 60), (3, 1)):
        make_bundle(root, n, age)
    if policy:
        (env.state / POLICY_FILE).write_text(
            json.dumps({"backup_root": str(root), "backup_max_age_days": 30})
        )
    return env, root, TestClient(create_app(env.state))


def _human(env):
    header = (env.state / "owner.header").read_text().strip().split(": ", 1)[1]
    return {"Authorization": header}


def _agent(env, actions, name):
    env.agent(actions, name)
    return {
        "Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30,
        "X-Agent-Destination": "planner.local",
    }


def _receipts(env):
    return [json.loads(p.read_text()) for p in sorted((env.state / RECEIPT_DIR).glob("*.json"))]


def test_human_plans_then_applies_by_digest(tmp_path):
    env, root, client = _world(tmp_path)
    headers = _human(env)
    assert client.post("/storage-retention/plan", json={}).status_code == 401
    plan = client.post("/storage-retention/plan", headers=headers, json={})
    assert plan.status_code == 200
    body = plan.json()
    assert set(body) == {"run_id", "plan_digest", "candidates", "skipped"}
    deletions = [c for c in body["candidates"] if c["decision"] == "delete"]
    assert len(deletions) == 2 and len(list(root.iterdir())) == 3
    assert all(
        set(c) == {"decision", "reasons", "kind", "ref", "path", "size_bytes", "sha256"}
        for c in body["candidates"]
    )
    assert str(tmp_path) not in plan.text
    assert _receipts(env)[-1]["trigger"] == "manual"
    done = client.post(
        "/storage-retention/apply", headers=headers, json={"plan_digest": body["plan_digest"]}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "complete" and len(done.json()["deleted"]) == 2
    assert str(tmp_path) not in done.text
    assert sorted(p.name for p in root.iterdir()) == ["backup-3"]
    assert client.get("/storage-retention/status", headers=headers).status_code == 200


def test_an_agent_without_the_scope_is_refused(tmp_path):
    env, root, client = _world(tmp_path)
    headers = _agent(env, ["campaign:read"], "plain")
    assert client.get("/storage-retention/status", headers=headers).status_code == 403
    assert client.post("/storage-retention/plan", headers=headers, json={}).status_code == 403
    digest = {"plan_digest": "0" * 64}
    assert client.post("/storage-retention/apply", headers=headers, json=digest).status_code == 403
    assert len(list(root.iterdir())) == 3 and not (env.state / RECEIPT_DIR).exists()


def test_read_scope_plans_but_cannot_apply_and_apply_scope_applies(tmp_path):
    env, root, client = _world(tmp_path)
    reader = _agent(env, ["retention:read"], "reader")
    assert client.get("/storage-retention/status", headers=reader).status_code == 200
    plan = client.post("/storage-retention/plan", headers=reader, json={})
    assert plan.status_code == 200 and _receipts(env)[-1]["trigger"] == "agent"
    digest = {"plan_digest": plan.json()["plan_digest"]}
    assert client.post("/storage-retention/apply", headers=reader, json=digest).status_code == 403
    assert len(list(root.iterdir())) == 3
    applier = _agent(env, ["retention:apply"], "applier")
    assert client.post("/storage-retention/plan", headers=applier, json={}).status_code == 403
    done = client.post("/storage-retention/apply", headers=applier, json=digest)
    assert done.status_code == 200 and done.json()["status"] == "complete"
    assert _receipts(env)[-1]["trigger"] == "agent"
    assert sorted(p.name for p in root.iterdir()) == ["backup-3"]
    assert str(tmp_path) not in done.text


def test_agent_cannot_widen_the_plan_with_a_body(tmp_path):
    env, _root, client = _world(tmp_path)
    reader = _agent(env, ["retention:read"], "reader")
    body = {"backup_root": "/etc"}
    assert client.post("/storage-retention/plan", headers=reader, json=body).status_code == 422


def test_stale_digest_is_409_with_a_refused_receipt(tmp_path):
    env, root, client = _world(tmp_path)
    applier = _agent(env, ["retention:apply"], "applier")
    stale = client.post("/storage-retention/apply", headers=applier, json={"plan_digest": "0" * 64})
    assert stale.status_code == 409
    assert "listed" in stale.json()["reason"] and str(tmp_path) not in stale.text
    assert _receipts(env)[-1]["status"] == "refused"
    assert len(list(root.iterdir())) == 3


def test_no_policy_apply_is_409(tmp_path):
    env, _root, client = _world(tmp_path, policy=False)
    response = client.post(
        "/storage-retention/apply", headers=_human(env), json={"plan_digest": "0" * 64}
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "no retention policy is configured"


def test_a_worker_credential_is_refused(tmp_path):
    env, root, client = _world(tmp_path)
    headers = {"Authorization": "Bearer worker-token"}
    for method, path, body in (
        ("get", "/storage-retention/status", None),
        ("post", "/storage-retention/plan", {}),
        ("post", "/storage-retention/apply", {"plan_digest": "0" * 64}),
    ):
        response = getattr(client, method)(
            path, headers=headers, **({"json": body} if body else {})
        )
        assert response.status_code in {401, 403}
    assert len(list(root.iterdir())) == 3


def _tools(tmp_path, actions, seen):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path == "/external-agent/status":
            return httpx.Response(200, json={"actions": actions})
        if request.url.path == "/storage-retention/apply" and b"0000" in request.content:
            return httpx.Response(409, json={"status": "refused", "reason": "plan changed"})
        return httpx.Response(200, json={"ok": True})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return client


def test_mcp_tools_register_by_scope_and_hit_the_routes(tmp_path):
    seen: list = []
    none = _tools(tmp_path, [], seen)
    names = {t.name for t in asyncio.run(create_server(none).list_tools())}
    assert not {n for n in names if "retention" in n}
    reader = create_server(none, retention_actions=frozenset({"retention:read"}))
    names = {t.name for t in asyncio.run(reader.list_tools())}
    assert {n for n in names if "retention" in n} == {
        "tessera_storage_retention_status",
        "tessera_storage_retention_plan",
    }
    full = create_server(none, retention_actions=frozenset({"retention:read", "retention:apply"}))
    tools = {t.name: t for t in asyncio.run(full.list_tools())}
    apply = tools["tessera_storage_retention_apply"]
    assert apply.annotations.destructiveHint is True
    assert "operator's policy" in apply.description
    asyncio.run(full.call_tool("tessera_storage_retention_plan", {}))
    asyncio.run(full.call_tool("tessera_storage_retention_apply", {"plan_digest": "a" * 64}))
    assert [(m, p) for m, p, _ in seen] == [
        ("POST", "/storage-retention/plan"),
        ("POST", "/storage-retention/apply"),
    ]
    assert json.loads(seen[1][2]) == {"plan_digest": "a" * 64}
    try:
        asyncio.run(full.call_tool("tessera_storage_retention_apply", {"plan_digest": "0" * 64}))
    except Exception as error:  # noqa: BLE001 - the relayed refusal is the assertion
        assert "plan changed" in str(error)
    else:
        raise AssertionError("a 409 must surface as an error")
    flag = create_server(none, storage_retention_tool=True)
    names = {t.name for t in asyncio.run(flag.list_tools())}
    assert {n for n in names if "retention" in n} == {"tessera_storage_retention_status"}
