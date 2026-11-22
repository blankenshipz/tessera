"""McpToolBridge against a fake FastMCP stdio server; never the real control plane."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_agent.mcp_tool_bridge import (  # noqa: E402
    McpToolBridge,
    ToolBridgeBlocked,
    catalog_sha256,
)

FAKE_SERVER = """
import json, os, time
from mcp.server.fastmcp import FastMCP

CALLS = {calls!r}
server = FastMCP("fake")

def record(name):
    with open(CALLS, "a") as stream:
        stream.write(name + "\\n")

@server.tool()
def tessera_status() -> str:
    "Report status."
    record("tessera_status")
    return "ok"

@server.tool()
def tessera_echo(text: str) -> str:
    "Echo text."
    record("tessera_echo")
    return text

@server.tool()
def tessera_fail() -> str:
    "Always fails."
    record("tessera_fail")
    raise ValueError("boom")

@server.tool()
def tessera_env() -> str:
    "Return the child environment."
    record("tessera_env")
    return json.dumps(dict(os.environ))

@server.tool()
def tessera_slow() -> str:
    "Sleep past the bridge timeout."
    record("tessera_slow")
    time.sleep(30)
    return "late"

@server.tool()
def tessera_secret() -> str:
    "A tool the profile does not allow."
    record("tessera_secret")
    return "secret"

server.run(transport="stdio")
"""

TOKEN_VALUE = "tok-secret-value-7f3a"
PROVIDER_KEY = "sk-ant-provider-secret-91c2"


@pytest.fixture
def fake(tmp_path):
    calls = tmp_path / "calls.log"
    calls.touch()
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER.format(calls=str(calls)))
    token = tmp_path / "agent-token"
    token.write_text(TOKEN_VALUE)
    token.chmod(0o600)
    return {"script": script, "calls": calls, "token": token}


def make(fake, allowed, **kwargs):
    kwargs.setdefault("timeout", 10.0)
    return McpToolBridge(
        allowed=frozenset(allowed),
        control_url="http://127.0.0.1:9",
        project_id="project-1",
        destination="agent@test",
        token_file=fake["token"],
        server_command=[sys.executable, str(fake["script"])],
        **kwargs,
    )


def calls(fake):
    return fake["calls"].read_text().split()


def test_default_command_launches_the_tessera_mcp_server_module(tmp_path):
    bridge = McpToolBridge(
        allowed=frozenset({"tessera_status"}),
        control_url="http://127.0.0.1:9",
        project_id="p",
        destination="d",
        token_file=tmp_path / "agent-token",
        python="/opt/py/bin/python",
    )
    assert bridge.command() == ["/opt/py/bin/python", "-m", "tessera_mcp.server"]


def test_lists_only_allowed_tools_as_anthropic_definitions(fake):
    with make(fake, {"tessera_status", "tessera_echo"}) as bridge:
        tools = bridge.tools()
    assert [tool["name"] for tool in tools] == ["tessera_echo", "tessera_status"]
    for tool in tools:
        assert set(tool) == {"name", "description", "input_schema"}
    echo = tools[0]
    assert echo["description"] == "Echo text."
    assert echo["input_schema"]["properties"]["text"]["type"] == "string"
    assert echo["input_schema"]["required"] == ["text"]


def test_allowed_tool_missing_from_server_blocks_catalog_changed(fake):
    with make(fake, {"tessera_status", "tessera_vanished"}) as bridge:
        with pytest.raises(ToolBridgeBlocked) as caught:
            bridge.tools()
    assert str(caught.value) == "tool_catalog_changed"


def test_call_returns_text_and_maps_is_error(fake):
    with make(fake, {"tessera_echo", "tessera_fail"}) as bridge:
        assert bridge.call("tessera_echo", {"text": "hello"}) == ("hello", False)
        assert bridge.last_truncated is False
        text, is_error = bridge.call("tessera_fail", {})
    assert is_error is True
    assert isinstance(text, str)


def test_disallowed_call_never_reaches_the_server(fake):
    with make(fake, {"tessera_status"}) as bridge:
        assert bridge.call("tessera_secret", {}) == ("tool not permitted", True)
        assert bridge.call("tessera_unknown", {"x": 1}) == ("tool not permitted", True)
        assert bridge.call("tessera_status", {}) == ("ok", False)
    assert calls(fake) == ["tessera_status"]


def test_result_truncated_to_max_bytes_and_flagged(fake):
    with make(fake, {"tessera_echo"}, max_result_bytes=64) as bridge:
        text, is_error = bridge.call("tessera_echo", {"text": "é" * 200})
        assert bridge.last_truncated is True
        assert is_error is False
        assert len(text.encode("utf-8")) <= 64
        assert text.endswith("[truncated]")
        assert text.startswith("é")
        short, _ = bridge.call("tessera_echo", {"text": "short"})
        assert short == "short"
        assert bridge.last_truncated is False


def test_timeout_is_uncertain(fake):
    with make(fake, {"tessera_slow"}, timeout=1.0) as bridge:
        started = time.monotonic()
        with pytest.raises(ToolBridgeBlocked) as caught:
            bridge.call("tessera_slow", {})
        assert time.monotonic() - started < 10
    assert str(caught.value) == "tool_call_uncertain"


def test_dead_server_is_uncertain(fake):
    bridge = make(fake, {"tessera_status"})
    bridge.start()
    try:
        os.kill(bridge._process.pid, 9)
        deadline = time.monotonic() + 5
        while bridge._process.returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
        with pytest.raises(ToolBridgeBlocked) as caught:
            bridge.call("tessera_status", {})
        assert str(caught.value) == "tool_call_uncertain"
    finally:
        bridge.close()


def test_child_environment_holds_only_runtime_references(fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("UNRELATED_PARENT_VAR", "parent")
    with make(fake, {"tessera_env"}, ca_file="/etc/tessera/ca.pem") as bridge:
        text, is_error = bridge.call("tessera_env", {})
    assert is_error is False
    env = json.loads(text)
    # CPython may coerce a C locale by setting LC_CTYPE in its own environment (PEP 538).
    env.pop("LC_CTYPE", None)
    assert env == {
        "TESSERA_CONTROL_URL": "http://127.0.0.1:9",
        "TESSERA_AGENT_TOKEN_FILE": str(fake["token"]),
        "TESSERA_PROJECT_ID": "project-1",
        "TESSERA_AGENT_DESTINATION": "agent@test",
        "TESSERA_CONTROL_CA_FILE": "/etc/tessera/ca.pem",
        "PATH": os.environ.get("PATH") or os.defpath,
        "PYTHONNOUSERSITE": "1",
    }
    assert not any(key.startswith("ANTHROPIC_") for key in env)
    for value in env.values():
        assert TOKEN_VALUE not in value
        assert PROVIDER_KEY not in value


def test_ca_reference_absent_when_not_configured(fake):
    with make(fake, {"tessera_env"}) as bridge:
        env = json.loads(bridge.call("tessera_env", {})[0])
    assert "TESSERA_CONTROL_CA_FILE" not in env


def test_catalog_sha256_stable_and_order_independent(fake):
    with make(fake, {"tessera_status", "tessera_echo", "tessera_fail"}) as bridge:
        tools = bridge.tools()
    with make(fake, {"tessera_status", "tessera_echo", "tessera_fail"}) as bridge:
        again = bridge.tools()
    digest = catalog_sha256(tools)
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert catalog_sha256(again) == digest
    assert catalog_sha256(list(reversed(tools))) == digest
    extra = [dict(tool, annotations={"readOnlyHint": True}) for tool in tools]
    assert catalog_sha256(extra) == digest
    changed = [dict(tool) for tool in tools]
    changed[0]["description"] += " (changed)"
    assert catalog_sha256(changed) != digest
    with pytest.raises(ToolBridgeBlocked) as caught:
        catalog_sha256(tools + tools[:1])
    assert str(caught.value) == "tool_catalog_invalid"


def test_close_reaps_the_child(fake):
    bridge = make(fake, {"tessera_status"})
    bridge.start()
    pid = bridge._process.pid
    os.kill(pid, 0)
    bridge.close()
    assert bridge._process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not bridge._thread.is_alive()
    bridge.close()  # idempotent
    with pytest.raises(ToolBridgeBlocked) as caught:
        bridge.call("tessera_status", {})
    assert str(caught.value) == "tool_call_uncertain"


def test_close_reaps_a_child_stuck_in_a_tool(fake):
    bridge = make(fake, {"tessera_slow"}, timeout=0.5)
    bridge.start()
    pid = bridge._process.pid
    with pytest.raises(ToolBridgeBlocked):
        bridge.call("tessera_slow", {})
    bridge.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_unstartable_server_blocks_without_content(tmp_path):
    bridge = McpToolBridge(
        allowed=frozenset({"tessera_status"}),
        control_url="http://127.0.0.1:9",
        project_id="p",
        destination="d",
        token_file=tmp_path / "agent-token",
        server_command=[sys.executable, "-c", "import sys; sys.exit(3)"],
        startup_timeout=10.0,
    )
    with pytest.raises(ToolBridgeBlocked) as caught:
        bridge.start()
    assert str(caught.value) == "tool_server_unavailable"
    assert bridge._thread is None or not bridge._thread.is_alive()


def test_tools_before_start_is_blocked(tmp_path):
    bridge = McpToolBridge(
        allowed=frozenset({"tessera_status"}),
        control_url="http://127.0.0.1:9",
        project_id="p",
        destination="d",
        token_file=Path(tmp_path / "agent-token"),
    )
    with pytest.raises(ToolBridgeBlocked):
        bridge.tools()
    assert bridge.call("tessera_secret", {}) == ("tool not permitted", True)
