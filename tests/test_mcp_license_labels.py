"""The snapshot licence-label MCP tool: read-only, scope-gated, pass-through."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_mcp.client import ControlClient, ControlError  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402

ANSWER = {"snapshot_id": "snap1", "labels": ["cc-by"], "labelled": True}


def _client(tmp_path, seen):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.content or b""))
        return httpx.Response(200, json=ANSWER)

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _server(tmp_path, seen, actions=None):
    from mcp.server.fastmcp import FastMCP

    from tessera_mcp.server import register_campaign_tools

    server = FastMCP("t", log_level="CRITICAL")
    register_campaign_tools(
        server, _client(tmp_path, seen), None if actions is None else frozenset(actions)
    )
    return server


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def test_the_tool_is_read_only_and_says_a_person_sets_labels(tmp_path):
    tools = {t.name: t for t in asyncio.run(_server(tmp_path, []).list_tools())}
    tool = tools["tessera_get_snapshot_license_labels"]
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    text = " ".join(tool.description.split())
    for phrase in ("person sets labels", "only be added", "unlabelled", "changes nothing"):
        assert phrase in text
    # No tool of the licence family can add a label.
    assert {n for n in tools if "license" in n} == {"tessera_get_snapshot_license_labels"}


def test_it_reads_the_projects_route_and_passes_the_answer_through(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    result = asyncio.run(
        server.call_tool("tessera_get_snapshot_license_labels", {"snapshot_id": "snap1"})
    )
    result = result[1] if isinstance(result, tuple) else result
    assert result == ANSWER
    assert seen == [("GET", "/projects/proj1/snapshots/snap1/license-labels", b"")]


def test_it_follows_the_snapshot_read_scope(tmp_path):
    assert "tessera_get_snapshot_license_labels" in _names(_server(tmp_path, [], ["snapshot:read"]))
    assert "tessera_get_snapshot_license_labels" not in _names(
        _server(tmp_path, [], ["campaign:read"])
    )


def test_a_hostile_snapshot_id_never_becomes_a_different_route(tmp_path):
    seen: list = []
    client = _client(tmp_path, seen)
    for bad in ("..", "", "x" * 400):
        with pytest.raises(ControlError):
            asyncio.run(client.snapshot_license_labels(bad))
    assert seen == []
    asyncio.run(client.snapshot_license_labels("a/b?x=1"))  # one quoted segment, no new route
    assert seen[0][1] == "/projects/proj1/snapshots/a%2Fb%3Fx%3D1/license-labels"
