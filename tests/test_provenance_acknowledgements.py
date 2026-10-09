"""Strict response normalization; synthetic records, no inference or services."""

import json

import httpx
import pytest
from test_runtime_provenance_client import acknowledge, proof, record

from tessera_agent.provenance_client import ProvenanceClient

ACTOR = {"org_id": "org1", "actor_principal_id": "agent1", "credential_id": "credential1"}


def send(mutator, *, value=None, expected_actor=ACTOR):
    requests = []

    def handler(request):
        requests.append(request)
        ack = acknowledge(request).json()
        mutator(ack)
        return httpx.Response(201, json=ack)

    with ProvenanceClient(
        "http://127.0.0.1:8765",
        "synthetic-token",
        "lab",
        expected_actor=expected_actor,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.append("project1", value or record(), proof())
    assert len(requests) == 1
    return result, requests[0]


@pytest.mark.parametrize(
    "field", ["output_manifest_id", "provider_job_uid", "study_id", "experiment_id", "context_id"]
)
@pytest.mark.parametrize("representation", ["omitted", "null"])
def test_omitted_and_null_known_optional_references_are_equivalent(field, representation):
    def omit(ack):
        assert ack["references"].get(field) is None
        if representation == "null":
            ack["references"][field] = None
        else:
            ack["references"].pop(field, None)

    result, request = send(omit)
    assert result.status == "accepted"
    # Normalizing a response never mutates/rekeys the immutable submitted body.
    assert json.loads(request.content)["record"]["references"].get(field) is None


def test_known_optional_remote_and_record_defaults_can_be_omitted():
    def omit(ack):
        for field in ("task_id", "peer_id"):
            ack["remote"].pop(field)
        for field in ("causation_id", "tool_id", "content_digest"):
            ack.pop(field)

    result, _ = send(omit)
    assert result.status == "accepted"


@pytest.mark.parametrize(
    "field", ["output_manifest_id", "provider_job_uid", "context_id", "grant_id", "task_id"]
)
def test_nonnull_or_changed_central_reference_is_not_optional_equivalence(field):
    result, _ = send(lambda ack: ack["references"].update({field: "foreign-reference"}))
    assert result.status == "unknown" and result.code == "invalid_acknowledgement"


@pytest.mark.parametrize("section", ["references", "remote"])
@pytest.mark.parametrize("value", [None, "foreign-reference"])
def test_unknown_nested_reference_is_rejected_even_when_null(section, value):
    result, _ = send(lambda ack: ack[section].update({"unknown_field": value}))
    assert result.status == "unknown" and result.code == "invalid_acknowledgement"


def test_unknown_top_level_receipt_field_is_not_ignored():
    result, _ = send(lambda ack: ack.update(untrusted_content="synthetic-sensitive-output"))
    assert result.status == "unknown" and "synthetic-sensitive-output" not in str(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_id", "wrong-record"),
        ("project_id", "foreign-project"),
        ("org_id", "foreign-org"),
        ("actor_principal_id", "wrong-agent"),
        ("credential_id", "wrong-credential"),
        ("correlation_id", "wrong-session"),
        ("kind", "agent"),
        ("outcome", "succeeded"),
        ("model_id", "wrong-model"),
        ("tool_id", "unexpected-tool"),
        ("causation_id", "unexpected-cause"),
        ("content_digest", "sha256:" + "a" * 64),
        ("error_code", "permission_denied"),
        ("grant_revision", 2),
        ("grant_epoch", 2),
        ("grant_revision", True),
        ("sequence", True),
        ("model_attribution", "unknown"),
    ],
)
def test_identity_outcome_digest_and_grant_pins_remain_strict(field, value):
    result, _ = send(lambda ack: ack.update({field: value}))
    assert result.status == "unknown" and result.code == "invalid_acknowledgement"


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "wrong-task"),
        ("message_id", "wrong-message"),
        ("peer_id", "wrong-peer"),
        ("destination_digest", "sha256:" + "b" * 64),
    ],
)
def test_remote_identity_and_destination_are_not_relaxed(field, value):
    result, _ = send(lambda ack: ack["remote"].update({field: value}))
    assert result.status == "unknown"


def test_nonnull_reference_cannot_be_omitted():
    value = record()
    value["references"]["context_id"] = "context1"
    result, _ = send(lambda ack: ack["references"].pop("context_id"), value=value)
    assert result.status == "unknown"


def test_nonnull_remote_identity_cannot_be_omitted():
    value = record()
    value["remote"] = {"message_id": "message1"}
    result, _ = send(lambda ack: ack["remote"].pop("message_id"), value=value)
    assert result.status == "unknown"


def test_normalization_cannot_hide_nonnull_record_pin_conflicting_with_proof():
    value = record()
    value["grant_epoch"] = 2
    result, _ = send(lambda ack: None, value=value)
    assert result.status == "unknown"


def test_first_ack_actor_attribution_is_pinned_if_enrollment_pins_not_supplied():
    calls = []

    def handler(request):
        calls.append(request)
        ack = acknowledge(request).json()
        if len(calls) > 1:
            ack["actor_principal_id"] = "other-agent"
        return httpx.Response(201, json=ack)

    with ProvenanceClient(
        "http://127.0.0.1:8765", "synthetic-token", "lab", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.append("project1", record(), proof()).status == "accepted"
        assert client.append("project1", record(), proof()).status == "unknown"
    assert calls[0].content == calls[1].content
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]
