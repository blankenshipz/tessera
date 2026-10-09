"""Synthetic authenticated-record/MCP responses; not live approval/inference evidence."""

from types import SimpleNamespace

import pytest

from tessera_agent.research_approvals import MCPJourneyApprovals
from tessera_agent.research_consumer_store import ConsumerBlocked, ResearchConsumerStore


def test_model_or_agent_review_is_not_human_approval(tmp_path):
    store = ResearchConsumerStore(tmp_path / "state")
    store.bind_journey("journey", {})
    store.reference("journey", "proposal_id", "proposal")
    planner = SimpleNamespace(
        binding={"project_id": "project", "org_id": "org"},
        mcp=SimpleNamespace(call=lambda *a: {"id": "proposal"}),
    )
    source = SimpleNamespace(
        read_current_references=lambda *a: {},
        read_human_review=lambda *a: {
            "id": "review",
            "subject_id": "proposal",
            "project_id": "project",
            "org_id": "org",
            "issuer_role": "agent",
            "decision": "approved",
        },
    )
    poller = MCPJourneyApprovals(
        planner=planner,
        journey_store=store,
        journey_id="journey",
        record_source=source,
        experiment_reader=None,
    )
    with pytest.raises(ConsumerBlocked, match="human_review_record_invalid"):
        poller.poll("journey")
    store.close()


def test_revoked_authority_blocks_typed_dispatch_even_with_human_review(tmp_path):
    store = ResearchConsumerStore(tmp_path / "state")
    store.bind_journey("journey", {})
    command = {
        "grant_id": "grant",
        "payload": {
            "expected_revision": 1,
            "expected_epoch": 1,
            "model": "model",
            "command": {"action": "attempt:dispatch"},
        },
    }
    planner = SimpleNamespace(
        binding={"credential_id": "planner"},
        mcp=SimpleNamespace(call=lambda *a: {"id": "grant", "status": "revoked"}),
    )
    poller = MCPJourneyApprovals(
        planner=planner,
        journey_store=store,
        journey_id="journey",
        record_source=SimpleNamespace(
            read_current_references=lambda *a: {"commands": {"dispatch": command}}
        ),
        experiment_reader=None,
    )
    with pytest.raises(ConsumerBlocked, match="current_execution_authority_denied"):
        poller.poll("journey")
    store.close()
