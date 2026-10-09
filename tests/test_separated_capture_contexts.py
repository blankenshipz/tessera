"""Actual temporary backend input/policy/output reads; synthetic public/model source."""

import base64
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_agent_authority import enroll, post
from test_generated_output_provenance import registered  # noqa: F401

from tessera_control.public_capture import CaptureBinding, PublicCaptureAdapter, canonical
from tessera_control.public_capture_contexts import resolve_separated_capture_contexts
from tessera_control.public_capture_outbox import PublicCaptureOutbox
from tessera_control.public_capture_producer import NormalizedPublicMetadataProducer
from tessera_control.store import Store, _connect


def create_context(setup, body, *, credential=None, approved=True):
    return post(
        setup["client"],
        setup["owner"],
        f"/projects/{setup['project']}/agent-contexts",
        {
            "credential_id": credential or setup["binding"]["credential_id"],
            "destination": setup["author"]["X-Agent-Destination"],
            "title": "separate immutable context",
            "body": body,
            "classification": "project",
            "disclosure_approved": approved,
        },
    )


@pytest.fixture
def separated(request):
    setup = request.getfixturevalue("registered")
    text = "Curated synthetic research question, independently approved as input."
    input_context = create_context(setup, text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    binding = {
        **setup["binding"],
        "run_id": "separate_run_1",
        "context_id": input_context["id"],
        "context_digest": digest,
        "input_context_id": input_context["id"],
        "input_body_digest": digest,
    }
    policy = {
        key: binding[key]
        for key in (
            "run_id",
            "task_id",
            "context_digest",
            "skill_digest",
            "config_digest",
            "allowed_public_events",
            "input_context_id",
            "input_body_digest",
            "model_id",
            "session_id",
            "harness_instance_id",
        )
    }
    policy["schema_version"] = "tessera-separated-public-harness-disclosure-v1"
    policy_context = create_context(setup, canonical(policy))
    binding["capture_policy_context_id"] = policy_context["id"]
    principal = Store(setup["state"]).authenticate(
        setup["author"]["Authorization"].split(" ", 1)[1]
    )
    principal["agent_destination"] = setup["author"]["X-Agent-Destination"]

    def resolver(b):
        with _connect(setup["state"] / "tessera.sqlite3") as conn:
            conn.execute("BEGIN")
            return resolve_separated_capture_contexts(conn, principal, b)

    pins = {
        key: binding[key]
        for key in (
            "model_id",
            "run_id",
            "session_id",
            "harness_instance_id",
            "context_id",
            "context_digest",
            "skill_digest",
            "config_digest",
            "task_id",
        )
    }
    pins["operation_id"] = None
    grant_body = {**setup["grant"]["bounds"], "pins": pins}
    grant = post(
        setup["client"],
        setup["owner"],
        f"/projects/{setup['project']}/generated-output-grants",
        grant_body,
    )
    content = b"Synthetic separate-input critic recommendation; review required."
    output = post(
        setup["client"],
        setup["author"],
        f"/projects/{setup['project']}/generated-outputs",
        {
            "grant_id": grant["id"],
            "expected_revision": 1,
            "pins": pins,
            "source_event_id": "separate_source_1",
            "source_sequence": 1,
            "output_kind": "critic_review",
            "visibility": "public",
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    disclosure = post(
        setup["client"],
        setup["owner"],
        f"/generated-outputs/{output['id']}/disclosures",
        {
            "credential_id": binding["credential_id"],
            "destination": setup["author"]["X-Agent-Destination"],
            "disclosure_approved": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )

    def output_resolver(ident, b):
        response = setup["client"].get(
            f"/generated-outputs/{ident}/metadata",
            params={"disclosure_id": disclosure["id"]},
            headers=setup["author"],
        )
        assert response.status_code == 200
        value = response.json()
        assert value["pins"] == pins
        return {
            "family": "generated_public_research_output_v1",
            "output_id": value["id"],
            "artifact_id": value["artifact_id"],
            "disclosure_id": disclosure["id"],
            "output_kind": value["output_kind"],
            "writer_actor_id": value["actor_id"],
            "writer_credential_id": value["credential_id"],
            "writer_org_id": value["org_id"],
            "binding": b,
            "source_event_id": value["source_event_id"],
            "source_sequence": value["source_sequence"],
            "operation_id": None,
            "sha256": value["sha256"],
        }

    adapter = PublicCaptureAdapter(binding, resolver, output_resolver)
    observation = {
        **setup["observation"],
        **pins,
        "input_context_id": binding["input_context_id"],
        "input_body_digest": digest,
        "capture_policy_context_id": policy_context["id"],
        "source_event_id": "separate_source_1",
        "registered_output_id": output["id"],
    }
    return {
        **setup,
        "binding": binding,
        "adapter": adapter,
        "resolver": resolver,
        "policy": policy,
        "input": input_context,
        "policy_context": policy_context,
        "input_body": text,
        "output": output,
        "disclosure": disclosure,
        "output_resolver": output_resolver,
        "observation": observation,
        "envelope": adapter.envelope(observation),
    }


def append(setup, envelope=None, key="separate_append"):
    return setup["client"].post(
        f"/projects/{setup['project']}/agent-provenance",
        json=envelope or setup["envelope"],
        headers={**setup["author"], "Idempotency-Key": key},
    )


def test_actual_readback_uses_input_not_policy_and_registered_output_collector(separated, tmp_path):
    setup = separated
    resolution = setup["adapter"].resolve_contexts()
    assert resolution["input_context"]["body"] == setup["input_body"]
    assert resolution["capture_policy"]["context_id"] != resolution["input_context"]["context_id"]
    assert resolution["capture_policy"]["body"] != setup["input_body"]
    assert setup["output"]["pins"]["context_id"] == setup["input"]["id"]
    assert (
        setup["output"]["pins"]["context_digest"]
        == hashlib.sha256(setup["input_body"].encode()).hexdigest()
    )
    assert "capture_policy_context_id" not in setup["output"]["pins"]
    first = append(setup)
    assert first.status_code == 201, first.text
    assert append(setup).json() == first.json()
    assert append(setup, key="another_key").json() == first.json()
    producer = NormalizedPublicMetadataProducer(setup["adapter"], lambda _: [setup["observation"]])
    observed = producer.read_public_metadata(
        {"id": setup["binding"]["run_id"], "session_id": setup["binding"]["session_id"]}
    )
    box = PublicCaptureOutbox(tmp_path / "private", setup["adapter"])
    assert box.capture(observed[0]) == first.json()["record_id"]
    wire = box.connection.execute("SELECT envelope FROM capture_events").fetchone()[0]
    assert setup["input_body"] not in wire and setup["policy_context"]["body"] not in wire
    assert "input_body_digest" in wire and "capture_policy_context_id" in wire
    box.close()
    response = setup["client"].get(
        f"/projects/{setup['project']}/agent-notebook/export", headers=setup["author"]
    )
    assert response.status_code == 200
    rows = response.json()["provenance"]
    row = next(r for r in rows if r["record_id"] == first.json()["record_id"])
    assert (
        row["harness"]["context_id"] == row["harness"]["input_context_id"] == setup["input"]["id"]
    )
    assert row["output"]["pins"]["context_id"] == setup["input"]["id"]
    assert setup["input_body"] not in canonical(response.json())


@pytest.mark.parametrize(
    "change", ["same_id", "swapped", "policy_digest", "input_digest", "partial", "foreign_policy"]
)
def test_no_policy_input_conflation_or_digest_echo(separated, change):
    setup = separated
    binding = deepcopy(setup["binding"])
    if change == "same_id":
        binding["capture_policy_context_id"] = binding["input_context_id"]
    elif change == "swapped":
        binding["input_context_id"], binding["capture_policy_context_id"] = (
            binding["capture_policy_context_id"],
            binding["input_context_id"],
        )
        binding["context_id"] = binding["input_context_id"]
        binding["input_body_digest"] = binding["context_digest"] = hashlib.sha256(
            setup["policy_context"]["body"].encode()
        ).hexdigest()
    elif change == "policy_digest":
        binding["input_body_digest"] = binding["context_digest"] = hashlib.sha256(
            setup["policy_context"]["body"].encode()
        ).hexdigest()
    elif change == "input_digest":
        binding["input_body_digest"] = binding["context_digest"] = "d" * 64
    elif change == "partial":
        del binding["input_body_digest"]
    elif change == "foreign_policy":
        _, credential = enroll(
            setup["client"], setup["owner"], setup["project"], ["context:read", "provenance:append"]
        )
        other = create_context(
            setup, canonical(setup["policy"]), credential=credential["credential_id"]
        )
        binding["capture_policy_context_id"] = other["id"]
    with pytest.raises(ValueError):
        PublicCaptureAdapter(binding, setup["resolver"], setup["output_resolver"])


@pytest.mark.parametrize(
    "pin",
    [
        "model_id",
        "session_id",
        "harness_instance_id",
        "run_id",
        "skill_digest",
        "config_digest",
        "input_context_id",
    ],
)
def test_distinct_human_policy_must_approve_actual_pins(separated, pin):
    setup = separated
    policy = deepcopy(setup["policy"])
    policy[pin] = "d" * 64 if pin.endswith("digest") else "different_opaque_id"
    context = create_context(setup, canonical(policy))
    binding = {**setup["binding"], "capture_policy_context_id": context["id"]}
    with pytest.raises(ValueError):
        PublicCaptureAdapter(binding, setup["resolver"])


def test_plain_or_unapproved_policy_and_supplied_pin_echo_fail_closed(separated):
    setup = separated
    context = create_context(setup, "not a capture policy")
    with pytest.raises(ValueError):
        PublicCaptureAdapter(
            {**setup["binding"], "capture_policy_context_id": context["id"]}, setup["resolver"]
        )
    response = setup["client"].post(
        f"/projects/{setup['project']}/agent-contexts",
        json={
            "credential_id": setup["binding"]["credential_id"],
            "destination": setup["author"]["X-Agent-Destination"],
            "title": "unapproved policy",
            "body": canonical(setup["policy"]),
            "classification": "project",
            "disclosure_approved": False,
        },
        headers={**setup["owner"], "Idempotency-Key": "unapproved-context"},
    )
    # Backend does not create unapproved immutable body contexts.
    assert response.status_code == 422
    with pytest.raises(ValueError):
        PublicCaptureAdapter(setup["binding"], lambda b: b)
    readback = setup["resolver"](setup["binding"])
    readback["input_context"]["body"] = "RAW_PRIVATE_READBACK_SENTINEL"
    with pytest.raises(ValueError) as error:
        PublicCaptureAdapter(setup["binding"], lambda _: readback)
    assert "RAW_PRIVATE_READBACK_SENTINEL" not in str(error.value)


def test_collector_split_policy_validation_replay_and_revoked_disclosure(separated):
    setup = separated
    first = append(setup)
    assert first.status_code == 201
    changed = deepcopy(setup["envelope"])
    new_policy = create_context(setup, canonical(setup["policy"]))
    changed["record"]["harness"]["capture_policy_context_id"] = new_policy["id"]
    assert append(setup, changed).status_code == 409
    assert append(setup, changed, key="rekey").status_code == 409
    post(
        setup["client"],
        setup["owner"],
        f"/generated-output-disclosures/{setup['disclosure']['id']}/revoke",
        {"expected_revision": 1},
        expected=200,
    )
    assert append(setup).status_code == 403
    with pytest.raises(ValueError):
        setup["adapter"].envelope(setup["observation"])


def test_policy_json_cannot_be_copied_into_a_distinct_input_context(separated):
    setup = separated
    copied_input = create_context(setup, canonical(setup["policy"]))
    digest = hashlib.sha256(copied_input["body"].encode()).hexdigest()
    policy = {
        **setup["policy"],
        "input_context_id": copied_input["id"],
        "input_body_digest": digest,
        "context_digest": digest,
    }
    separate_policy = create_context(setup, canonical(policy))
    binding = {
        **setup["binding"],
        "context_id": copied_input["id"],
        "input_context_id": copied_input["id"],
        "input_body_digest": digest,
        "context_digest": digest,
        "capture_policy_context_id": separate_policy["id"],
    }
    with pytest.raises(ValueError):
        PublicCaptureAdapter(binding, setup["resolver"])


def test_actual_revoked_actor_cannot_resolve_input_or_policy(separated):
    setup = separated
    response = setup["client"].post(
        f"/credentials/{setup['binding']['credential_id']}/revoke",
        headers={**setup["owner"], "Idempotency-Key": "revoke-reader"},
    )
    assert response.status_code == 200
    with pytest.raises(ValueError):
        setup["adapter"].resolve_contexts()
    assert append(setup).status_code == 401


@pytest.mark.parametrize("change", ["policy_id", "input_digest", "swap", "missing_field"])
def test_collector_rechecks_actual_separated_backend_contexts(separated, change):
    setup = separated
    envelope = deepcopy(setup["envelope"])
    harness = envelope["record"]["harness"]
    if change == "policy_id":
        context = create_context(setup, "not a policy, never substituted as input")
        harness["capture_policy_context_id"] = context["id"]
    elif change == "input_digest":
        harness["input_body_digest"] = harness["context_digest"] = "d" * 64
    elif change == "swap":
        harness["input_context_id"], harness["capture_policy_context_id"] = (
            harness["capture_policy_context_id"],
            harness["input_context_id"],
        )
        harness["context_id"] = harness["input_context_id"]
        envelope["record"]["references"]["context_id"] = harness["input_context_id"]
        harness["context_digest"] = harness["input_body_digest"] = hashlib.sha256(
            setup["policy_context"]["body"].encode()
        ).hexdigest()
    elif change == "missing_field":
        del harness["capture_policy_context_id"]
    response = append(setup, envelope)
    assert response.status_code in {403, 409, 422}, response.text
    with _connect(setup["state"] / "tessera.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_provenance WHERE record_id=?",
                (envelope["record"]["record_id"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("bad", ["raw", "private", "wrong_row", "overflow"])
def test_normalized_producer_port_does_not_extract_or_count_private_parts(separated, bad):
    setup = separated
    row = {"id": setup["binding"]["run_id"], "session_id": setup["binding"]["session_id"]}
    observations = [setup["observation"]]
    if bad == "raw":
        observations = [{"type": "message.updated", "properties": {"sessionID": row["session_id"]}}]
    elif bad == "private":
        observations = [
            {**setup["observation"], "parts": [{"type": "reasoning", "text": "PRIVATE_SENTINEL"}]}
        ]
    elif bad == "wrong_row":
        row["id"] = "other_run"
    elif bad == "overflow":
        observations = observations * 2
    producer = NormalizedPublicMetadataProducer(
        setup["adapter"], lambda _: observations, batch_limit=1
    )
    with pytest.raises(ValueError) as error:
        producer.read_public_metadata(row)
    assert "PRIVATE_SENTINEL" not in str(error.value)


def test_legacy_binding_and_observation_serialize_without_separation_fields(request):
    setup = request.getfixturevalue("registered")
    value = CaptureBinding.model_validate(setup["binding"]).model_dump(mode="json")
    for field in ("input_context_id", "input_body_digest", "capture_policy_context_id"):
        assert field not in value
        assert field not in setup["adapter"].envelope(setup["observation"])["record"]["harness"]
