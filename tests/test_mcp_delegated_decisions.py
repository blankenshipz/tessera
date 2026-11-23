"""ADR 0122 tools: exact routes, bodies, idempotency, scope gating and refusal surfacing."""

import asyncio
import json

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

READ = {
    "tessera_list_approval_delegations",
    "tessera_get_approval_delegation",
    "tessera_delegation_escalations",
}
ACT = {"tessera_delegated_approve_campaign", "tessera_delegated_gate_decision"}


def _server(tmp_path, seen, actions=None, respond=None):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                request.url.query.decode(),
                request.headers.get("idempotency-key"),
                request.content,
            )
        )
        return respond(request) if respond else httpx.Response(200, json={"ok": True})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(
        client,
        campaign_tools=True,
        campaign_actions=None if actions is None else frozenset(actions),
    )


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _tools(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_read_tools_are_read_only_and_send_no_key(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    tools = _tools(server)
    for name in READ:
        assert tools[name].annotations.readOnlyHint is True
    _call(server, "tessera_list_approval_delegations", {"status": "active", "offset": 5})
    _call(server, "tessera_get_approval_delegation", {"delegation_id": "dg_1"})
    _call(server, "tessera_delegation_escalations", {"limit": 10})
    assert [(m, p, q) for m, p, q, _, _ in seen[:1]] == [
        ("GET", "/projects/proj1/approval-delegations", "offset=5&limit=50&status=active")
    ]
    paths = [p for _, p, _, _, _ in seen]
    assert "/approval-delegations/dg_1" in paths
    assert "/approval-delegations/dg_1/decisions" in paths
    assert "/projects/proj1/delegation-escalations" in paths
    assert all(m == "GET" and key is None and not body for m, _, _, key, body in seen)


def test_acting_tools_hit_exact_routes(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    _call(
        server,
        "tessera_delegated_approve_campaign",
        {
            "campaign_id": "cmp_1",
            "delegation_id": "dg_1",
            "expected_revision": 3,
            "expected_spec_digest": "d" * 64,
            "idempotency_key": "k1",
        },
    )
    _call(
        server,
        "tessera_delegated_gate_decision",
        {
            "campaign_id": "cmp_1",
            "delegation_id": "dg_1",
            "rule": "r1",
            "expected_revision": 4,
            "idempotency_key": "k2",
        },
    )
    (m1, p1, _, k1, b1), (m2, p2, _, k2, b2) = seen
    assert (m1, p1, k1) == ("POST", "/campaigns/cmp_1/delegated-approval", "k1")
    assert json.loads(b1) == {
        "delegation_id": "dg_1",
        "expected_revision": 3,
        "expected_spec_digest": "d" * 64,
    }
    assert (m2, p2, k2) == ("POST", "/campaigns/cmp_1/delegated-gate-decision", "k2")
    assert json.loads(b2) == {"delegation_id": "dg_1", "rule": "r1", "expected_revision": 4}
    tools = _tools(server)
    for name in ACT:
        assert tools[name].annotations.readOnlyHint is False
        doc = " ".join(tools[name].description.lower().split())
        assert "outside_delegation" in doc and "never raises a cap" in doc
        assert "tessera_get_campaign" in doc and "idempotency_key" in doc


def test_no_tool_creates_revokes_or_signs_a_delegation(tmp_path):
    names = set(_tools(_server(tmp_path, [])))
    assert READ | ACT <= names
    delegation_tools = {n for n in names if "delegat" in n}
    assert delegation_tools == READ | ACT
    for name in delegation_tools:
        assert not any(w in name for w in ("create", "revoke", "sign", "edit", "update"))


def test_acting_tools_register_only_with_the_scope(tmp_path):
    def names(actions):
        return set(_tools(_server(tmp_path, [], actions))) & (READ | ACT)

    assert names(["campaign:read"]) == READ
    assert names(["campaign:decide_delegated"]) == ACT
    assert names(["campaign:decide_delegated", "campaign:read"]) == READ | ACT
    assert names(["campaign:submit", "campaign:cancel"]) == set()


@pytest.mark.parametrize("tool", sorted(ACT))
def test_outside_delegation_refusal_is_surfaced_with_reasons(tmp_path, tool):
    def respond(request):
        return httpx.Response(
            409,
            json={
                "detail": "outside",
                "outside_delegation": True,
                "reasons": ["cap_exceeded", "spec_changed"],
                "escalated_to": "owner",
            },
        )

    server = _server(tmp_path, [], respond=respond)
    args = {
        "campaign_id": "cmp_1",
        "delegation_id": "dg_1",
        "expected_revision": 1,
        "idempotency_key": "k",
        "expected_spec_digest": "d" * 64,
        "rule": "r1",
    }
    args = {
        k: v
        for k, v in args.items()
        if k in ("campaign_id", "delegation_id", "expected_revision", "idempotency_key")
        or (k == "expected_spec_digest") == (tool == "tessera_delegated_approve_campaign")
        and (k != "rule" or tool == "tessera_delegated_gate_decision")
    }
    if tool == "tessera_delegated_gate_decision":
        args["rule"] = "r1"
    with pytest.raises(ToolError) as caught:
        _call(server, tool, args)
    text = str(caught.value)
    assert "outside_delegation" in text and "cap_exceeded" in text and "spec_changed" in text
