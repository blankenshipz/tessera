"""Synthetic client/outbox and real isolated collector route checks, no inference."""

import copy
import hashlib
import json

import httpx
import pytest
from test_agent_authority import prepared  # noqa: F401
from test_agent_provenance_http_integration import collector  # noqa: F401

from tessera_agent.provenance_client import (
    DeliveryResult,
    ProvenanceClient,
    sanitize_envelope,
    stable_key,
)
from tessera_agent.remote_reasoning_runtime import main as runtime_main
from tessera_agent.remote_reasoning_store import ReasoningStore


def record():
    return {
        "record_id": "record1",
        "kind": "model",
        "outcome": "unknown",
        "correlation_id": "session1",
        "model_id": "model1",
        "references": {"grant_id": "grant1"},
        "error_code": "outcome_unknown",
    }


def proof():
    return {"grant_id": "grant1", "expected_revision": 1, "expected_epoch": 1}


def acknowledge(request):
    body = json.loads(request.content)
    ack = {
        **body["record"],
        "sequence": 1,
        "org_id": "org1",
        "project_id": request.url.path.split("/")[2],
        "actor_principal_id": "agent1",
        "credential_id": "credential1",
        "recorded_at": "2026-10-04T00:00:00+00:00",
        "model_attribution": "self_reported" if body["record"]["model_id"] else "unknown",
    }
    ack["remote"] = {
        **ack["remote"],
        "destination_digest": "sha256:"
        + hashlib.sha256(request.headers["X-Agent-Destination"].encode()).hexdigest(),
    }
    if body["grant_proof"]:
        ack["grant_revision"] = body["grant_proof"]["expected_revision"]
        ack["grant_epoch"] = body["grant_proof"]["expected_epoch"]
    return httpx.Response(201, json=ack)


def test_exact_enrolled_http_boundary_and_stable_replay():
    calls = []

    def handler(request):
        calls.append(request)
        response = acknowledge(request)
        response.headers["Set-Cookie"] = "must-not-persist=synthetic; Path=/"
        return response

    with ProvenanceClient(
        "http://127.0.0.1:8765", "synthetic-token", "lab", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.append("project1", record(), proof()).status == "accepted"
        assert client.append("project1", record(), proof()).status == "accepted"
    assert len(calls) == 2
    assert str(calls[0].url) == "http://127.0.0.1:8765/projects/project1/agent-provenance"
    assert calls[0].headers["Authorization"] == "Bearer synthetic-token"
    assert calls[0].headers["X-Agent-Destination"] == "lab"
    assert calls[0].headers["Idempotency-Key"] == stable_key("record1")
    assert calls[0].content == calls[1].content
    assert "Cookie" not in calls[0].headers and "Cookie" not in calls[1].headers
    assert "synthetic-token" not in calls[0].content.decode()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
    ],
)
def test_endpoint_is_exact_and_secret_free(url):
    with pytest.raises(ValueError):
        ProvenanceClient(url, "synthetic-token", "lab")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(prompt="synthetic-sensitive-content"),
        lambda r: r.update(tool_id="human.approve"),
        lambda r: r.update(tool_id="authority.issue"),
        lambda r: r.update(error_code="raw exception with synthetic-secret"),
        lambda r: r.update(correlation_id="https://secret.invalid"),
        lambda r: r.update(content_digest="raw sensitive text"),
        lambda r: r.update(remote={"session_id": "unsupported"}),
    ],
)
def test_strict_redaction_and_no_approval_events(mutate):
    value = record()
    mutate(value)
    with pytest.raises(ValueError) as caught:
        sanitize_envelope(value, proof())
    assert "synthetic-sensitive-content" not in str(caught.value)
    assert "synthetic-secret" not in str(caught.value)


def test_transport_uncertainty_has_no_retry_or_secret_exception():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-secret raw error")

    with ProvenanceClient(
        "http://127.0.0.1:8765", "synthetic-token", "lab", transport=httpx.MockTransport(fail)
    ) as client:
        result = client.append("project1", record(), proof())
    assert result.status == "unknown" and result.code == "transport_failure"
    assert len(calls) == 1 and "synthetic-secret" not in str(result)


@pytest.mark.parametrize("status", [302, 401, 403, 409, 422])
def test_explicit_rejection_never_claims_ack_or_follows_redirect(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://unapproved.invalid"},
            text="synthetic-secret collector denial",
        )

    with ProvenanceClient(
        "http://127.0.0.1:8765", "synthetic-token", "lab", transport=httpx.MockTransport(handler)
    ) as client:
        result = client.append("project1", record(), proof())
    assert result.status == "rejected" and result.sequence is None
    assert len(calls) == 1 and "synthetic-secret" not in str(result)


@pytest.mark.parametrize(
    "ack",
    [
        {"record_id": "record1"},
        {"record_id": "other", "sequence": 1},
        {"record_id": "record1", "sequence": True},
    ],
)
def test_invalid_or_missing_ack_stays_unknown(ack):
    with ProvenanceClient(
        "http://127.0.0.1:8765",
        "synthetic-token",
        "lab",
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json=ack)),
    ) as client:
        assert client.append("project1", record(), proof()).status == "unknown"


def test_oversized_response_is_unknown():
    with ProvenanceClient(
        "http://127.0.0.1:8765",
        "synthetic-token",
        "lab",
        transport=httpx.MockTransport(lambda _: httpx.Response(201, content=b"x" * 65537)),
    ) as client:
        assert client.append("project1", record(), proof()).code == "response_bound"


def binding():
    return {"grant_id": "grant1", "revision": 1, "epoch": 1, "model": "model1"}


def test_outbox_restart_replays_exact_body_and_key(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    store.begin("run1", "create")
    before = store.provenance_pending("run1")
    assert len(before) == 2
    store.close()
    restarted = ReasoningStore(tmp_path / "runtime")
    assert restarted.provenance_pending("run1") == before
    calls = []

    def handler(request):
        calls.append(request)
        return acknowledge(request)

    with ProvenanceClient(
        "http://127.0.0.1:8765", "synthetic-token", "lab", transport=httpx.MockTransport(handler)
    ) as client:
        assert restarted.drain_provenance("run1", client, "project1")["delivery_complete"]
        assert restarted.drain_provenance("run1", client, "project1")["delivery_complete"]
    assert len(calls) == 2
    assert calls[0].headers["Idempotency-Key"] == before[0]["key"]
    assert calls[0].content.decode() == before[0]["body"]
    restarted.close()


def test_uncertain_delivery_replay_retains_same_identity_and_pins(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    original = store.provenance_pending("run1")[0]
    with ProvenanceClient(
        "http://127.0.0.1:8765",
        "synthetic-token",
        "lab",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    ) as client:
        assert store.drain_provenance("run1", client, "project1")["pending"] == 1
    after = store.provenance_pending("run1")[0]
    assert after["delivery"] == "unknown" and after["body"] == original["body"]
    with ProvenanceClient(
        "http://127.0.0.1:8765",
        "synthetic-token",
        "lab",
        transport=httpx.MockTransport(acknowledge),
    ) as client:
        assert store.drain_provenance("run1", client, "project1")["pending"] == 0
    store.close()


def test_stale_revocation_denial_keeps_exact_rejected_record_pending(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    store.state("run1", "stopping", "grant_revoked")
    original = [item["body"] for item in store.provenance_pending("run1")]
    with ProvenanceClient(
        "http://127.0.0.1:8765",
        "synthetic-token",
        "lab",
        transport=httpx.MockTransport(lambda _: httpx.Response(409)),
    ) as client:
        assert not store.drain_provenance("run1", client, "project1")["delivery_complete"]
    assert [item["body"] for item in store.provenance_pending("run1")] == original
    assert store.provenance_pending("run1")[0]["delivery"] == "rejected"
    store.close()


def test_transition_and_outbox_commit_or_rollback_together(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    with pytest.raises(ValueError):
        store.begin("run1", "raw tool output with secret")
    assert store.effect("run1", "raw tool output with secret") is None
    assert len(store.provenance_pending("run1")) == 1
    store.begin("run1", "create")
    store.finish("run1", "create", "uncertain")
    unknown = json.loads(store.provenance_pending("run1")[-1]["body"])["record"]
    assert unknown["outcome"] == "unknown"
    assert unknown["error_code"] == "transport_failure"
    store.close()


def test_delivery_ack_requires_sequence_and_is_monotonic(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    rid = store.provenance_pending("run1")[0]["record_id"]
    with pytest.raises(ValueError):
        store.provenance_receipt(rid, DeliveryResult("accepted", "collector_acknowledged", rid))
    store.provenance_receipt(rid, DeliveryResult("accepted", "collector_acknowledged", rid, 201, 1))
    store.provenance_receipt(rid, DeliveryResult("unknown", "transport_failure", rid))
    assert store.provenance_coverage("run1")["pending"] == 0
    store.close()


def test_unknown_fence_is_not_fabricated_permission_denial(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    store.state("run1", "stopping", "invalid_authority_or_usage")
    body = json.loads(store.provenance_pending("run1")[-1]["body"])
    assert body["record"]["outcome"] == "unknown"
    assert body["record"]["error_code"] == "outcome_unknown"
    assert body["grant_proof"]["expected_epoch"] == 1
    store.close()


def test_outbox_is_bounded_per_run_and_full_queue_rolls_back_transition(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    with store.db:
        for index in range(63):
            store._record("run1", "synthetic." + str(index), "agent", "unknown")
    assert len(store.provenance_pending("run1", limit=64)) == 64
    with pytest.raises(ValueError, match="transition bound"):
        store.state("run1", "completed")
    assert store.get("run1")["state"] == "ready"
    with pytest.raises(ValueError):
        store.provenance_pending("run1", limit=65)
    store.close()


def test_real_collector_route_client_append_replay_conflict_and_stale_proof(request):
    _, api, _, agent, _, project, _, body = request.getfixturevalue("collector")

    def route(request):
        response = api.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": "application/json"},
        )

    with ProvenanceClient(
        "http://127.0.0.1:8765",
        agent["Authorization"].removeprefix("Bearer "),
        agent["X-Agent-Destination"],
        transport=httpx.MockTransport(route),
    ) as client:
        first = client.append(project, body["record"], body["grant_proof"])
        assert first.status == "accepted" and first.sequence is not None
        assert client.append(project, body["record"], body["grant_proof"]) == first
        changed = copy.deepcopy(body["record"])
        changed["outcome"] = "succeeded"
        assert client.append(project, changed, body["grant_proof"]).status_code == 409
        stale = {**body["grant_proof"], "expected_epoch": 2}
        assert client.append(project, body["record"], stale).status_code == 409


def test_installed_report_path_never_constructs_harness(tmp_path, monkeypatch, capsys):
    profile = {
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "grant1",
        "subject_credential_id": "agent1",
        "destination": "lab",
        "project_id": "project1",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai", "id": "model1"},
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "control_url": "http://127.0.0.1:8765",
        "agent_token_env": "SYNTHETIC_AGENT_TOKEN",
    }
    path = tmp_path / "profile"
    path.write_text(json.dumps(profile))
    path.chmod(0o600)
    state = tmp_path / "state"
    store = ReasoningStore(state)
    store.bind("run1", binding(), 100)
    store.close()
    monkeypatch.setenv("SYNTHETIC_AGENT_TOKEN", "synthetic-sensitive-token")

    class Collector:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def append(self, project, record, proof, key):
            return DeliveryResult("accepted", "collector_acknowledged", record["record_id"], 201, 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("report must not construct a reasoning harness")

    monkeypatch.setattr("tessera_agent.remote_reasoning_runtime.ProvenanceClient", Collector)
    monkeypatch.setattr("tessera_agent.remote_reasoning_runtime.OpenCodeV2Reasoning", forbidden)
    assert runtime_main(["report", "--profile", str(path), "--state-dir", str(state)]) == 0
    output = capsys.readouterr().out
    assert "synthetic-sensitive-token" not in output
    assert json.loads(output)["provenance"]["delivery_complete"] is True
