"""The credential health MCP tool is read-only and registered with the provider-trust tools."""

from __future__ import annotations

import asyncio

import httpx
from mcp_required import require_mcp

require_mcp()

from tessera_mcp import server as mcp_server  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

HEALTH = {
    "checked_at": "2026-01-01T00:00:00Z",
    "worst_state": "warning",
    "warning_count": 1,
    "expired_count": 0,
    "items": [
        {
            "provider_id": "salad",
            "profile": "gpu-main",
            "reference": "credential-ref:salad-management",
            "version": 3,
            "pinned_version": 3,
            "state": "warning",
            "seconds_remaining": 3600,
            "expires_at": 1767229200.0,
            "revoked": False,
            "reason": "expires within 24 h",
        }
    ],
}


def _client(tmp_path, seen, actions=("provider_trust:read",)):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/external-agent/status":
            return httpx.Response(200, json={"actions": list(actions)})
        return httpx.Response(200, json=HEALTH)

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _tools(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_the_tool_is_registered_with_the_provider_trust_tools_and_read_only(tmp_path):
    seen: list = []
    client = _client(tmp_path, seen)
    assert "tessera_credential_health" not in _tools(create_server(client))
    tools = _tools(create_server(client, provider_trust_tools=True))
    assert {"tessera_get_provider_trust", "tessera_credential_health"} <= set(tools)
    assert tools["tessera_credential_health"].annotations.readOnlyHint is True
    assert tools["tessera_credential_health"].annotations.destructiveHint is False
    # No write tool was added for credentials or trust; the re-enrollment tool is read-only.
    assert {n for n in tools if "credential" in n} == {
        "tessera_credential_health",
        "tessera_credential_reenroll_command",
    }
    assert tools["tessera_credential_reenroll_command"].annotations.readOnlyHint is True


def test_the_call_passes_through_to_the_health_route_only(tmp_path):
    seen: list = []
    server = create_server(_client(tmp_path, seen), provider_trust_tools=True)
    result = asyncio.run(server.call_tool("tessera_credential_health", {}))
    result = result[1] if isinstance(result, tuple) else result
    assert result["worst_state"] == "warning"
    assert result["items"][0]["reference"] == "credential-ref:salad-management"
    assert seen == [("GET", "/credential-health")]


def test_the_description_says_metadata_only_and_that_it_changes_nothing(tmp_path):
    tool = _tools(create_server(_client(tmp_path, []), provider_trust_tools=True))[
        "tessera_credential_health"
    ]
    text = " ".join(tool.description.split())
    assert "never a value" in text and "changes nothing" in text and "24 h" in text


def test_registration_follows_the_live_provider_trust_scope(tmp_path):
    assert mcp_server._has_provider_trust_scope(_client(tmp_path, []))
    assert not mcp_server._has_provider_trust_scope(
        _client(tmp_path, [], actions=("campaign:read",))
    )
