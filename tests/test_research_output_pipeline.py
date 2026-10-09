"""Actual temporary registry/MCP SDK; model bytes/capture association are synthetic."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_research_consumer import arguments, consumer_setup, output_setup  # noqa: F401

from tessera_agent.research_consumer import ResearchConsumer
from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_output_pipeline import ResearchOutputPipeline
from tessera_agent.v2_public_output import FinalOutput


@pytest.mark.parametrize("consumer_setup", ["planner"], indirect=True)
def test_actual_sdk_continuation_registration_reuses_effect_then_waits_for_human_disclosure(
    consumer_setup,  # noqa: F811
    monkeypatch,
):
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    consumer = ResearchConsumer(**constructor)
    consumer.tick("synthetic initial prompt")
    reader = SimpleNamespace(
        read_after_prompt=lambda *a: FinalOutput(
            "public-continuation-source", 2, b"Synthetic continuation output; not a model receipt."
        )
    )
    pipeline = ResearchOutputPipeline(continuation_reader=reader, experiment_reader=None)
    kwargs = dict(phase="critic_response", source_sequence=2, prompt_message_id="msg_continuation")
    with pytest.raises(ConsumerBlocked, match="continuation_author_disclosure_required"):
        pipeline.register_and_capture(consumer, **kwargs, author_disclosure_id=None)
    output_id = consumer.store.effect_result(consumer.run_id, "register_critic_response")
    assert output_id and output_id != consumer.receipt()["output_id"]
    disclosure = http.post(
        f"/generated-outputs/{output_id}/disclosures",
        headers=owner | {"Idempotency-Key": "synthetic-human-continuation-disclosure"},
        json={
            "credential_id": consumer.binding["credential_id"],
            "destination": "openai:personal",
            "disclosure_approved": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert disclosure.status_code == 201
    captures = []
    # Capture-policy/installed-producer adaptation is deliberately not claimed by
    # this registry test. The separate normalized capture tests cover its journal.
    monkeypatch.setattr(consumer, "capture", lambda *a, **k: captures.append(k))
    reference = pipeline.register_and_capture(
        consumer, **kwargs, author_disclosure_id=disclosure.json()["id"]
    )
    assert reference["output_id"] == output_id and reference["source_sequence"] == 2
    assert len(mcp.registrations) == 2 and len(prompts) == 1
    assert captures[0]["registered_ref"]["output_id"] == output_id


@pytest.mark.parametrize("consumer_setup", ["planner"], indirect=True)
def test_historical_final_output_cannot_be_reregistered_as_continuation(consumer_setup):  # noqa: F811
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    consumer = ResearchConsumer(**constructor)
    consumer.tick("synthetic initial prompt")
    reader = SimpleNamespace(
        read_after_prompt=lambda *a: FinalOutput(
            "public-final-source", 2, b"Synthetic initial output"
        )
    )
    pipeline = ResearchOutputPipeline(continuation_reader=reader, experiment_reader=None)
    with pytest.raises(ConsumerBlocked, match="continuation_output_not_fresh"):
        pipeline.register_and_capture(
            consumer,
            phase="critic_response",
            source_sequence=2,
            prompt_message_id="msg_continuation",
            author_disclosure_id="unused",
        )
    assert len(mcp.registrations) == 1
