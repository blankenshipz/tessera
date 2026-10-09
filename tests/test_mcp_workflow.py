"""Synthetic schema/transport evidence only; not full live workflow acceptance."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

pytest.importorskip("mcp")

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.mcp_workflow import (  # noqa: E402
    AuthorityBounds,
    Consume,
    Observation,
    ResearchProposal,
    ReviewRequest,
    notebook_path,
)
from tessera_mcp.server import create_server  # noqa: E402


def bounds():
    return {
        "subject_credential_id": "agent",
        "destination": "test:destination",
        "model": "attested-model",
        "purpose": "bounded experiment",
        "actions": ["attempt:admit", "attempt:dispatch", "attempt:status", "execution:cancel"],
        "bindings": [
            {
                "variant_id": "variant",
                "expected_variant_revision": 1,
                "expected_design_revision": 1,
                "execution_grant_id": "execution",
                "expected_execution_grant_revision": 1,
                "expected_execution_epoch": 1,
                "task_id": "task",
                "expected_task_revision": 1,
                "plan_digest": "0" * 64,
                "target_digest": "1" * 64,
            }
        ],
        "budget_cents": 0,
        "max_duration_seconds": 30,
        "expires_at": "2030-01-01T00:00:00Z",
        "delegation_depth": 0,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"actions": ["approve"]},
        {"actions": ["attempt:status", "attempt:status"]},
        {"expires_at": "2030-01-01T00:00:00"},
        {"budget_cents": True},
        {"bindings": []},
        {"owner_token": "forbidden"},
    ],
)
def test_closed_bounds(changes):
    with pytest.raises(ValidationError):
        AuthorityBounds.model_validate({**bounds(), **changes})


@pytest.mark.parametrize(
    "command",
    [
        {"action": "approve"},
        {"action": "forward", "path": "/credentials"},
        {"action": "attempt:status", "attempt_id": "attempt", "human": True},
    ],
)
def test_no_human_or_arbitrary_command(command):
    with pytest.raises(ValidationError):
        Consume.model_validate(
            {
                "expected_revision": 1,
                "expected_epoch": 1,
                "model": "model",
                "command": command,
            }
        )


def test_duplicate_resource_bindings_denied():
    payload = bounds()
    payload["bindings"] *= 2
    with pytest.raises(ValidationError):
        AuthorityBounds.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"after_sequence": -1},
        {"after_event_cursor": -1},
        {"limit": 1001},
        {"max_bytes": 2097153},
        {"max_bytes": 1023},
        {"limit": True},
    ],
)
def test_notebook_cursor_bounds(changes):
    with pytest.raises(ValueError):
        notebook_path("project", **changes)


def test_notebook_cursor_path_is_closed_and_complete():
    from urllib.parse import parse_qs, urlsplit

    path = notebook_path("project", export=True, after_sequence=3, after_event_cursor=8, limit=2)
    parts = urlsplit(path)
    assert parts.path == "/projects/project/agent-notebook/export"
    assert parse_qs(parts.query) == {
        "after_sequence": ["3"],
        "after_event_cursor": ["8"],
        "limit": ["2"],
        "max_bytes": ["2097152"],
    }


def test_research_proposals_are_closed_and_not_canonical_creation():
    provenance = {"client": "test", "model": "attested", "session_id": "session"}
    for proposal in [
        {"kind": "project", "name": "proposed", "purpose": "review only"},
        {"kind": "study", "study": {"purpose": "test", "protocol": "test"}},
        {
            "kind": "experiment",
            "study_id": "study",
            "experiment": {
                "goal": "test",
                "success_criterion": "test",
                "protocol": "test",
                "variants": [
                    {
                        "kind": kind,
                        "name": kind,
                        "method_digest": "0" * 64,
                        "input_digest": "1" * 64,
                        "metadata": {},
                    }
                    for kind in ["baseline", "intervention"]
                ],
            },
        },
    ]:
        record = ResearchProposal.model_validate({"proposal": proposal, "provenance": provenance})
        assert record.proposal.kind == proposal["kind"]
    with pytest.raises(ValidationError):
        ResearchProposal.model_validate({"proposal": {"kind": "approve"}, "provenance": provenance})
    with pytest.raises(ValidationError):
        Observation.model_validate(
            {
                "attempt_id": "attempt",
                "expected_attempt_revision": 1,
                "summary": "test",
                "artifact_ids": [],
                "provenance": provenance,
                "human": True,
            }
        )
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(
            {
                "subject_type": "attempt",
                "subject_id": "attempt",
                "expected_revision": 1,
                "reason": "test",
                "accept": True,
            }
        )


@pytest.mark.parametrize(
    "command",
    [
        {
            "action": "attempt:admit",
            "variant_id": "variant",
            "expected_variant_revision": 1,
            "expected_design_revision": 1,
            "execution_grant_id": "grant",
        },
        {
            "action": "attempt:dispatch",
            "attempt_id": "attempt",
            "expected_revision": 1,
            "target_digest": "1" * 64,
            "profile_version": "development-minikube-opencode-v1",
        },
        {"action": "attempt:status", "attempt_id": "attempt"},
        {"action": "execution:cancel", "execution_grant_id": "grant", "expected_revision": 1},
    ],
)
def test_all_closed_execution_commands(command):
    payload = {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": "attested-model",
        "command": command,
    }
    assert Consume.model_validate(payload).model_dump(mode="json") == payload


def test_reconnect_reuses_exact_identity_key_and_body(tmp_path):
    token = tmp_path / "agent"
    token.write_text("synthetic-agent-only")
    token.chmod(0o600)
    config = Config("http://127.0.0.1:1234", token, "project", "test:destination")
    observed = []

    def receive(request):
        observed.append(request)
        return httpx.Response(200, json={"id": "same-consumption"})

    arguments = {
        "grant_id": "grant",
        "idempotency_key": "persisted-key",
        "payload": {
            "expected_revision": 1,
            "expected_epoch": 1,
            "model": "model",
            "command": {"action": "attempt:status", "attempt_id": "attempt"},
        },
    }
    for _ in range(2):
        server = create_server(ControlClient(config, httpx.MockTransport(receive)))
        asyncio.run(server.call_tool("tessera_consume_grant", arguments))
    assert len(observed) == 2
    assert observed[0].content == observed[1].content
    assert json.loads(observed[0].content) == arguments["payload"]
    for request in observed:
        assert request.url.path == "/agent-authority-grants/grant/consume"
        assert request.headers["idempotency-key"] == "persisted-key"
        assert request.headers["authorization"] == "Bearer synthetic-agent-only"
        assert request.headers["x-agent-destination"] == "test:destination"


def test_tool_schema_and_denial_redaction(tmp_path):
    token = tmp_path / "agent"
    token.write_text("synthetic-agent-only")
    token.chmod(0o600)
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "protected-marker"})

    server = create_server(
        ControlClient(
            Config("http://127.0.0.1:1234", token, "project", "test:destination"),
            httpx.MockTransport(receive),
        )
    )
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(tools) == 18
    assert not any("approve" in name or "issue" in name or "promote" in name for name in tools)
    command = tools["tessera_consume_grant"].inputSchema["$defs"]["Consume"]["properties"][
        "command"
    ]
    assert command["discriminator"]["propertyName"] == "action"
    assert len(command["oneOf"]) == 4
    with pytest.raises(Exception) as error:
        asyncio.run(server.call_tool("tessera_authority_status", {"grant_id": "grant"}))
    assert "protected-marker" not in str(error.value)
    assert len(calls) == 1
