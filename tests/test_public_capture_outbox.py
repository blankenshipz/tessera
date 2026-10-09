"""Normalized metadata and isolated client/outbox fixtures, NOT real harness capture."""

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from tessera_agent.provenance_client import DeliveryResult, ProvenanceClient
from tessera_control.public_capture import PublicCaptureAdapter, canonical
from tessera_control.public_capture_outbox import PublicCaptureOutbox


@pytest.fixture
def binding():
    return {
        "schema_version": "tessera-public-capture-binding-v1",
        "org_id": "org_1",
        "project_id": "project_1",
        "actor_principal_id": "agent_1",
        "credential_id": "cred_1",
        "context_id": "context_1",
        "run_id": "run_1",
        "session_id": "session_1",
        "harness_instance_id": "harness_1",
        "model_id": "test_model_1",
        "task_id": "task_1",
        "destination_digest": "sha256:" + hashlib.sha256(b"critic.local").hexdigest(),
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "allowed_public_events": [
            "message",
            "tool",
            "permission",
            "delegation",
            "generated_output",
        ],
    }


def disclosure(binding):
    policy = {
        k: binding[k]
        for k in (
            "run_id",
            "task_id",
            "context_digest",
            "skill_digest",
            "config_digest",
            "allowed_public_events",
        )
    }
    policy["schema_version"] = "tessera-public-harness-disclosure-v1"
    return {
        **{
            k: binding[k]
            for k in ("context_id", "org_id", "project_id", "credential_id", "destination_digest")
        },
        "issuer_role": "human",
        "disclosure_approved": True,
        "classification": "project",
        "body": canonical(policy),
    }


def observation(binding, sequence=1, event="message", **changes):
    return {
        **{
            k: binding[k]
            for k in (
                "run_id",
                "session_id",
                "harness_instance_id",
                "task_id",
                "context_id",
                "model_id",
                "context_digest",
                "skill_digest",
                "config_digest",
            )
        },
        "schema_version": "tessera-normalized-public-observation-v1",
        "public_event": event,
        "source_event_id": f"event_{sequence}",
        "source_sequence": sequence,
        "source_watermark": sequence,
        "capture_state": "bounded_public_stream",
        "gaps": [],
        "outcome": "unknown",
        "message_id": f"message_{sequence}" if event == "message" else None,
        "tool_id": "research.status" if event == "tool" else None,
        **changes,
    }


def adapter(binding, **kwargs):
    return PublicCaptureAdapter(binding, lambda b: disclosure(b), **kwargs)


def accepted(rid, sequence=19):
    return DeliveryResult("accepted", "collector_acknowledged", rid, 201, sequence)


class Client:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or accepted

    def append(self, project, record, grant_proof=None, idempotency_key=None):
        self.calls.append((project, deepcopy(record), grant_proof, idempotency_key))
        return self.answer(record["record_id"])


@pytest.mark.parametrize("event", ["message", "tool", "permission", "delegation"])
def test_allowlisted_normalized_categories_only_observe(tmp_path, binding, event):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    rid = box.capture(observation(binding, event=event))
    client = Client()
    assert box.deliver_next(client) == {
        "record_id": rid,
        "delivery": "accepted",
        "collector_sequence": 19,
    }
    record = client.calls[0][1]
    assert record["kind"] == event and record["outcome"] == "unknown"
    assert record["references"]["task_id"] == binding["task_id"]
    assert record["harness"]["schema_version"] == "tessera-public-harness-metadata-v1"
    assert box.watermarks()["authority_effect"] == "none"
    box.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": "PRIVATE_SENTINEL"},
        {"public": True},
        {"approved": True},
        {"parts": [{"type": "reasoning", "text": "PRIVATE_SENTINEL"}]},
        {"tool_body": "PRIVATE_SENTINEL"},
        {"public_event": "reasoning"},
        {"schema_version": "opencode-v2-event"},
        {"source_sequence": True},
        {"capture_state": "complete_transcript"},
        {"config_digest": "d" * 64},
        {"context_id": "context_other"},
        {"model_id": "model_other"},
        {"session_id": "session_other"},
        {"run_id": "run_other"},
        {"task_id": "task_other"},
        {"harness_instance_id": "harness_other"},
        {"source_sequence": 2},
        {"gaps": [{"first": 1, "last": 1, "reason": "delivery_unknown"}]},
        {"public_event": "tool", "tool_id": "authority.approve", "message_id": None},
        {"registered_output_id": "artifact_unapproved"},
    ],
)
def test_fail_closed_before_persistence_no_private_count(tmp_path, binding, changes):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    with pytest.raises(ValueError) as error:
        box.capture(observation(binding, **changes))
    assert "PRIVATE_SENTINEL" not in str(error.value)
    assert box.watermarks()["captured_events"] == 0
    assert box.watermarks()["reported_source_watermark"] == 0
    assert box.connection.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] == 0
    box.close()
    assert b"PRIVATE_SENTINEL" not in (tmp_path / "private/public-capture.sqlite3").read_bytes()


def test_unknown_raw_v2_fields_not_a_supported_parser(tmp_path, binding):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    with pytest.raises(ValueError, match="Unsupported"):
        box.capture(
            {
                "type": "message.updated",
                "properties": {
                    "info": {"id": "message_1", "sessionID": "session_1", "role": "assistant"}
                },
            }
        )
    assert box.watermarks()["captured_events"] == 0
    box.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"issuer_role": "agent"},
        {"disclosure_approved": False},
        {"public": True, "body": "{}"},
        {"credential_id": "other"},
        {"destination_digest": "sha256:" + "d" * 64},
        {"classification": "restricted"},
    ],
)
def test_human_manifest_not_self_assertion(binding, changed):
    with pytest.raises(ValueError, match="human disclosure"):
        PublicCaptureAdapter(binding, lambda b: {**disclosure(b), **changed})


def test_revoked_disclosure_never_sends_cached_metadata(tmp_path, binding):
    document = disclosure(binding)
    capture = PublicCaptureAdapter(binding, lambda _: document)
    box = PublicCaptureOutbox(tmp_path / "private", capture)
    box.capture(observation(binding))
    document["disclosure_approved"] = False
    client = Client()
    assert box.deliver_next(client)["delivery"] == "unknown"
    assert client.calls == []
    assert box.watermarks()["acknowledged_events"] == 0
    box.close()


def test_restart_stable_replay_and_crash_redelivery(tmp_path, binding):
    path = tmp_path / "private"
    box = PublicCaptureOutbox(path, adapter(binding))
    rid = box.capture(observation(binding))
    assert box.capture(observation(binding)) == rid

    def crash(_):
        raise KeyboardInterrupt()

    crashed = Client(crash)
    with pytest.raises(KeyboardInterrupt):
        box.deliver_next(crashed)
    assert box.watermarks()["unknown_delivery_events"] == 1
    box.close()
    box = PublicCaptureOutbox(path, adapter(binding))
    client = Client()
    assert box.deliver_next(client)["delivery"] == "accepted"
    assert client.calls[0] == crashed.calls[0]
    assert box.deliver_next(client) is None
    assert box.watermarks()["acknowledged_source_prefix"] == 1
    assert box.connection.execute("SELECT attempts FROM capture_events").fetchone()[0] == 2
    with pytest.raises(ValueError, match="replay differs"):
        box.capture(observation(binding, outcome="succeeded"))
    with pytest.raises(ValueError, match="replay differs"):
        box.capture(observation(binding, source_event_id="other_event"))
    box.close()
    with pytest.raises(ValueError, match="pins"):
        PublicCaptureOutbox(path, adapter({**binding, "session_id": "replacement"}))


@pytest.mark.parametrize(
    "mutation",
    [
        {"sequence": True},
        {"sequence": "19"},
        {"sequence": 0},
        {"status_code": True},
        {"record_id": "wrong"},
        {"code": "not_collector_ack"},
        {"status": "unknown"},
    ],
)
def test_strict_delivery_ack_no_truthy_watermark(tmp_path, binding, mutation):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    box.capture(observation(binding))
    client = Client(lambda rid: replace(accepted(rid), **mutation))
    assert box.deliver_next(client)["delivery"] == "unknown"
    assert box.watermarks()["acknowledged_source_prefix"] == 0
    box.close()


def test_raw_or_extra_ack_not_accepted(tmp_path, binding):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    box.capture(observation(binding))
    client = Client(lambda rid: SimpleNamespace(**vars(accepted(rid)), raw="PRIVATE_SENTINEL"))
    assert box.deliver_next(client)["delivery"] == "unknown"
    assert box.watermarks()["unknown_delivery_events"] == 1
    box.close()
    assert b"PRIVATE_SENTINEL" not in (tmp_path / "private/public-capture.sqlite3").read_bytes()


def test_failed_unknown_gap_and_ack_watermarks_separate(tmp_path, binding):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    box.capture(
        observation(
            binding,
            source_watermark=4,
            capture_state="unknown",
            gaps=[{"first": 2, "last": 3, "reason": "delivery_unknown"}],
        )
    )
    client = Client(lambda rid: DeliveryResult("rejected", "collector_rejected", rid, 403))
    assert box.deliver_next(client)["delivery"] == "send_failed"
    counters = box.watermarks()
    assert counters["send_failed_events"] == 1 and counters["missing_source_events"] == 3
    assert counters["reported_gap_events"] == 1 and counters["unknown_capture_events"] == 1
    assert counters["acknowledged_events"] == 0
    assert box.deliver_next(Client())["delivery"] == "accepted"
    box.capture(observation(binding, 4))
    box.deliver_next(Client())
    assert box.watermarks()["acknowledged_events"] == 2
    assert box.watermarks()["acknowledged_source_prefix"] == 1
    box.close()


def test_private_permissions_capacity_and_symlink_rejection(tmp_path, binding):
    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding), capacity=1)
    box.capture(observation(binding))
    with pytest.raises(ValueError, match="capacity"):
        box.capture(observation(binding, 2))
    box.close()
    assert os.stat(tmp_path / "private").st_mode & 0o777 == 0o700
    db = tmp_path / "private/public-capture.sqlite3"
    assert os.stat(db).st_mode & 0o777 == 0o600
    os.chmod(db, 0o644)
    with pytest.raises(ValueError, match="private"):
        PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    (tmp_path / "linked").symlink_to(tmp_path / "private")
    with pytest.raises(ValueError, match="private"):
        PublicCaptureOutbox(tmp_path / "linked", adapter(binding))


def registered_output(_, binding):
    """Synthetic reference resolver fixture, not evidence of output registration."""
    return {
        "output_id": "registered_1",
        "family": "generated_public_research_output_v1",
        "artifact_id": "registered_1",
        "output_kind": "critic_review",
        "writer_actor_id": binding["actor_principal_id"],
        "writer_credential_id": binding["credential_id"],
        "writer_org_id": binding["org_id"],
        "disclosure_id": "disclosure_1",
        "binding": binding,
        "source_event_id": "event_1",
        "source_sequence": 1,
        "operation_id": "op_1",
        "sha256": "d" * 64,
    }


def test_output_requires_registry_reference_never_fakes_job_manifest(tmp_path, binding):
    event = observation(
        binding, event="generated_output", operation_id="op_1", registered_output_id="registered_1"
    )
    with pytest.raises(ValueError):
        adapter(binding).envelope(event)
    box = PublicCaptureOutbox(
        tmp_path / "private", adapter(binding, output_resolver=registered_output)
    )
    box.capture(event)
    # Synthetic rejection is delivery uncertainty, not proof of registration or reasoning.
    client = Client(lambda rid: DeliveryResult("rejected", "collector_rejected", rid, 403))
    assert box.deliver_next(client)["delivery"] == "send_failed"
    record = client.calls[0][1]
    assert record["references"]["artifact_id"] is None
    assert record["references"]["control_event_id"] is None
    assert record["generated_output"]["output_id"] == "registered_1"
    assert "output_manifest_id" not in record["references"]
    assert record["content_digest"] == "sha256:" + "d" * 64
    assert box.watermarks()["acknowledged_events"] == 0
    box.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"output_id": "other"},
        {"source_sequence": 2},
        {"source_event_id": "other"},
        {"operation_id": "other"},
        {"sha256": "PRIVATE_SENTINEL"},
        {"output_manifest_id": "fake_job_manifest"},
        {"disclosure_id": None},
    ],
)
def test_output_reference_mismatch_denied(binding, changes):
    capture = adapter(binding, output_resolver=lambda o, b: {**registered_output(o, b), **changes})
    with pytest.raises(ValueError, match="Unsupported"):
        capture.envelope(
            observation(
                binding,
                event="generated_output",
                operation_id="op_1",
                registered_output_id="registered_1",
            )
        )


def test_exact_existing_client_interface_with_strict_http_ack(tmp_path, binding):
    calls = []

    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["Idempotency-Key"].startswith("provenance-")
        record = body["record"]
        return httpx.Response(
            201,
            json={
                **record,
                "sequence": 27,
                "org_id": binding["org_id"],
                "project_id": binding["project_id"],
                "actor_principal_id": binding["actor_principal_id"],
                "credential_id": binding["credential_id"],
                "recorded_at": "2026-10-05T00:00:00Z",
                "model_attribution": "self_reported",
            },
        )

    box = PublicCaptureOutbox(tmp_path / "private", adapter(binding))
    box.capture(observation(binding))
    with ProvenanceClient(
        "http://127.0.0.1:8000",
        "isolated-test-token",
        "critic.local",
        transport=httpx.MockTransport(transport),
        expected_actor={k: binding[k] for k in ("org_id", "actor_principal_id", "credential_id")},
    ) as client:
        assert box.deliver_next(client)["collector_sequence"] == 27
    assert len(calls) == 1 and box.watermarks()["acknowledged_source_prefix"] == 1
    box.close()
