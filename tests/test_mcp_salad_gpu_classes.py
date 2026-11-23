"""``tessera_list_salad_gpu_classes``: opt-in under provider_trust:read, exact route, no body."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

NAME = "tessera_list_salad_gpu_classes"


def _client(tmp_path, seen):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode(), request.content))
        return httpx.Response(200, json={"schema": "tessera.salad.gpu-classes.v1", "classes": []})

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _tools(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_listed_only_with_the_provider_trust_group_and_read_only(tmp_path):
    client = _client(tmp_path, [])
    assert NAME not in _tools(create_server(client))
    assert NAME not in _tools(create_server(client, campaign_tools=True))
    tool = _tools(create_server(client, provider_trust_tools=True))[NAME]
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    text = " ".join(tool.description.split())
    assert "not_recorded" in text and "changes nothing" in text


def test_calls_the_exact_route_with_no_body_or_key(tmp_path):
    seen: list = []
    server = create_server(_client(tmp_path, seen), provider_trust_tools=True)
    asyncio.run(server.call_tool(NAME, {}))
    asyncio.run(server.call_tool(NAME, {"days": 30}))
    assert [(m, p, q) for m, p, q, _ in seen] == [
        ("GET", "/admin/campaign-providers/salad-gpu-classes", "days=14"),
        ("GET", "/admin/campaign-providers/salad-gpu-classes", "days=30"),
    ]
    assert all(not body for *_, body in seen)


def test_an_out_of_range_window_is_refused_before_any_request(tmp_path):
    seen: list = []
    server = create_server(_client(tmp_path, seen), provider_trust_tools=True)
    for days in (0, 91):
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool(NAME, {"days": days}))
    assert seen == []
