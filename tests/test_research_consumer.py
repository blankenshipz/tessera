"""Synthetic harness/admission; actual temporary registry + MCP SDK where noted."""

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_generated_outputs import output_setup  # noqa: F401
from test_mcp_generated_outputs import adapter
from test_mcp_harness_binding import arguments, observations  # noqa: F401

from tessera_agent.research_consumer import ResearchConsumer
from tessera_agent.research_consumer_store import ConsumerBlocked, ResearchConsumerStore
from tessera_agent.v2_public_output import FinalOutput, V2FinalOutputReader
from tessera_control.store import Store
from tessera_mcp.harness_binding import generate_binding


def test_v2_projects_final_text_without_private_bodies():
    message = {
        "id": "msg_final",
        "type": "assistant",
        "agent": "critic",
        "model": {"id": "model"},
        "time": {"created": 1, "completed": 2},
        "finish": "stop",
        "content": [
            {"type": "reasoning", "text": "PRIVATE-REASONING"},
            {"type": "tool", "input": "PRIVATE-TOOL", "output": "PRIVATE-RESULT"},
            {"type": "text", "text": "Public final output"},
        ],
    }
    calls = []
    harness = SimpleNamespace(
        profile={"agent": "critic", "model": {"id": "model"}},
        verify=lambda: calls.append("verify"),
        endpoint=SimpleNamespace(call=lambda *args: {"data": [deepcopy(message)], "cursor": {}}),
    )
    final = V2FinalOutputReader(harness).read({"session_id": "ses_original"})
    assert final.content == b"Public final output" and final.source_sequence == 1
    assert "PRIVATE" not in repr(final)
    message["content"].append({"type": "unknown", "text": "PRIVATE"})
    with pytest.raises(ConsumerBlocked, match="unsupported_v2_content_schema"):
        V2FinalOutputReader(harness).read({"session_id": "ses_original"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("finish", "tool-calls"),
        ("agent", "foreign"),
        ("model", {"id": "foreign"}),
        ("error", {"message": "private"}),
        ("visibility", "public"),
    ],
)
def test_v2_unknown_or_nonterminal_schema_fails_closed(field, value):
    message = {
        "id": "msg_final",
        "type": "assistant",
        "agent": "critic",
        "model": {},
        "time": {"completed": 1},
        "finish": "stop",
        "content": [{"type": "text", "text": "ok"}],
        field: value,
    }
    harness = SimpleNamespace(
        profile={"agent": "critic", "model": {}},
        verify=lambda: None,
        endpoint=SimpleNamespace(call=lambda *a: {"data": [message], "cursor": {}}),
    )
    with pytest.raises(ConsumerBlocked):
        V2FinalOutputReader(harness).read({"session_id": "ses_one"})


def test_store_restart_retains_effect_identity_and_unknown(tmp_path):
    store = ResearchConsumerStore(tmp_path / "private")
    store.bind("run", {"context_digest": "a" * 64})
    intent, fresh = store.begin("run", "register", "b" * 64)
    assert fresh and intent["state"] == "unknown"
    store.close()
    store = ResearchConsumerStore(tmp_path / "private")
    previous, fresh = store.begin("run", "register", "b" * 64)
    assert not fresh and previous["identity"] == intent["identity"]
    with pytest.raises(ConsumerBlocked, match="consumer_effect_changed"):
        store.begin("run", "register", "c" * 64)
    with pytest.raises(ConsumerBlocked, match="consumer_binding_changed"):
        store.bind("run", {"context_digest": "d" * 64})
    store.close()


class SdkMCP:
    def __init__(self, setup, actor, directory):
        self.server = adapter(setup, actor, directory)
        self.calls = []
        self.lose_ack = False

    def call(self, tool, args):
        self.calls.append((tool, deepcopy(args)))

        async def invoke():
            async with create_connected_server_and_client_session(self.server) as sdk:
                result = await sdk.call_tool(tool, args)
                if result.isError:
                    raise ConsumerBlocked("mcp_denied")
                return result.structuredContent

        result = asyncio.run(invoke())
        if self.lose_ack and tool == "tessera_register_generated_output":
            self.lose_ack = False
            raise OSError("synthetic lost acknowledgement")
        return result

    @property
    def registrations(self):
        return [call for call in self.calls if call[0] == "tessera_register_generated_output"]


@pytest.fixture
def consumer_setup(output_setup, arguments, tmp_path, request):  # noqa: F811
    state, http, owner, project, sender, _, _, original, body = output_setup
    actor = Store(state).authenticate(sender["Authorization"].removeprefix("Bearer "))
    args = {
        **arguments,
        "max_steps": 3,
        "role": getattr(request, "param", "critic"),
        "subject_credential_id": actor["credential_id"],
        "project_id": project,
        "destination": sender["X-Agent-Destination"],
    }
    binding = generate_binding(**args)
    observed = observations(binding, args)
    observed.pop("now")
    observed.pop("expected_binding_sha256")
    observed["principal_status"].update(
        id=actor["id"],
        credential_id=actor["credential_id"],
        project_id=project,
        destination=sender["X-Agent-Destination"],
    )
    store = ResearchConsumerStore(tmp_path / "consumer")
    runtime_binding = {
        "grant_id": "synthetic-reasoning-grant",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": actor["credential_id"],
        "destination": args["destination"],
        "model": body["pins"]["model_id"],
        "prompt_sha256": "d" * 64,
    }
    run = store.runtime.bind(body["pins"]["run_id"], runtime_binding, 1)
    store.runtime.state(run["id"], "running")
    pins = {
        **body["pins"],
        "model_id": "synthetic-oauth-model",
        "context_digest": hashlib.sha256(b"public source context").hexdigest(),
        "session_id": run["session_id"],
        "config_digest": json.loads(binding.manifest)["config_sha256"],
        "skill_digest": json.loads(binding.manifest)["skill_sha256"],
    }
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    response = http.post(
        f"/projects/{project}/generated-output-grants",
        headers=owner | {"Idempotency-Key": "consumer-human-admission"},
        json={
            "action": "generated-output:register",
            "output_kind": "planner_proposal" if args["role"] == "planner" else "critic_review",
            "credential_id": actor["credential_id"],
            "destination": args["destination"],
            "pins": pins,
            "public_output_admitted": True,
            "max_outputs": 2,
            "max_total_bytes": 1024,
            "expires_at": expiry,
        },
    )
    assert response.status_code == 201
    grant = response.json()
    capture = {
        **{k: v for k, v in pins.items() if k != "operation_id"},
        "schema_version": "tessera-public-capture-binding-v1",
        "org_id": actor["org_id"],
        "project_id": project,
        "actor_principal_id": actor["id"],
        "credential_id": actor["credential_id"],
        "allowed_public_events": ["generated_output"],
        "destination_digest": "sha256:" + hashlib.sha256(args["destination"].encode()).hexdigest(),
    }
    observed["observed_harness_instance_id"] = pins["harness_instance_id"]
    calls = []

    def tick(run_id, prompt):
        calls.append(prompt)
        store.runtime.state(run_id, "completed")
        return store.runtime.get(run_id)

    supervisor = SimpleNamespace(
        tick=tick,
        fence=lambda row: None,
        profile={"max_steps": 3, "max_duration_seconds": 120, "max_tokens": 8192},
    )
    reader = SimpleNamespace(
        read=lambda row: FinalOutput("public-final-source", 1, b"Synthetic public critic")
    )
    mcp = SdkMCP(output_setup, sender, tmp_path)
    constructor = dict(
        store=store,
        supervisor=supervisor,
        capture_binding=capture,
        harness_binding=binding,
        observation_reader=lambda: deepcopy(observed),
        final_reader=reader,
        mcp=mcp,
        output_grant_id=grant["id"],
        output_grant_revision=1,
        output_kind="planner_proposal" if args["role"] == "planner" else "critic_review",
        capture_factory=lambda metadata: None,
        collector=None,
    )
    yield constructor, calls, http, owner, grant, observed, mcp
    store.close()


def test_actual_registry_sdk_registration_and_restart_no_new_inference(consumer_setup):
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    consumer = ResearchConsumer(**constructor)
    assert consumer.tick("synthetic prompt")["stage"] == "awaiting_output_disclosure"
    restarted = ResearchConsumer(**constructor)
    assert restarted.tick() == consumer.receipt()
    assert len(prompts) == 1 and len(mcp.registrations) == 1
    assert "synthetic prompt" not in " ".join(
        str(tuple(row)) for row in constructor["store"].db.execute("SELECT * FROM research_effects")
    )


def test_lost_registry_ack_held_unknown_then_exact_key_reconciliation(consumer_setup):
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    mcp.lose_ack = True
    consumer = ResearchConsumer(**constructor)
    with pytest.raises(ConsumerBlocked, match="consumer_effect_unknown"):
        consumer.tick("synthetic prompt")
    with pytest.raises(ConsumerBlocked, match="consumer_effect_unknown"):
        ResearchConsumer(**constructor).tick()
    assert len(mcp.registrations) == 1 and len(prompts) == 1
    receipt = ResearchConsumer(**constructor).tick(reconcile_registration=True)
    assert (
        receipt["output_id"] and mcp.registrations[0] == mcp.registrations[1] and len(prompts) == 1
    )


def test_revoked_human_registration_grant_is_not_rekeyed(consumer_setup):
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    assert (
        http.post(
            f"/generated-output-grants/{grant['id']}/revoke",
            headers=owner | {"Idempotency-Key": "human-revoke"},
            json={"expected_revision": 1},
        ).status_code
        == 200
    )
    consumer = ResearchConsumer(**constructor)
    with pytest.raises(ConsumerBlocked, match="consumer_effect_unknown"):
        consumer.tick("synthetic prompt")
    assert consumer.receipt()["output_id"] is None


def test_installed_admission_drift_blocks_before_prompt_or_registry(consumer_setup):
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    observed["included_oauth_verified"] = False
    consumer = ResearchConsumer(**constructor)
    with pytest.raises(ValueError, match="included_oauth_model_admission_missing"):
        consumer.tick("synthetic prompt")
    assert not prompts and not mcp.calls


@pytest.mark.parametrize("consumer_setup", ["planner"], indirect=True)
def test_generated_or_unreviewed_proposal_cannot_dispatch(consumer_setup):
    constructor, prompts, http, owner, grant, observed, mcp = consumer_setup
    consumer = ResearchConsumer(**constructor)
    with pytest.raises(ConsumerBlocked, match="human_review_required"):
        consumer.consume_grant(
            "unapproved",
            {
                "expected_revision": 1,
                "expected_epoch": 1,
                "model": "synthetic-oauth-model",
                "command": {"action": "attempt:status", "attempt_id": "attempt1"},
            },
        )
    assert not mcp.calls and not prompts
