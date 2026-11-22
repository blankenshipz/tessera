"""Read-only storage retention status: summary, malformed receipts, route auth, MCP client."""

from __future__ import annotations

import asyncio
import json

import httpx
from campaign_fixture import Env
from fastapi.testclient import TestClient
from mcp_required import require_mcp

from tessera_control.api import create_app
from tessera_control.storage_retention_models import POLICY_FILE, RECEIPT_DIR
from tessera_control.storage_retention_status import retention_status

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def _candidate(ref, size, decision="delete"):
    return {
        "kind": "backup",
        "ref": ref,
        "path": ref,
        "size_bytes": size,
        "sha256": "a" * 64,
        "created_at": "2026-01-01T00:00:00Z",
        "decision": decision,
        "reasons": [],
    }


def _receipt(state, stamp, run_id, mode, sizes=(100,), deleted=0, failed=0, freed=0):
    plan = {
        "generated_at": stamp,
        "policy_digest": "p" * 64,
        "plan_digest": "d" + run_id,
        "candidates": [_candidate(f"b{i}", s) for i, s in enumerate(sizes)]
        + [_candidate("kept", 999, "keep")],
        "skipped": [],
    }
    body = {
        "run_id": run_id,
        "started_at": stamp,
        "finished_at": stamp,
        "mode": mode,
        "trigger": "timer",
        "policy_digest": "p" * 64,
        "plan_digest": "d" + run_id,
        "plan": plan,
        "deleted": [{"kind": "backup", "ref": "x", "path": "/secret/path"}] * deleted,
        "failed": [{"kind": "backup", "ref": "x", "error": "boom"}] * failed,
        "bytes_freed": freed,
    }
    directory = state / RECEIPT_DIR
    directory.mkdir(exist_ok=True)
    (directory / f"{stamp}-{run_id}.json").write_text(json.dumps(body))


def test_unconfigured(tmp_path):
    status = retention_status(tmp_path)
    assert status["configured"] is False
    assert status["auto_apply"] is False
    assert status["last_run"] is None
    assert status["pending_dry_run"] is None
    assert status["unreadable_receipts"] == 0
    assert status["totals"]["receipts"] == 0


def test_configured_with_receipts(tmp_path):
    (tmp_path / POLICY_FILE).write_text(
        json.dumps({"auto_apply": True, "timer_interval_seconds": 120})
    )
    _receipt(tmp_path, "20260101T000000Z", "r1", "dry_run", sizes=(10, 20))
    _receipt(tmp_path, "20260102T000000Z", "r2", "apply", sizes=(10,), deleted=1, freed=10)
    _receipt(tmp_path, "20260103T000000Z", "r3", "dry_run", sizes=(5, 7), failed=0)
    status = retention_status(tmp_path)
    assert status["configured"] is True
    assert status["auto_apply"] is True
    assert status["timer_interval_seconds"] == 120
    assert status["last_run"]["run_id"] == "r3"
    assert status["pending_dry_run"] == {
        "plan_digest": "dr3",
        "deletions_planned": 2,
        "bytes_reclaimable": 12,
    }
    assert status["totals"] == {
        "receipts": 3,
        "apply_runs": 1,
        "dry_runs": 2,
        "deleted_count": 1,
        "bytes_freed": 10,
        "failed_count": 0,
    }
    assert "/secret/path" not in json.dumps(status)
    _receipt(tmp_path, "20260104T000000Z", "r4", "apply", deleted=2, failed=1, freed=30)
    status = retention_status(tmp_path)
    assert status["pending_dry_run"] is None
    assert status["last_run"]["deleted_count"] == 2
    assert status["last_run"]["failed_count"] == 1
    assert status["last_run"]["bytes_freed"] == 30


def test_malformed_receipts_are_skipped_and_counted(tmp_path):
    _receipt(tmp_path, "20260101T000000Z", "r1", "dry_run")
    directory = tmp_path / RECEIPT_DIR
    (directory / "20260102T000000Z-bad.json").write_text("{not json")
    (directory / "20260103T000000Z-list.json").write_text("[]")
    (directory / "20260104T000000Z-mode.json").write_text(json.dumps({"run_id": "x", "mode": "?"}))
    status = retention_status(tmp_path)
    assert status["unreadable_receipts"] == 3
    assert status["last_run"]["run_id"] == "r1"


def test_malformed_policy_reads_as_unconfigured(tmp_path):
    (tmp_path / POLICY_FILE).write_text(json.dumps({"auto_apply": "yes"}))
    assert retention_status(tmp_path)["configured"] is False


def test_route_requires_auth_and_returns_status(tmp_path):
    env = Env(tmp_path)
    _receipt(env.state, "20260101T000000Z", "r1", "dry_run", sizes=(4,))
    client = TestClient(create_app(env.state))
    assert client.get("/storage-retention/status").status_code == 401
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert client.get("/storage-retention/status", headers=bad).status_code == 401
    header = (env.state / "owner.header").read_text().strip().split(": ", 1)[1]
    response = client.get("/storage-retention/status", headers={"Authorization": header})
    assert response.status_code == 200
    assert response.json() == retention_status(env.state)
    assert response.json()["pending_dry_run"]["bytes_reclaimable"] == 4


def test_mcp_client_and_tool_hit_the_route(tmp_path):
    reference = tmp_path / "token"
    reference.write_text("synthetic-human-token")
    reference.chmod(0o600)
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"configured": False})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    names = {t.name for t in asyncio.run(create_server(client).list_tools())}
    assert "tessera_storage_retention_status" not in names
    server = create_server(client, storage_retention_tool=True)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["tessera_storage_retention_status"].annotations.readOnlyHint is True
    asyncio.run(server.call_tool("tessera_storage_retention_status", {}))
    assert seen == [("GET", "/storage-retention/status")]
