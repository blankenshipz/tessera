"""Synthetic supervisor seam tests, not live OpenCode/inference evidence."""

import hashlib
import json

import pytest
from test_remote_reasoning_runtime import Harness

from tessera_agent.a2a_reasoning_bridge import A2AReasoningBridge
from tessera_agent.remote_reasoning_runtime import RuntimeBlocked
from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_control.a2a_store import A2AStore, PeerConflict, PeerNotFound
from tessera_control.agent_provenance_models import AgentProvenanceRecord, AppendAgentProvenance


class Control:
    def __init__(self, context, grant):
        self.context, self.grant, self.calls = context, grant, []
        self.withdraw_after = None

    def call(self, method, path):
        self.calls.append((method, path))
        if path == "/agent-authority-grants/reasoning-grant":
            return self.grant
        assert path == "/agent-contexts/context-1"
        if self.withdraw_after and sum(p == path for _, p in self.calls) >= self.withdraw_after:
            raise RuntimeBlocked("endpoint_unavailable_or_invalid")
        return self.context


@pytest.fixture
def setup(tmp_path):
    peer = A2AStore(tmp_path / "peer.sqlite")
    runtime = ReasoningStore(tmp_path / "runtime")
    prompt = b"Operator-approved disclosed critic request"
    binding = {"task_id": "control-task-1", "plan_digest": "a" * 64}
    envelope = {
        "operation": "submit",
        "reference": "context-1",
        "authority": {
            "grant_id": "exchange-grant",
            "revision": 1,
            "epoch": 1,
            "binding": binding,
        },
    }
    task = peer.send("tenant", "sender", "peer-message", envelope)
    config = {
        "credential_id": "recipient",
        "destination": "lab",
        "model": "gpt-model",
        "control_url": "http://127.0.0.1:8765",
    }
    profile = {
        "run_id": "critic-run-1",
        "agent": "critic",
        "grant_id": "reasoning-grant",
        "subject_credential_id": "recipient",
        "destination": "lab",
        "revision": 1,
        "epoch": 1,
        "model": {"providerID": "openai", "id": "gpt-model"},
        "account_mode": "included-chatgpt-oauth",
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "control_url": config["control_url"],
    }
    raw = json.dumps(profile).encode()
    mapping = {
        "task_id": task["id"],
        "tenant": "tenant",
        "sender_id": "sender",
        "project_id": "project",
        "context_id": "context-1",
        "profile_file": "/private/profile.json",
        "prompt_file": "/private/prompt.txt",
        "profile_sha256": hashlib.sha256(raw).hexdigest(),
    }
    context = {
        "id": "context-1",
        "org_id": "tenant",
        "project_id": "project",
        "credential_id": "recipient",
        "destination": "lab",
        "disclosure_approved": True,
        "body": prompt.decode(),
    }
    grant = {
        "org_id": "tenant",
        "project_id": "project",
        "revision": 1,
        "epoch": 1,
        "status": "active",
        "effective_status": "active",
        "remaining_budget_cents": 20,
        "bounds": {
            "subject_credential_id": "recipient",
            "destination": "lab",
            "model": "gpt-model",
            "budget_cents": 20,
            "max_duration_seconds": 60,
            "expires_at": "1970-01-01T00:04:00+00:00",
            "bindings": [binding],
        },
    }
    control, harness = Control(context, grant), Harness()
    exchange = {"active": True}

    async def verifier(identity, fence):
        assert identity == {"tenant": "tenant", "agent_id": "sender", "project_id": "project"}
        assert fence == envelope["authority"]
        if not exchange["active"]:
            raise RuntimeBlocked("fixture_exchange_revoked")

    def build(**changes):
        args = dict(
            peer=peer,
            runtime=runtime,
            peer_config=config,
            mapping=mapping,
            profile_bytes=raw,
            prompt_bytes=prompt,
            control=control,
            harness=harness,
            exchange_verifier=verifier,
            clock=lambda: 100,
        )
        args.update(changes)
        return A2AReasoningBridge(**args)

    yield {
        "peer": peer,
        "runtime": runtime,
        "task": task,
        "build": build,
        "control": control,
        "harness": harness,
        "mapping": mapping,
        "raw": raw,
        "prompt": prompt,
        "profile": profile,
        "exchange": exchange,
    }
    runtime.close()


def test_actual_supervisor_seam_durable_completed_link_and_redacted_outbox(setup):
    setup["harness"].observation["outcome"] = "succeeded"
    bridge = setup["build"]()
    result = bridge.tick()
    assert result["state"] == "completed"
    peer = setup["peer"]
    task = peer.get("tenant", "sender", setup["task"]["id"])
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["metadata"]["reasoning_session_id"] == result["session_id"]
    assert task["artifacts"][0]["parts"][0]["data"]["content_collected"] is False
    outbox = peer.provenance_outbox("tenant", "sender", task["id"])
    assert len(outbox) == 4
    for item in outbox:
        assert item["delivery_state"] == "pending"
        AgentProvenanceRecord.model_validate(item["envelope"]["record"])
        AppendAgentProvenance.model_validate(
            {key: item["envelope"][key] for key in ("record", "grant_proof")}
        )
        assert "peer_id" not in item["envelope"]["record"]["references"]
        assert "message_id" not in item["envelope"]["record"]["references"]
    assert outbox[-1]["envelope"]["links"]["session_id"] == result["session_id"]
    assert setup["prompt"].decode() not in json.dumps(outbox)
    assert setup["harness"].calls == ["create", "prompt"]
    with pytest.raises(PeerNotFound):
        peer.provenance_outbox("other-tenant", "sender", task["id"])


def test_restart_reuses_run_session_prompt_and_immutable_profile(setup, tmp_path):
    first = setup["build"]().tick()
    reopened = ReasoningStore(tmp_path / "runtime")
    try:
        second = setup["build"](runtime=reopened).tick()
        assert first["session_id"] == second["session_id"]
        assert setup["harness"].calls == ["create", "prompt"]
        profile = {**setup["profile"], "max_tokens": 99}
        raw = json.dumps(profile).encode()
        changed = {**setup["mapping"], "profile_sha256": hashlib.sha256(raw).hexdigest()}
        with pytest.raises(PeerConflict):
            setup["build"](runtime=reopened, profile_bytes=raw, mapping=changed).tick()
    finally:
        reopened.close()


@pytest.mark.parametrize("boundary", ["create", "prompt"])
def test_uncertain_external_effect_never_replayed(setup, boundary):
    setup["harness"].fail = boundary
    bridge = setup["build"]()
    assert bridge.tick()["state"] == "stop_acknowledged"
    assert bridge.tick()["state"] == "stop_acknowledged"
    assert setup["harness"].calls.count(boundary) == 1
    assert setup["runtime"].effect("critic-run-1", boundary) == "uncertain"


def test_cancellation_interrupts_with_honest_unknown_termination(setup):
    bridge = setup["build"]()
    bridge.tick()
    setup["peer"].cancel("tenant", "sender", setup["task"]["id"])
    assert bridge.tick()["state"] == "stop_acknowledged"
    task = bridge.get_task()
    assert task["status"]["state"] == "TASK_STATE_CANCELED"
    assert task["metadata"]["termination_verified"] is False
    outcomes = {
        entry["envelope"]["record"]["outcome"]
        for entry in setup["peer"].provenance_outbox("tenant", "sender", task["id"])
    }
    assert {"canceled", "unknown"} <= outcomes
    bridge.tick()
    assert setup["harness"].calls.count("interrupt") == 1


def test_interrupt_failure_is_stop_unknown(setup):
    bridge = setup["build"]()
    bridge.tick()
    setup["harness"].fail = "interrupt"
    setup["peer"].cancel("tenant", "sender", setup["task"]["id"])
    assert bridge.tick()["state"] == "stop_unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"body": "changed unapproved prompt"},
        {"disclosure_approved": False},
        {"credential_id": "other"},
        {"destination": "other"},
        {"project_id": "other"},
        {"org_id": "other"},
    ],
)
def test_context_fences_before_any_effect(setup, change):
    setup["control"].context.update(change)
    bridge = setup["build"]()
    assert bridge.tick()["state"] == "stop_acknowledged"
    assert not setup["harness"].calls
    outbox = setup["peer"].provenance_outbox("tenant", "sender", setup["task"]["id"])
    assert "denied" in {entry["envelope"]["record"]["outcome"] for entry in outbox}


def test_withdrawal_between_create_and_prompt_fences_new_prompt(setup):
    setup["control"].withdraw_after = 3
    assert setup["build"]().tick()["state"] == "stop_acknowledged"
    assert setup["harness"].calls == ["create", "interrupt"]
    assert all(method == "GET" for method, _ in setup["control"].calls)


@pytest.mark.parametrize("fence", ["exchange", "revoked", "expired", "budget", "wrong-project"])
def test_authority_fences_no_new_effects(setup, fence):
    if fence == "exchange":
        setup["exchange"]["active"] = False
    elif fence == "revoked":
        setup["control"].grant["effective_status"] = "fenced"
    elif fence == "expired":
        setup["control"].grant["bounds"]["expires_at"] = "1970-01-01T00:01:00+00:00"
    elif fence == "budget":
        setup["control"].grant["remaining_budget_cents"] = 0
    else:
        setup["control"].grant["project_id"] = "other"
    assert setup["build"]().tick()["state"] == "stop_acknowledged"
    assert not setup["harness"].calls


def test_no_prompt_rebinding_or_arbitrary_context_url(setup):
    with pytest.raises(ValueError, match="prompt digest"):
        setup["build"](prompt_bytes=b"unapproved replacement")
    with pytest.raises(ValueError, match="profile digest"):
        setup["build"](profile_bytes=b"{}")
    with pytest.raises(ValueError, match="identifier"):
        setup["build"](mapping={**setup["mapping"], "context_id": "https://elsewhere/"})


def test_operator_shutdown_interrupts_without_replacement(setup):
    bridge = setup["build"]()
    bridge.tick()
    bridge.shutdown_requested = True
    assert bridge.tick()["state"] == "stop_acknowledged"
    assert setup["harness"].calls == ["create", "prompt", "interrupt"]


def test_bridge_claim_fences_caller_completion_and_canceled_effect_gate(setup):
    bridge = setup["build"]()
    bridge.tick()
    envelope = {"operation": "result", "reference": "caller-artifact", "authority": bridge.fence}
    with pytest.raises(PeerConflict):
        setup["peer"].send("tenant", "sender", "late-message", envelope, setup["task"]["id"])
    setup["peer"].cancel("tenant", "sender", setup["task"]["id"])
    with pytest.raises(PeerConflict):
        with bridge.effect_gate():
            raise AssertionError("canceled task cannot enter an external effect")
