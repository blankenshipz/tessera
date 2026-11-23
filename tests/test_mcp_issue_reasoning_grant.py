"""MCP exposure of the reasoning-only grant (ADR 0155), against the real in-process control app.

``tessera_request_authority`` accepts the reasoning bounds variant (a request, no authority
effect); ``tessera_issue_reasoning_grant`` is an opt-in person-only tool that is absent by default
and refused for an agent credential by the control API itself. The mcp extra is required: a
missing one fails this module at import instead of skipping it. Synthetic credentials only.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError
from test_agent_authority import post
from test_reasoning_authority_grant import MODEL, SPEND_ACTIONS, Bare, reasoning_bounds

from tessera_mcp import server as mcp_server
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.mcp_workflow import AnyAuthorityBounds, AuthorityBounds, ReasoningBounds
from tessera_mcp.server import CAMPAIGN_TOOL_SCOPES, create_server, main

TOOL = "tessera_issue_reasoning_grant"
BOUNDS = TypeAdapter(AnyAuthorityBounds)


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {tool.name for tool in asyncio.run(server.list_tools())}


class World(Bare):
    """A project with nothing running, an agent credential and a person's session."""

    def __init__(self, tmp_path, actions=SPEND_ACTIONS):
        super().__init__(tmp_path, actions)
        self.tmp = tmp_path
        self.person_token = "synthetic-person-session-" + secrets.token_hex(16)
        post(
            self.client,
            self.owner,
            "/credentials",
            {
                "token": self.person_token,
                "kind": "human_session",
                "audience": "control",
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            },
        )

    def server(self, token, name, **flags):
        reference = self.tmp / f"{name}.token"
        reference.write_text(token)
        reference.chmod(0o600)
        client = ControlClient(
            Config(
                "http://127.0.0.1:8765",
                reference,
                self.project,
                self.agent["X-Agent-Destination"],
            ),
            httpx.ASGITransport(app=self.client.app),
        )
        return create_server(client, **flags)

    def person(self):
        return self.server(self.person_token, "person", reasoning_grant_tool=True)

    def labelled_limits(self, provider="anthropic"):
        body = {"project_id": self.project, "provider_id": provider, "per_call_cents": 5}
        response = self.client.put(
            "/model-spend/limits",
            json=body | {"daily_cents": 20},
            headers={**self.owner, "X-Tessera-Automation": "test-operator", **self.key()},
        )
        assert response.status_code == 200, response.text

    def arguments(self, **over):
        return {
            "subject_credential_id": self.credential,
            "destination": self.agent["X-Agent-Destination"],
            "provider_id": "anthropic",
            "model": MODEL,
            "budget_cents": 30,
            "expires_in_seconds": 3600,
            "purpose": "plan critique",
        } | over


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# -- registration ------------------------------------------------------------------------------


def test_the_tool_is_absent_by_default_and_registers_only_on_the_flag(world):
    assert TOOL not in _names(world.server(world.token, "agent"))
    assert TOOL not in _names(world.server(world.person_token, "plain-person"))
    assert TOOL in _names(world.person())
    assert TOOL not in CAMPAIGN_TOOL_SCOPES  # no enrollable scope ever registers it
    tool = {t.name: t for t in asyncio.run(world.person().list_tools())}[TOOL]
    assert tool.annotations.readOnlyHint is False and tool.annotations.idempotentHint is False


def test_the_flag_is_plumbed_and_makes_a_decision_only_server(tmp_path, monkeypatch):
    built: list = []

    class Stub:
        def run(self, transport):
            pass

    def fake_create(client, **kwargs):
        built.append(kwargs)
        return Stub()

    reference = tmp_path / "token"
    reference.write_text("synthetic-session-token")
    reference.chmod(0o600)
    monkeypatch.setenv("TESSERA_CONTROL_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TESSERA_AGENT_TOKEN_FILE", str(reference))
    monkeypatch.setenv("TESSERA_PROJECT_ID", "proj1")
    monkeypatch.setenv("TESSERA_AGENT_DESTINATION", "dest")
    monkeypatch.setattr(mcp_server, "create_server", fake_create)
    monkeypatch.setattr(mcp_server, "_live_actions", lambda client: pytest.fail("no scope read"))
    monkeypatch.setattr(mcp_server, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(mcp_server, "_has_model_spend_scope", lambda client: False)
    assert main(["--reasoning-grant-tool"]) == 0
    assert built[-1].get("reasoning_grant_tool") is True
    assert built[-1]["campaign_tools"] is False  # no submit, cancel or other agent tools
    monkeypatch.setattr(mcp_server, "_live_actions", lambda client: frozenset())
    assert main([]) == 0
    assert "reasoning_grant_tool" not in built[-1]


# -- issuing through the real control app ------------------------------------------------------


def test_a_person_issues_a_reasoning_grant(world):
    world.labelled_limits()
    result = _call(world.person(), TOOL, world.arguments())
    (grant,) = world.rows("SELECT * FROM agent_authority_grants")
    assert result["grant_id"] == grant["id"] and result["kind"] == "reasoning"
    assert result["remaining_budget_cents"] == 30 and result["status"] == "active"
    assert result["runtime_profile"] == {
        "grant_id": grant["id"],
        "revision": grant["revision"],
        "epoch": grant["epoch"],
        "subject_credential_id": world.credential,
        "destination": world.agent["X-Agent-Destination"],
    }
    assert world.person_token not in repr(result) and world.token not in repr(result)
    (event,) = world.events("agent_authority.issued")
    assert event["kind"] == "reasoning" and event["decision_class"] == "human_session"
    assert world.rows("SELECT * FROM execution_grants") == []
    # The agent sees the grant it was given and can name it in its runtime profile.
    assert world.read(grant["id"])["effective_status"] == "active"


def test_a_lost_answer_replays_with_the_same_key_and_expiry(world):
    world.labelled_limits()
    first = _call(world.person(), TOOL, world.arguments(idempotency_key="issue-once"))
    retry = world.arguments(idempotency_key="issue-once", expires_at=first["expires_at"])
    del retry["expires_in_seconds"]
    again = _call(world.person(), TOOL, retry)
    assert again["grant_id"] == first["grant_id"]
    assert len(world.rows("SELECT * FROM agent_authority_grants")) == 1


def test_refused_with_the_stable_code_until_spend_limits_exist(world):
    with pytest.raises(ToolError, match="model_spend_not_configured"):
        _call(world.person(), TOOL, world.arguments())
    assert world.rows("SELECT * FROM agent_authority_grants") == []


def test_an_agent_credential_cannot_issue_even_with_the_tool_registered(world):
    world.labelled_limits()
    server = world.server(world.token, "agent-with-flag", reasoning_grant_tool=True)
    with pytest.raises(ToolError, match="HTTP 403"):
        _call(server, TOOL, world.arguments())
    assert world.rows("SELECT * FROM agent_authority_grants") == []


@pytest.mark.real_owner_trust
def test_the_owner_credential_is_refused_without_an_automation_label(world):
    world.labelled_limits()
    owner_token = world.owner["Authorization"].removeprefix("Bearer ")
    server = world.server(owner_token, "owner", reasoning_grant_tool=True)
    with pytest.raises(ToolError, match="HTTP 403"):
        _call(server, TOOL, world.arguments())  # this adapter never sends a label
    assert world.rows("SELECT * FROM agent_authority_grants") == []


@pytest.mark.parametrize(
    "over",
    [
        {"expires_in_seconds": 86401},
        {"expires_in_seconds": 0},
        {"budget_cents": 0},
        {"provider_id": "*"},
        {"max_duration_seconds": 86401},
        {"expires_at": "2030-01-01T00:00:00+00:00"},  # both expiries given
        {"expires_in_seconds": None, "expires_at": "2030-01-01T00:00:00"},  # no offset
        {"expires_in_seconds": None},  # neither
    ],
)
def test_invalid_arguments_send_nothing(world, over):
    world.labelled_limits()
    before = world.rows("SELECT COUNT(*) AS n FROM events")[0]["n"]
    with pytest.raises(ToolError):
        _call(world.person(), TOOL, world.arguments(**over))
    assert world.rows("SELECT * FROM agent_authority_grants") == []
    assert world.rows("SELECT COUNT(*) AS n FROM events")[0]["n"] == before


# -- the agent's request tool: the bounds union ------------------------------------------------


def test_missing_kind_is_execution_and_reasoning_is_its_own_closed_shape():
    from test_mcp_workflow import bounds as execution_bounds

    assert isinstance(BOUNDS.validate_python(execution_bounds()), AuthorityBounds)
    dumped = BOUNDS.validate_python(execution_bounds() | {"kind": "execution"}).model_dump(
        mode="json"
    )
    assert "kind" not in dumped  # the execution request body is unchanged
    reasoning = BOUNDS.validate_python(reasoning_bounds("agent"))
    assert isinstance(reasoning, ReasoningBounds)
    assert reasoning.model_dump(mode="json")["bindings"] == []


@pytest.mark.parametrize(
    "over",
    [
        {"bindings": [{"variant_id": "v"}]},
        "execution-binding",
        {"actions": ["attempt:admit"]},
        {"actions": ["model:call", "attempt:status"]},
        {"actions": []},
        {"budget_cents": 0},
        {"delegation_depth": 1},
        {"expires_at": "2030-01-01T00:00:00"},
        {"provider_id": "*"},
        {"kind": "operator"},
    ],
)
def test_reasoning_bounds_with_execution_parts_are_rejected(over):
    from test_mcp_workflow import bounds as execution_bounds

    if over == "execution-binding":
        over = {"bindings": execution_bounds()["bindings"]}
    with pytest.raises(ValidationError):
        BOUNDS.validate_python(reasoning_bounds("agent", **over))
    # An execution body cannot be relabelled reasoning either.
    with pytest.raises(ValidationError):
        BOUNDS.validate_python(execution_bounds() | {"kind": "reasoning"})


def test_an_agent_requests_reasoning_authority_without_effect(tmp_path):
    world = World(tmp_path, actions=[*SPEND_ACTIONS, "authority:request"])
    server = world.server(world.token, "requester")
    result = _call(
        server,
        "tessera_request_authority",
        {"bounds": reasoning_bounds(world.credential), "idempotency_key": "request-1"},
    )
    assert result["authority_effect"] == "none"
    assert result["bounds"]["kind"] == "reasoning"
    assert world.rows("SELECT * FROM agent_authority_grants") == []
    assert len(world.rows("SELECT * FROM agent_authority_requests")) == 1


def test_a_reasoning_request_needs_model_spend_record(tmp_path):
    world = World(tmp_path, actions=["authority:consume", "authority:request", "campaign:read"])
    server = world.server(world.token, "requester")
    with pytest.raises(ToolError, match="HTTP 403"):
        _call(
            server,
            "tessera_request_authority",
            {"bounds": reasoning_bounds(world.credential), "idempotency_key": "request-1"},
        )
    assert world.rows("SELECT * FROM agent_authority_requests") == []


def test_the_request_tool_rejects_execution_actions_with_kind_reasoning(tmp_path):
    world = World(tmp_path, actions=[*SPEND_ACTIONS, "authority:request"])
    server = world.server(world.token, "requester")
    for over in ({"actions": ["attempt:admit"]}, {"bindings": [{"variant_id": "v"}]}):
        with pytest.raises(ToolError):
            _call(
                server,
                "tessera_request_authority",
                {"bounds": reasoning_bounds(world.credential, **over), "idempotency_key": "r"},
            )
    assert world.rows("SELECT * FROM agent_authority_requests") == []
