"""The model-spend MCP tool is read-only and registered only with its scope (ADR 0090)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_mcp import server as mcp_server  # noqa: E402
from tessera_mcp.client import ControlClient, ControlError  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server, main  # noqa: E402

BODY = {
    "project_id": "proj1",
    "day": "2026-01-01",
    "limits": [{"provider_id": "*", "per_call_cents": 5, "daily_cents": 50, "revision": 1}],
    "today": {"settled_cents": 1.25, "reserved_cents": 0, "calls": 2},
    "by_day": [{"day": "2026-01-01", "settled_cents": 1.25, "calls": 2}],
    "by_provider": {"anthropic": {"settled_cents": 1.25, "calls": 2}},
    "open_reservations": [],
}


def _client(tmp_path, seen, actions=("model_spend:read",)):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, str(request.url.query, "ascii")))
        if request.url.path == "/external-agent/status":
            return httpx.Response(200, json={"actions": list(actions)})
        return httpx.Response(200, json=BODY)

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def test_the_tool_is_opt_in_read_only_and_cannot_change_limits(tmp_path):
    seen: list = []
    client = _client(tmp_path, seen)
    assert "tessera_model_spend" not in {
        t.name for t in asyncio.run(create_server(client).list_tools())
    }
    server = create_server(client, model_spend_tools=True)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    spend = {n for n in tools if "spend" in n}
    assert spend == {"tessera_model_spend"}  # no tool sets a limit or writes a ledger row
    assert tools["tessera_model_spend"].annotations.readOnlyHint is True
    text = " ".join(tools["tessera_model_spend"].description.split())
    assert "separate from compute" in text and "set by a person" in text
    assert "cannot change them" in text
    assert _call(server, "tessera_model_spend", {})["today"]["calls"] == 2
    assert seen == [("GET", "/model-spend", "project_id=proj1&days=7")]


def test_the_project_and_window_are_passed_and_validated(tmp_path):
    seen: list = []
    server = create_server(_client(tmp_path, seen), model_spend_tools=True)
    _call(server, "tessera_model_spend", {"project_id": "other/proj", "days": 30})
    assert seen[-1] == ("GET", "/model-spend", "project_id=other%2Fproj&days=30")
    client = _client(tmp_path, [])
    for days in (0, 91, True, "7"):
        with pytest.raises(ControlError):
            asyncio.run(client.model_spend(None, days))
    with pytest.raises(ControlError):
        asyncio.run(client.model_spend("..", 7))


def test_registration_follows_the_live_scope_and_fails_closed(tmp_path):
    assert mcp_server._has_model_spend_scope(_client(tmp_path, []))
    assert not mcp_server._has_model_spend_scope(_client(tmp_path, [], actions=("campaign:read",)))

    def broken(request):
        raise httpx.ConnectError("down")

    failing = ControlClient(_client(tmp_path, []).config, httpx.MockTransport(broken))
    assert not mcp_server._has_model_spend_scope(failing)


def test_the_command_line_flag_is_plumbed(tmp_path, monkeypatch):
    built: list = []

    class Stub:
        def run(self, transport):
            pass

    def fake_create(client, **kwargs):
        built.append(kwargs)
        return Stub()

    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)
    monkeypatch.setenv("TESSERA_CONTROL_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TESSERA_AGENT_TOKEN_FILE", str(reference))
    monkeypatch.setenv("TESSERA_PROJECT_ID", "proj1")
    monkeypatch.setenv("TESSERA_AGENT_DESTINATION", "dest")
    monkeypatch.setattr(mcp_server, "create_server", fake_create)
    monkeypatch.setattr(mcp_server, "_live_actions", lambda client: frozenset())
    monkeypatch.setattr(mcp_server, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(mcp_server, "_has_model_spend_scope", lambda client: False)
    assert main(["--model-spend-tools"]) == 0
    assert built[-1].get("model_spend_tools") is True
    assert main([]) == 0
    assert "model_spend_tools" not in built[-1]
    assert main(["--no-model-spend-tools"]) == 0
    assert "model_spend_tools" not in built[-1]


def test_the_resolve_tool_is_absent_by_default_and_a_thin_person_only_client(tmp_path):
    seen: list = []
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("idempotency-key")))
        bodies.append(request.content)
        return httpx.Response(200, json={"call_id": "c1", "status": "released"})

    reference = tmp_path / "token"
    reference.write_text("synthetic-session-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    name = "tessera_resolve_model_spend_reservation"
    assert name not in {t.name for t in asyncio.run(create_server(client).list_tools())}
    server = create_server(client, model_spend_resolve_tool=True)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert name in tools and tools[name].annotations.readOnlyHint is False
    text = " ".join(tools[name].description.split())
    assert "person's session credential" in text and "refuses an agent" in text
    assert "recorded on the call and on a control event" in text
    _call(server, name, {"call_id": "c1", "outcome": "not_sent", "reason": "never sent",
                         "idempotency_key": "k-1"})  # fmt: skip
    assert seen[-1] == ("POST", "/model-spend/reservations/c1/resolution", "k-1")
    assert json.loads(bodies[-1]) == {"outcome": "not_sent", "reason": "never sent"}
    _call(server, name, {"call_id": "c1", "outcome": "spent", "actual_cents": 2.5,
                         "reason": "invoice"})  # fmt: skip
    assert json.loads(bodies[-1]) == {"outcome": "spent", "reason": "invoice", "actual_cents": 2.5}
    assert seen[-1][2].startswith("resolve-spend-")


def test_the_resolve_client_validates_before_sending(tmp_path):
    client = _client(tmp_path, [])
    bad = [
        ("c1", "spent", None, "r"),
        ("c1", "not_sent", 1, "r"),
        ("c1", "spent", -1, "r"),
        ("c1", "other", None, "r"),
        ("c1", "not_sent", None, ""),
        ("c1", "not_sent", None, "x" * 501),
        ("..", "not_sent", None, "r"),
    ]
    for args in bad:
        with pytest.raises(ControlError):
            asyncio.run(client.resolve_model_spend(*args, "key"))


def test_the_resolve_flag_is_plumbed_and_makes_a_resolve_only_server(tmp_path, monkeypatch):
    built: list = []

    class Stub:
        def run(self, transport):
            pass

    def fake_create(client, **kwargs):
        built.append(kwargs)
        return Stub()

    reference = tmp_path / "token"
    reference.write_text("synthetic-session-token")
    reference.chmod(0o600)
    monkeypatch.setenv("TESSERA_CONTROL_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TESSERA_AGENT_TOKEN_FILE", str(reference))
    monkeypatch.setenv("TESSERA_PROJECT_ID", "proj1")
    monkeypatch.setenv("TESSERA_AGENT_DESTINATION", "dest")
    monkeypatch.setattr(mcp_server, "create_server", fake_create)
    monkeypatch.setattr(mcp_server, "_live_actions", lambda client: pytest.fail("no scope read"))
    monkeypatch.setattr(mcp_server, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(mcp_server, "_has_model_spend_scope", lambda client: False)
    assert main(["--model-spend-resolve-tool"]) == 0
    assert built[-1].get("model_spend_resolve_tool") is True
    assert built[-1]["campaign_tools"] is False  # a resolve-only server exposes no campaign tools
