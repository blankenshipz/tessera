"""Read-only MCP result tools against the real app; no write authority is exposed."""

import asyncio

import httpx
import pytest
from test_agent_authority import prepared  # noqa: F401
from test_agent_results_summary import world  # noqa: F401

pytest.importorskip("mcp")

from tessera_mcp.client import ControlClient, ControlError  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.harness_binding import PERMITTED_TOOLS  # noqa: E402
from tessera_mcp.results import register_result_tools  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def _client(world, headers, tmp_path):  # noqa: F811
    http, _, project, *_ = world
    reference = tmp_path / "synthetic-agent-token"
    reference.write_text(headers["Authorization"].split("Bearer ", 1)[1])
    reference.chmod(0o600)
    return ControlClient(
        Config("http://127.0.0.1:8765", reference, project, headers["X-Agent-Destination"]),
        httpx.ASGITransport(app=http.app),
    )


def _tools(client):
    server = create_server(client)
    return {t.name: t for t in asyncio.run(server.list_tools())}, server


def test_tools_are_read_only_and_catalogued(world, tmp_path):  # noqa: F811
    tools, _ = _tools(_client(world, world[4], tmp_path))
    for name in ("tessera_experiment_results", "tessera_program_results"):
        assert name in PERMITTED_TOOLS
        assert tools[name].annotations.readOnlyHint is True
    assert callable(register_result_tools)


def test_tools_read_and_denial_hides_body(world, tmp_path):  # noqa: F811
    _, _, project, experiment, reader, _, _ = world
    _, server = _tools(_client(world, reader, tmp_path))
    result = asyncio.run(
        server.call_tool("tessera_experiment_results", {"experiment_id": experiment})
    )
    body = result[1] if isinstance(result, tuple) else result
    assert body["authority_effect"] == "none"
    assert body["sections"]["measurements"]["measurements"][0]["value"] == {"v": 3}
    with pytest.raises(Exception) as denied:
        asyncio.run(server.call_tool("tessera_experiment_results", {"experiment_id": "missing"}))
    assert "404" in str(denied.value)
    wrong = {**reader, "X-Agent-Destination": "elsewhere"}
    _, server = _tools(_client(world, wrong, tmp_path))
    with pytest.raises(Exception) as forbidden:
        asyncio.run(server.call_tool("tessera_program_results", {"study_id": "s"}))
    assert "403" in str(forbidden.value) and "detail" not in str(forbidden.value)
    assert ControlError
