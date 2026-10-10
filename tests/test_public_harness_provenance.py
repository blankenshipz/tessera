"""Isolated public metadata fixtures; no harness/model/public-output execution."""

import json
import sqlite3
from copy import deepcopy

import pytest
from test_agent_authority import enroll, post, prepared  # noqa: F401
from test_agent_provenance_http_integration import collector  # noqa: F401
from test_agent_provenance_outputs import output  # noqa: F401

from tessera_control.agent_provenance_models import AgentProvenanceRecord
from tessera_control.api import create_app
from tessera_control.store import _connect


def policy(task_id, **changes):
    return {
        "schema_version": "tessera-public-harness-disclosure-v1",
        "run_id": "public_run_1",
        "task_id": task_id,
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
        **changes,
    }


def create_policy(client, owner, credential_id, destination, project_id, document, key="policy"):
    return post(
        client,
        owner,
        f"/projects/{project_id}/agent-contexts",
        {
            "credential_id": credential_id,
            "destination": destination,
            "title": "Bounded public capture metadata policy",
            "body": json.dumps(document),
            "classification": "project",
            "disclosure_approved": True,
        },
        key=key,
    )


def harness(context_id, task_id, seq=1, watermark=None, event="message", **changes):
    return {
        "schema_version": "tessera-public-harness-metadata-v1",
        "run_id": "public_run_1",
        "session_id": "session_1",
        "harness_instance_id": "instance_1",
        "task_id": task_id,
        "context_id": context_id,
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "public_event": event,
        "source_event_id": f"public_event_{seq}",
        "source_sequence": seq,
        "source_watermark": watermark or seq,
        "capture_state": "bounded_public_stream",
        "gaps": [],
        **changes,
    }


@pytest.fixture
def capture(request):
    state, client, owner, agent, credential, project, grant, body = request.getfixturevalue(
        "collector"
    )
    body = deepcopy(body)
    task_id = body["record"]["references"]["task_id"]
    context = create_policy(
        client,
        owner,
        credential["credential_id"],
        agent["X-Agent-Destination"],
        project,
        policy(task_id),
    )
    body["record"]["references"]["context_id"] = context["id"]
    body["record"].update(kind="message", harness=harness(context["id"], task_id))
    body["record"]["remote"]["message_id"] = "upstream_message_1"
    return state, client, owner, agent, credential, project, grant, body


def append(capture, body=None, key="capture_1"):
    _, client, _, agent, _, project, _, default = capture
    return client.post(
        f"/projects/{project}/agent-provenance",
        json=body or default,
        headers={**agent, "Idempotency-Key": key},
    )


def projected(capture, record_id):
    _, client, _, agent, _, project, _, _ = capture
    response = client.get(f"/projects/{project}/agent-notebook/export", headers=agent)
    assert response.status_code == 200
    return next(
        record for record in response.json()["provenance"] if record["record_id"] == record_id
    )


def test_public_metadata_policy_pins_replay_and_bounded_capture(capture):
    response = append(capture)
    assert response.status_code == 201, response.text
    assert append(capture).json() == response.json()
    row = projected(capture, "runtime_record_1")
    assert row["harness"]["source_sequence"] == 1
    assert row["harness_capture"]["source_completeness"] == (
        "complete_recorded_public_metadata_through_reported_watermark"
    )
    assert row["harness_capture"]["authority_effect"] == "none"
    assert row["harness_capture"]["raw_transcript"] == "not_captured"
    assert row["harness_capture"]["unseen_sources_and_beyond_watermark"] == "unknown"
    assert row["model_attribution"] == "self_reported"
    with sqlite3.connect(capture[0] / "tessera.sqlite3") as conn:
        assert conn.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 56
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_provenance WHERE record_id='runtime_record_1'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "PRIVATE_FIXTURE"},
        {"tool_payload": {"value": "PRIVATE_FIXTURE"}},
        {"chain_of_thought": "PRIVATE_FIXTURE"},
        {"public": True},
        {"approved": True},
        {"public_event": "reasoning"},
        {"source_sequence": True},
        {"source_sequence": 2},
        {"source_watermark": 2**63},
        {"capture_state": "complete_full_transcript"},
        {"config_digest": "https://unsafe.invalid/?token=PRIVATE_FIXTURE"},
        {"gaps": [{"first": 2, "last": 1, "reason": "source_unavailable"}]},
        {
            "source_watermark": 3,
            "gaps": [
                {"first": 2, "last": 3, "reason": "delivery_unknown"},
                {"first": 3, "last": 3, "reason": "source_unavailable"},
            ],
        },
    ],
)
def test_no_raw_or_self_authorized_public_claims(capture, change):
    body = deepcopy(capture[-1])
    body["record"]["harness"].update(change)
    response = append(capture, body)
    assert response.status_code == 422
    assert "PRIVATE_FIXTURE" not in response.text


@pytest.mark.parametrize("field", ["context_digest", "skill_digest", "config_digest"])
def test_immutable_approved_digest_mismatch_denied(capture, field):
    body = deepcopy(capture[-1])
    body["record"]["harness"][field] = "d" * 64
    assert append(capture, body).status_code == 409


def test_plain_context_is_not_public_capture_approval(capture):
    _, client, owner, agent, credential, project, _, default = capture
    context = create_policy(
        client,
        owner,
        credential["credential_id"],
        agent["X-Agent-Destination"],
        project,
        {"public": True},
        key="plain",
    )
    body = deepcopy(default)
    body["record"]["references"]["context_id"] = context["id"]
    body["record"]["harness"]["context_id"] = context["id"]
    assert append(capture, body).status_code == 403


def test_foreign_capture_context_and_destination_are_denied(capture):
    _, client, owner, agent, _, project, _, default = capture
    peer, credential = enroll(client, owner, project, ["context:read", "provenance:append"])
    context = create_policy(
        client,
        owner,
        credential["credential_id"],
        peer["X-Agent-Destination"],
        project,
        policy(default["record"]["references"]["task_id"]),
        key="peer-policy",
    )
    body = deepcopy(default)
    body["record"]["references"]["context_id"] = context["id"]
    body["record"]["harness"]["context_id"] = context["id"]
    assert append(capture, body).status_code == 403
    assert (
        client.post(
            f"/projects/{project}/agent-provenance",
            json=default,
            headers={**agent, "X-Agent-Destination": "other", "Idempotency-Key": "dest"},
        ).status_code
        == 403
    )


def test_source_identity_cannot_rekey_or_restart_with_different_session(capture):
    assert append(capture).status_code == 201
    duplicate = deepcopy(capture[-1])
    duplicate["record"]["record_id"] = "different_record"
    assert append(capture, duplicate, key="rekey").status_code == 409
    restarted = deepcopy(duplicate)
    restarted["record"]["harness"].update(
        source_sequence=2,
        source_watermark=2,
        source_event_id="public_event_2",
        session_id="new_session",
    )
    assert append(capture, restarted, key="restart").status_code == 409
    changed = deepcopy(capture[-1])
    changed["record"]["outcome"] = "succeeded"
    assert append(capture, changed).status_code == 409


@pytest.mark.parametrize(
    "state,gaps,expected",
    [
        ("bounded_public_stream", [], "missing_source_events"),
        (
            "bounded_public_stream",
            [{"first": 2, "last": 3, "reason": "delivery_unknown"}],
            "reported_source_gaps",
        ),
        ("unknown", [], "unknown_source_capture"),
    ],
)
def test_source_gaps_and_unknowns_do_not_claim_complete_transcript(capture, state, gaps, expected):
    body = deepcopy(capture[-1])
    body["record"]["harness"].update(source_watermark=3, capture_state=state, gaps=gaps)
    assert append(capture, body).status_code == 201
    summary = projected(capture, "runtime_record_1")["harness_capture"]
    assert summary["source_completeness"] == expected
    assert summary["recorded_source_events"] == 1 and summary["missing_source_event_count"] == 2
    assert summary["hidden_reasoning"] == "not_captured"


def test_public_event_categories_remain_observations_not_authority(capture):
    for index, (event, kind) in enumerate(
        (
            ("message", "message"),
            ("tool", "tool"),
            ("permission", "permission"),
            ("delegation", "delegation"),
        ),
        1,
    ):
        body = deepcopy(capture[-1])
        body["record"].update(
            record_id=f"event_{index}",
            kind=kind,
            tool_id="mcp.research.status" if event == "tool" else None,
        )
        body["record"]["harness"] = harness(
            body["record"]["harness"]["context_id"],
            body["record"]["references"]["task_id"],
            seq=index,
            event=event,
        )
        assert append(capture, body, key=f"event_{index}").status_code == 201
    with sqlite3.connect(capture[0] / "tessera.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM agent_authority_consumptions").fetchone()[0] == 0
    assert projected(capture, "event_1")["harness_capture"]["recorded_source_events"] == 4


def test_absent_harness_preserves_legacy_wire_envelope():
    legacy = AgentProvenanceRecord.model_validate(
        {
            "record_id": "legacy",
            "kind": "agent",
            "outcome": "unknown",
            "correlation_id": "legacy_run",
        }
    ).model_dump(mode="json")
    assert "harness" not in legacy
    assert "generated_output" not in legacy
    assert "output_manifest_id" not in legacy["references"]


def test_unregistered_content_digest_is_not_public_payload_approval(capture):
    body = deepcopy(capture[-1])
    body["record"]["content_digest"] = "sha256:" + "a" * 64
    assert append(capture, body).status_code == 403


def test_generated_output_requires_registered_digest_and_matching_context(request):
    from fastapi.testclient import TestClient

    output = request.getfixturevalue("output")  # noqa: F811 - imported pytest fixture
    task_id = output["manifest"]["task_id"]
    owner_token = (output["state"] / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    agent = {
        "Authorization": "Bearer " + output["agent_token"],
        "X-Agent-Destination": "critic.local",
    }
    with TestClient(create_app(output["state"])) as client:
        context = create_policy(
            client,
            {"Authorization": "Bearer " + owner_token},
            output["agent"]["credential_id"],
            "critic.local",
            output["first"]["project_id"],
            policy(task_id),
        )
        body = {
            "record": {
                "record_id": "generated_output_record",
                "kind": "attempt",
                "outcome": "late",
                "correlation_id": "public_run_1",
                "content_digest": "sha256:" + output["first"]["sha256"],
                "references": {
                    "context_id": context["id"],
                    "task_id": task_id,
                    "output_manifest_id": output["manifest"]["id"],
                    "artifact_id": output["first"]["id"],
                    "operation_id": output["manifest"]["operation_id"],
                },
                "harness": harness(context["id"], task_id, event="generated_output"),
            },
            "output_proof": {
                "output_manifest_id": output["manifest"]["id"],
                "manifest_digest": output["manifest"]["manifest_digest"],
            },
        }
        path = f"/projects/{output['first']['project_id']}/agent-provenance"
        headers = {**agent, "Idempotency-Key": "generated"}
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 201, response.text
        assert client.post(path, headers=headers, json=body).json() == response.json()
        bad = deepcopy(body)
        bad["record"].update(record_id="bad_generated", content_digest="sha256:" + "d" * 64)
        assert (
            client.post(path, headers={**headers, "Idempotency-Key": "bad"}, json=bad).status_code
            == 409
        )
        bad = deepcopy(body)
        bad["record"].update(record_id="wrong_context")
        bad["record"]["harness"]["context_id"] = "another_context"
        assert (
            client.post(path, headers={**headers, "Idempotency-Key": "ctx"}, json=bad).status_code
            == 403
        )
    with _connect(output["state"] / "tessera.sqlite3") as conn:
        assert conn.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 56
