"""ADR 0093 tools: paths, bodies, scope gating."""

import asyncio
import json

import httpx

from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.server import CAMPAIGN_TOOL_SCOPES, create_server

TOOLS = {
    "tessera_list_pending_submissions": "campaign:read",
    "tessera_read_submission": "campaign:read",
    "tessera_cancel_submission": "campaign:cancel",
    "tessera_submit_cleared_campaign": "campaign:submit_cleared",
}


def _server(tmp_path, seen, actions=None):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request):
        seen.append((request.method, request.url.path, request.url.query.decode(), request.content))
        return httpx.Response(200, json={"ok": True})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(client, campaign_tools=True, campaign_actions=actions)


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def test_scopes_and_gating(tmp_path):
    assert {n: CAMPAIGN_TOOL_SCOPES[n] for n in TOOLS} == TOOLS
    assert TOOLS.keys() <= _names(_server(tmp_path, []))
    read = _names(_server(tmp_path, [], frozenset({"campaign:read"})))
    assert read & TOOLS.keys() == {
        "tessera_list_pending_submissions",
        "tessera_read_submission",
    }
    cancel = _names(_server(tmp_path, [], frozenset({"campaign:cancel"})))
    assert cancel & TOOLS.keys() == {"tessera_cancel_submission"}
    submit = _names(_server(tmp_path, [], frozenset({"campaign:submit_cleared"})))
    assert submit & TOOLS.keys() == {"tessera_submit_cleared_campaign"}
    none = _names(_server(tmp_path, [], frozenset({"campaign:submit"})))
    assert none & TOOLS.keys() == set()


def test_no_create_or_approve_submission_tool(tmp_path):
    names = _names(_server(tmp_path, []))
    assert not [n for n in names if "submission" in n and ("create" in n or "record" in n)]


def test_requests(tmp_path):
    seen = []
    server = _server(tmp_path, seen)
    _call(server, "tessera_list_pending_submissions", {})
    _call(server, "tessera_read_submission", {"approval_id": "a1"})
    _call(
        server,
        "tessera_cancel_submission",
        {"approval_id": "a1", "expected_revision": 2, "reason": "r", "idempotency_key": "k1"},
    )
    _call(
        server,
        "tessera_submit_cleared_campaign",
        {"approval_id": "a1", "expected_revision": 2, "idempotency_key": "k2"},
    )
    assert seen[0][:3] == (
        "GET",
        "/projects/proj1/submission-approvals",
        "status=pending&offset=0&limit=50",
    )
    assert seen[1][:2] == ("GET", "/submission-approvals/a1")
    assert seen[2][:2] == ("POST", "/submission-approvals/a1/cancel")
    assert json.loads(seen[2][3]) == {"expected_revision": 2, "reason": "r"}
    assert seen[3][:2] == ("POST", "/submission-approvals/a1/submit")
    assert json.loads(seen[3][3]) == {"expected_revision": 2}
