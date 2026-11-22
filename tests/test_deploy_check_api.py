"""The control-side deploy report: ``GET /admin/deploy-check`` and the MCP tool (ADR 0098)."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_provider_registration_api import ROUTE, World

from tessera_control import deploy_check

CHECK = "/admin/deploy-check"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _checks(report):
    return {check["id"]: check for check in report["checks"]}


def test_a_human_reads_the_control_side_report(world):
    response = world.client.get(CHECK, headers=world.human())
    assert response.status_code == 200
    report = response.json()
    assert report["schema"] == deploy_check.REPORT_SCHEMA
    assert report["scope"] == "control-plane"
    checks = _checks(report)
    assert checks["state.schema"]["status"] == "ok"  # a running control plane is migrated
    # The providers file does not exist yet: reported, never created by the read.
    assert "providers.file" in checks
    assert not world.path.exists()


def test_the_report_names_no_secret_or_path(world):
    text = json.dumps(world.client.get(CHECK, headers=world.human()).json())
    assert str(world.path.parent) not in text


def test_a_provider_trust_agent_reads_it_and_another_agent_does_not(world):
    allowed = world.agent(["provider_trust:read"])
    assert world.client.get(CHECK, headers=allowed).status_code == 200
    denied = world.agent(["campaign:read"])
    assert world.client.get(CHECK, headers=denied).status_code in (401, 403)


def test_no_credential_is_refused_and_the_report_is_read_only(world):
    assert world.client.get(CHECK).status_code in (401, 403)
    assert world.client.post(CHECK, headers=world.human(), json={}).status_code == 405
    assert (
        world.client.get(ROUTE, headers=world.human()).status_code == 200
    )  # providers read intact


def test_an_unconfigured_providers_file_is_skipped_not_failed(tmp_path):
    unconfigured = World(tmp_path, configured=False)
    report = unconfigured.client.get(CHECK, headers=unconfigured.human()).json()
    assert _checks(report)["providers.file"]["status"] == "skipped"
    assert report["ready"] is True


def test_the_mcp_tool_is_read_only_and_calls_the_route():
    from mcp.server.fastmcp import FastMCP

    from tessera_mcp.server import register_provider_registration_tools

    calls = []

    class Client:
        async def deploy_check_report(self):
            calls.append("deploy")
            return {"schema": deploy_check.REPORT_SCHEMA}

    server = FastMCP("test")
    register_provider_registration_tools(server, Client())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    tool = tools["tessera_deploy_report"]
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    asyncio.run(server.call_tool("tessera_deploy_report", {}))
    assert calls == ["deploy"]
