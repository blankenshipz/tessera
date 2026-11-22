"""The stuck-campaign MCP tools: read-only listing, acknowledge-only write, scope-gated."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_mcp.client import ControlClient, ControlError  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402

FINDING = {"id": "stuck_1", "status": "open", "code": "campaign_blocked", "occurrences": 1}


def _client(tmp_path, seen):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.raw_path.decode().split("?")[0],
                str(request.url.query, "ascii") if request.url.query else "",
                request.headers.get("Idempotency-Key"),
                request.content.decode() if request.content else "",
            )
        )
        if request.url.path.endswith("/scan"):
            return httpx.Response(
                200, json={"scanned_at": "t", "opened": [], "resolved": [], "open_count": 0}
            )
        if request.url.path.endswith("/acknowledge"):
            return httpx.Response(200, json={**FINDING, "status": "acknowledged"})
        return httpx.Response(200, json={"project_id": "proj1", "findings": [FINDING]})

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _tools(tmp_path, seen, actions=None):
    """Campaign tools registered for a credential holding ``actions`` (None: all)."""
    from mcp.server.fastmcp import FastMCP

    from tessera_mcp.server import register_campaign_tools

    server = FastMCP("t", log_level="CRITICAL")
    register_campaign_tools(
        server, _client(tmp_path, seen), None if actions is None else frozenset(actions)
    )
    return server


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def test_descriptions_and_annotations(tmp_path):
    server = _tools(tmp_path, [])
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    listing, ack = tools["tessera_list_stuck_campaigns"], tools["tessera_acknowledge_stuck"]
    assert listing.annotations.readOnlyHint is True
    text = " ".join(listing.description.split())
    assert "takes no action on any campaign" in text
    for word in ("reason code", "suggested next action", "receipt digest"):
        assert word in text
    assert ack.annotations.readOnlyHint is False and ack.annotations.destructiveHint is False
    assert "campaign is untouched" in " ".join(ack.description.split())
    # No tool in this module can pause, resume or cancel: only list and acknowledge exist.
    assert {n for n in tools if "stuck" in n} == {
        "tessera_list_stuck_campaigns",
        "tessera_acknowledge_stuck",
    }


def test_registration_follows_the_live_scopes(tmp_path):
    def names(actions):
        return {t.name for t in asyncio.run(_tools(tmp_path, [], actions).list_tools())}

    assert "tessera_list_stuck_campaigns" not in names({"snapshot:read"})
    assert "tessera_acknowledge_stuck" not in names({"campaign:read"})
    read_only = names({"campaign:read"})
    assert "tessera_list_stuck_campaigns" in read_only
    both = names({"campaign:read", "campaign:cancel"})
    assert {"tessera_list_stuck_campaigns", "tessera_acknowledge_stuck"} <= both
    assert "tessera_acknowledge_stuck" in names({"campaign:cancel"})


def test_list_passes_status_and_limit_and_makes_one_read(tmp_path):
    seen: list = []
    server = _tools(tmp_path, seen)
    result = _call(server, "tessera_list_stuck_campaigns", {"status": "all", "limit": 5})
    assert result["findings"][0]["id"] == "stuck_1"
    assert seen == [("GET", "/projects/proj1/campaign-stuck", "status=all&limit=5", None, "")]


def test_scan_true_posts_a_scan_with_a_fresh_key_before_the_list(tmp_path):
    seen: list = []
    server = _tools(tmp_path, seen)
    _call(server, "tessera_list_stuck_campaigns", {"scan": True})
    _call(server, "tessera_list_stuck_campaigns", {"scan": True})
    methods = [(m, p) for m, p, *_ in seen]
    assert (
        methods
        == [
            ("POST", "/projects/proj1/campaign-stuck/scan"),
            ("GET", "/projects/proj1/campaign-stuck"),
        ]
        * 2
    )
    keys = [k for m, p, q, k, b in seen if m == "POST"]
    assert all(keys) and keys[0] != keys[1]


def test_acknowledge_sends_the_note_and_the_idempotency_key(tmp_path):
    seen: list = []
    server = _tools(tmp_path, seen)
    result = _call(
        server,
        "tessera_acknowledge_stuck",
        {"finding_id": "stuck_1", "idempotency_key": "k1", "note": "seen, waiting on provider"},
    )
    assert result["status"] == "acknowledged"
    (call,) = seen
    assert call[:2] == ("POST", "/projects/proj1/campaign-stuck/stuck_1/acknowledge")
    assert call[3] == "k1" and '"note"' in call[4] and "waiting on provider" in call[4]


@pytest.mark.parametrize("status,limit", [("bogus", 5), ("open", 0), ("open", 201)])
def test_list_validates_status_and_limit(tmp_path, status, limit):
    seen: list = []
    client = _client(tmp_path, seen)
    with pytest.raises(ControlError):
        asyncio.run(client.list_stuck(status, limit))
    assert seen == []


def test_ids_and_notes_are_validated_before_any_request(tmp_path):
    seen: list = []
    client = _client(tmp_path, seen)
    for finding in ("", "..", "x" * 300):
        with pytest.raises(ControlError):
            asyncio.run(client.acknowledge_stuck(finding, "", "k"))
    with pytest.raises(ControlError):
        asyncio.run(client.acknowledge_stuck("stuck_1", "n" * 501, "k"))
    assert seen == []
    asyncio.run(client.acknowledge_stuck("a/b", "", "k"))  # path-quoted, never a new route
    assert seen[0][1] == "/projects/proj1/campaign-stuck/a%2Fb/acknowledge"
