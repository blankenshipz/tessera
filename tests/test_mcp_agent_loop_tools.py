"""Agent-loop status and pause MCP tools (ADR 0089)."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from test_standing_loop import make

from tessera_agent.standing_loop import SqliteCallLedger
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.harness_binding import PERMITTED_TOOLS
from tessera_mcp.server import create_server

LOOP_TOOLS = {"tessera_agent_loop_status", "tessera_pause_agent_loop"}


def _server(tmp_path, handler=None, **flags):
    reference = tmp_path / "tok"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"),
        httpx.MockTransport(handler or (lambda request: httpx.Response(200, json={}))),
    )
    return create_server(client, **flags)


def _call(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


@pytest.fixture
def box(tmp_path):
    directory = tmp_path / "loop"
    directory.mkdir(mode=0o700)
    return directory


def test_registered_only_with_the_marker_flag(tmp_path, box):
    default = _names(_server(tmp_path))
    assert not default & LOOP_TOOLS
    with_flag = _names(_server(tmp_path, agent_loop_marker=box / "kill"))
    assert with_flag - default == LOOP_TOOLS
    assert not [
        name
        for name in LOOP_TOOLS
        if any(word in name for word in ("resume", "disengage", "remove"))
    ]
    with pytest.raises(ValueError):
        _server(tmp_path, agent_loop_ledger=box / "ledger.sqlite")


def test_harness_catalog_is_unchanged():
    assert not (LOOP_TOOLS & set(PERMITTED_TOOLS))


def test_status_clear_present_and_marker_kinds(tmp_path, box):
    marker = box / "kill"
    server = _server(tmp_path, agent_loop_marker=marker)
    out = _call(server, "tessera_agent_loop_status")
    assert out["engaged"] is False and out["reason"] == "clear"
    assert out["halted"] is None and out["consecutive_failures"] is None
    assert out["backoff_until"] is None and out["uncertain_item_ids"] == []
    assert "not provider billing" in out["boundary"] and "not proof" in out["boundary"]
    marker.write_text("x")
    out = _call(server, "tessera_agent_loop_status")
    assert out["engaged"] is True and out["reason"] == "marker_present"
    marker.unlink()
    marker.symlink_to(box / "nowhere")
    out = _call(server, "tessera_agent_loop_status")
    assert out["engaged"] is True and out["reason"] == "marker_present"


def test_status_missing_parent_is_engaged_fail_closed(tmp_path):
    server = _server(tmp_path, agent_loop_marker=tmp_path / "gone" / "kill")
    out = _call(server, "tessera_agent_loop_status")
    assert out["engaged"] is True and out["reason"] == "marker_unreadable"


def test_status_missing_or_garbage_ledger_is_null_and_not_created(tmp_path, box):
    missing = box / "ledger.sqlite"
    server = _server(tmp_path, agent_loop_marker=box / "kill", agent_loop_ledger=missing)
    out = _call(server, "tessera_agent_loop_status")
    assert out["halted"] is None and out["uncertain_item_ids"] == []
    assert not missing.exists()
    missing.write_bytes(b"not a database" * 100)
    out = _call(server, "tessera_agent_loop_status")
    assert out["halted"] is None and out["consecutive_failures"] is None


def test_status_reads_ledger_read_only(tmp_path, box):
    path = box / "ledger.sqlite"
    ledger = SqliteCallLedger(path)
    ledger.loop_state_set("halted", "1")
    ledger.loop_state_set("consecutive_failures", "3")
    ledger.loop_state_set("backoff_until", "1234.5")
    assert ledger.begin("item-u") and ledger.begin("item-ok")
    ledger.finish("item-u", "uncertain")
    ledger.finish("item-ok", "succeeded")
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    # The ledger itself may keep a lock file beside it (single-instance ledger); the read must
    # add nothing to the directory and change nothing in the ledger.
    listing = sorted(p.name for p in box.iterdir())
    server = _server(tmp_path, agent_loop_marker=box / "kill", agent_loop_ledger=path)
    out = _call(server, "tessera_agent_loop_status")
    assert out["halted"] is True and out["consecutive_failures"] == 3
    assert out["backoff_until"] == 1234.5 and out["uncertain_item_ids"] == ["item-u"]
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before
    assert sorted(p.name for p in box.iterdir()) == listing


def test_pause_creates_private_marker_and_is_idempotent(tmp_path, box):
    marker = box / "kill"
    server = _server(tmp_path, agent_loop_marker=marker)
    assert _call(server, "tessera_pause_agent_loop", {"reason": "r" * 500}) == {
        "engaged": True,
        "already": False,
        "recorded": {},
    }
    assert marker.stat().st_mode & 0o777 == 0o600
    body = json.loads(marker.read_text())
    assert body == {"paused_by": "mcp", "reason": "r" * 200}
    before = (marker.read_bytes(), os.stat(marker).st_mtime_ns)
    assert _call(server, "tessera_pause_agent_loop", {"reason": "other"}) == {
        "engaged": True,
        "already": True,
        "recorded": {},
    }
    assert (marker.read_bytes(), os.stat(marker).st_mtime_ns) == before
    assert _call(server, "tessera_agent_loop_status")["reason"] == "marker_present"


def test_pause_refuses_symlink_marker_and_leaves_target(tmp_path, box):
    target = box / "target"
    target.write_text("keep")
    marker = box / "kill"
    marker.symlink_to(target)
    server = _server(tmp_path, agent_loop_marker=marker)
    assert _call(server, "tessera_pause_agent_loop", {"reason": "x"})["already"] is True
    assert target.read_text() == "keep"


def test_pause_refuses_unsafe_parents(tmp_path, box):
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    link = tmp_path / "link"
    link.symlink_to(box)
    for parent in (open_dir, link, tmp_path / "gone"):
        server = _server(tmp_path, agent_loop_marker=parent / "kill")
        with pytest.raises(ToolError, match="Refusing to pause"):
            _call(server, "tessera_pause_agent_loop", {"reason": "x"})
    assert not (open_dir / "kill").exists() and not (box / "kill").exists()


def test_paused_loop_skips_with_kill_switch(tmp_path):
    calls = []
    loop, _events, _clock, marker = make(
        tmp_path, [{"id": "a", "kind": "k"}], lambda item, **kw: calls.append(item["id"])
    )
    server = _server(tmp_path, agent_loop_marker=marker)
    _call(server, "tessera_pause_agent_loop", {"reason": "test"})
    assert loop.tick()["skipped"] == "kill_switch" and calls == []


def test_pause_is_recorded_on_the_platform_before_the_marker(tmp_path, box):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("idempotency-key")))
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"event": "agent_loop.paused", "cursor": 7})

    marker = box / "kill"
    server = _server(tmp_path, handler, agent_loop_marker=marker)
    out = _call(server, "tessera_pause_agent_loop", {"reason": "hold for review"})
    assert out["recorded"] == {"event": "agent_loop.paused", "cursor": 7}
    assert seen[0][:2] == ("POST", "/projects/proj1/agent-loop/pause")
    assert seen[0][2].startswith("loop-pause-") and seen[1] == {"reason": "hold for review"}
    assert marker.exists()  # the local fast path follows the recorded request


def test_a_pause_the_platform_refuses_creates_no_marker(tmp_path, box):
    marker = box / "kill"
    server = _server(
        tmp_path, lambda request: httpx.Response(403, json={}), agent_loop_marker=marker
    )
    with pytest.raises(Exception, match="403"):  # noqa: B017 - the SDK wraps tool errors
        _call(server, "tessera_pause_agent_loop", {"reason": "no scope"})
    assert not marker.exists()
