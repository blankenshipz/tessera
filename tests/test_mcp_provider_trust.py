"""The provider trust MCP tool is read-only and registered only with its scope."""

from __future__ import annotations

import asyncio

import httpx
from mcp_required import require_mcp

require_mcp()

from tessera_mcp import server as mcp_server  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def _client(tmp_path, seen, actions=("provider_trust:read",)):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/external-agent/status":
            return httpx.Response(200, json={"actions": list(actions)})
        return httpx.Response(200, json={"digest": "d" * 64, "policy": {"providers": {}}})

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def test_the_tool_is_opt_in_and_read_only(tmp_path):
    seen: list = []
    client = _client(tmp_path, seen)
    off = {t.name for t in asyncio.run(create_server(client).list_tools())}
    assert "tessera_get_provider_trust" not in off
    server = create_server(client, provider_trust_tools=True)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    trust = {n for n in tools if "provider_trust" in n}
    assert trust == {"tessera_get_provider_trust"}  # no tool can change the setting
    assert tools["tessera_get_provider_trust"].annotations.readOnlyHint is True
    result = asyncio.run(server.call_tool("tessera_get_provider_trust", {}))
    result = result[1] if isinstance(result, tuple) else result
    assert result["digest"] == "d" * 64
    assert seen == [("GET", "/admin/provider-trust")]


def test_registration_follows_the_live_scope(tmp_path):
    assert mcp_server._has_provider_trust_scope(_client(tmp_path, []))
    assert not mcp_server._has_provider_trust_scope(
        _client(tmp_path, [], actions=("campaign:read",))
    )

    def broken(request):
        raise httpx.ConnectError("down")

    client = _client(tmp_path, [])
    # Any failure of the status read leaves the tool unregistered (fail closed).
    failing = ControlClient(client.config, httpx.MockTransport(broken))
    assert not mcp_server._has_provider_trust_scope(failing)
