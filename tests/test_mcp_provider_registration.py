"""The provider registration MCP tools are operator-enabled thin clients of the control API."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp_required import require_mcp

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def _client(tmp_path, seen):
    reference = tmp_path / "token"
    reference.write_text("synthetic-human-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("idempotency-key")))
        return httpx.Response(200, json={"digest": "d" * 64, "entries": [], "status": "written"})

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _names(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_the_tools_are_absent_by_default_and_listed_when_enabled(tmp_path):
    seen: list = []
    client = _client(tmp_path, seen)
    assert not {n for n in _names(create_server(client)) if "provider" in n and "trust" not in n}
    tools = _names(create_server(client, provider_registration_tools=True))
    assert tools["tessera_list_providers"].annotations.readOnlyHint is True
    assert tools["tessera_register_provider"].annotations.readOnlyHint is False
    text = " ".join(tools["tessera_register_provider"].description.split())
    assert "credential references" in text and "not change provider trust" in text
    assert "next serve start" in text


def test_the_tools_call_the_control_api_with_an_idempotency_key(tmp_path):
    seen: list = []
    server = create_server(_client(tmp_path, seen), provider_registration_tools=True)
    asyncio.run(server.call_tool("tessera_list_providers", {}))
    asyncio.run(
        server.call_tool(
            "tessera_register_provider",
            {"action": "add", "section": "self-hosted", "entry": {"id": "box"}, "dry_run": True},
        )
    )
    assert seen[0] == ("GET", "/admin/campaign-providers", None)
    method, path, key = seen[1]
    assert (method, path) == ("POST", "/admin/campaign-providers") and key


def _flat(text):
    return " ".join(text.split())


def test_the_write_tool_is_not_marked_idempotent_and_the_text_is_exact(tmp_path):
    tools = _names(create_server(_client(tmp_path, []), provider_registration_tools=True))
    annotations = tools["tessera_register_provider"].annotations
    assert annotations.idempotentHint is False and annotations.destructiveHint is False
    text = _flat(tools["tessera_register_provider"].description)
    assert "not change provider trust" in text and "credential references" in text


def test_the_post_body_carries_key_digest_dry_run_and_the_supplied_idempotency_key(tmp_path):
    bodies: list = []
    reference = tmp_path / "token"
    reference.write_text("synthetic-human-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((dict(request.headers), json.loads(request.content or b"null")))
        return httpx.Response(200, json={"status": "validated"})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    server = create_server(client, provider_registration_tools=True)
    asyncio.run(
        server.call_tool(
            "tessera_register_provider",
            {
                "action": "update",
                "section": "salad-profile",
                "key": "cpu-main",
                "entry": {"max_run_cents": 20},
                "expected_digest": "e" * 64,
                "dry_run": True,
                "idempotency_key": "my-key-1",
            },
        )
    )
    headers, body = bodies[0]
    assert headers["idempotency-key"] == "my-key-1"
    assert body == {
        "action": "update",
        "section": "salad-profile",
        "entry": {"max_run_cents": 20},
        "dry_run": True,
        "key": "cpu-main",
        "expected_digest": "e" * 64,
    }


def test_a_refusal_reason_reaches_the_model_and_other_denials_stay_opaque(tmp_path):
    reference = tmp_path / "token"
    reference.write_text("synthetic-human-token")
    reference.chmod(0o600)
    answers = iter(
        [
            httpx.Response(409, json={"status": "refused", "reason": "Unknown section 'nope'."}),
            httpx.Response(403, json={"detail": "protected content that must not be relayed"}),
        ]
    )
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"),
        httpx.MockTransport(lambda request: next(answers)),
    )
    server = create_server(client, provider_registration_tools=True)
    arguments = {"action": "add", "section": "nope", "entry": {}}
    with pytest.raises(ToolError, match="Unknown section 'nope'"):
        asyncio.run(server.call_tool("tessera_register_provider", arguments))
    with pytest.raises(ToolError) as denied:
        asyncio.run(server.call_tool("tessera_register_provider", arguments))
    assert "protected content" not in str(denied.value) and "HTTP 403" in str(denied.value)


def test_the_command_line_flag_plumbs_provider_registration_tools(tmp_path, monkeypatch):
    from tessera_mcp import server as module

    reference = tmp_path / "cli.token"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    for name, value in {
        "TESSERA_CONTROL_URL": "http://127.0.0.1:8765",
        "TESSERA_AGENT_TOKEN_FILE": str(reference),
        "TESSERA_PROJECT_ID": "proj1",
        "TESSERA_AGENT_DESTINATION": "dest",
    }.items():
        monkeypatch.setenv(name, value)
    built: list = []

    class Stub:
        def run(self, transport):
            return None

    def fake_create(client, **kwargs):
        built.append(kwargs)
        return Stub()

    monkeypatch.setattr(module, "create_server", fake_create)
    monkeypatch.setattr(module, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(module, "_live_actions", lambda client: frozenset())
    assert module.main(["--provider-registration-tools"]) == 0
    assert built == [{"campaign_tools": False, "provider_registration_tools": True}]
    built.clear()
    assert module.main([]) == 0
    assert built == [{"campaign_tools": False}]
