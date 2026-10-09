"""Real temporary registration/collector/outbox joins; synthetic public source bytes."""

import base64
import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_authority import enroll, post

from tessera_agent.provenance_client import ProvenanceClient
from tessera_control.agent_provenance import project_agent_notebook
from tessera_control.api import create_app
from tessera_control.public_capture import PublicCaptureAdapter, canonical
from tessera_control.public_capture_outbox import PublicCaptureOutbox
from tessera_control.store import Store, _connect, initialize_state


@pytest.fixture
def registered(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    owner = {"Authorization": (state / "owner.header").read_text().split(": ", 1)[1].strip()}
    project = post(client, owner, "/projects", {"name": "provenance research output"})["id"]
    scopes = ["context:read", "provenance:append", "notebook:read"]
    author, credential = enroll(client, owner, project, scopes)
    receiver, recipient = enroll(client, owner, project, scopes)
    destination = author["X-Agent-Destination"]
    policy = {
        "schema_version": "tessera-public-harness-disclosure-v1",
        "run_id": "research_run_1",
        "task_id": None,
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "allowed_public_events": ["generated_output"],
    }
    context = post(
        client,
        owner,
        f"/projects/{project}/agent-contexts",
        {
            "credential_id": credential["credential_id"],
            "destination": destination,
            "title": "approved normalized research metadata",
            "body": canonical(policy),
            "classification": "project",
            "disclosure_approved": True,
        },
    )
    pins = {
        "model_id": "fixture_model",
        "run_id": policy["run_id"],
        "session_id": "session_1",
        "harness_instance_id": "instance_1",
        "context_id": context["id"],
        "context_digest": policy["context_digest"],
        "skill_digest": policy["skill_digest"],
        "config_digest": policy["config_digest"],
        "task_id": None,
        "operation_id": None,
    }
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    grant = post(
        client,
        owner,
        f"/projects/{project}/generated-output-grants",
        {
            "action": "generated-output:register",
            "output_kind": "critic_review",
            "credential_id": credential["credential_id"],
            "destination": destination,
            "pins": pins,
            "public_output_admitted": True,
            "max_outputs": 1,
            "max_total_bytes": 1024,
            "expires_at": expiry,
        },
    )
    content = b"Synthetic public critic recommendation; human review required."
    body = {
        "grant_id": grant["id"],
        "expected_revision": 1,
        "pins": pins,
        "source_event_id": "source_1",
        "source_sequence": 1,
        "output_kind": "critic_review",
        "visibility": "public",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }
    output = post(
        client, author, f"/projects/{project}/generated-outputs", body, key="registration"
    )
    disclosures = []
    for who in (credential, recipient):
        disclosures.append(
            post(
                client,
                owner,
                f"/generated-outputs/{output['id']}/disclosures",
                {
                    "credential_id": who["credential_id"],
                    "destination": destination,
                    "disclosure_approved": True,
                    "expires_at": expiry,
                },
            )
        )
    binding = {
        **{key: pins[key] for key in pins if key != "operation_id"},
        "schema_version": "tessera-public-capture-binding-v1",
        "project_id": project,
        "org_id": output["org_id"],
        "actor_principal_id": output["actor_id"],
        "credential_id": output["credential_id"],
        "destination_digest": "sha256:" + hashlib.sha256(destination.encode()).hexdigest(),
        "allowed_public_events": ["generated_output"],
    }

    def manifest(b):
        response = client.get(f"/agent-contexts/{context['id']}", headers=author)
        assert response.status_code == 200
        return {
            **{
                k: b[k]
                for k in (
                    "context_id",
                    "org_id",
                    "project_id",
                    "credential_id",
                    "destination_digest",
                )
            },
            "issuer_role": "human",
            "disclosure_approved": response.json()["disclosure_approved"],
            "classification": response.json()["classification"],
            "body": response.json()["body"],
        }

    def resolve(ident, b):
        response = client.get(
            f"/generated-outputs/{ident}/metadata",
            params={"disclosure_id": disclosures[0]["id"]},
            headers=author,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["pins"] == pins
        return {
            "family": "generated_public_research_output_v1",
            "output_id": result["id"],
            "artifact_id": result["artifact_id"],
            "disclosure_id": disclosures[0]["id"],
            "output_kind": result["output_kind"],
            "writer_actor_id": result["actor_id"],
            "writer_credential_id": result["credential_id"],
            "writer_org_id": result["org_id"],
            "binding": b,
            "source_event_id": result["source_event_id"],
            "source_sequence": result["source_sequence"],
            "operation_id": result["pins"]["operation_id"],
            "sha256": result["sha256"],
        }

    adapter = PublicCaptureAdapter(binding, manifest, resolve)
    observation = {
        **pins,
        "schema_version": "tessera-normalized-public-observation-v1",
        "public_event": "generated_output",
        "source_event_id": body["source_event_id"],
        "source_sequence": 1,
        "source_watermark": 3,
        "capture_state": "bounded_public_stream",
        "gaps": [{"first": 2, "last": 3, "reason": "source_unavailable"}],
        "outcome": "unknown",
        "registered_output_id": output["id"],
    }
    yield {
        "state": state,
        "client": client,
        "owner": owner,
        "project": project,
        "author": author,
        "receiver": receiver,
        "output": output,
        "grant": grant,
        "disclosures": disclosures,
        "binding": binding,
        "adapter": adapter,
        "observation": observation,
        "envelope": adapter.envelope(observation),
        "content": content,
    }
    client.close()


def append(setup, body=None, headers=None, key="collector"):
    return setup["client"].post(
        f"/projects/{setup['project']}/agent-provenance",
        headers={**(headers or setup["author"]), "Idempotency-Key": key},
        json=body or setup["envelope"],
    )


def notebook(setup, actor):
    if actor == setup["owner"]:
        human = Store(setup["state"]).authenticate(actor["Authorization"].split(" ", 1)[1])
        with _connect(setup["state"] / "tessera.sqlite3") as conn:
            conn.execute("BEGIN")
            return project_agent_notebook(conn, human, setup["project"])
    response = setup["client"].get(
        f"/projects/{setup['project']}/agent-notebook/export", headers=actor
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_actual_registry_collector_outbox_restart_ack_and_both_notebook_joins(registered, tmp_path):
    setup = registered
    box = PublicCaptureOutbox(tmp_path / "private", setup["adapter"])
    rid = box.capture(setup["observation"])
    box.close()
    box = PublicCaptureOutbox(tmp_path / "private", setup["adapter"])
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        response = setup["client"].request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, json=response.json())

    binding = setup["binding"]
    with ProvenanceClient(
        "http://127.0.0.1:8000",
        setup["author"]["Authorization"].split(" ", 1)[1],
        setup["author"]["X-Agent-Destination"],
        transport=httpx.MockTransport(transport),
        expected_actor={k: binding[k] for k in ("org_id", "actor_principal_id", "credential_id")},
    ) as client:
        delivery = box.deliver_next(client)
        assert delivery["delivery"] == "accepted", delivery
        assert delivery["record_id"] == rid
        assert box.deliver_next(client) is None
    assert len(calls) == 1 and box.watermarks()["acknowledged_source_prefix"] == 1
    assert box.watermarks()["reported_source_watermark"] == 3
    assert box.watermarks()["reported_gap_events"] == 1
    replay = append(setup, key="same_record_other_key")
    assert replay.status_code == 201 and replay.json()["record_id"] == rid
    for actor in (setup["author"], setup["receiver"], setup["owner"]):
        projection = notebook(setup, actor)
        record = next(r for r in projection["provenance"] if r["record_id"] == rid)
        output = record["output"]
        event = next(
            e
            for e in projection["control_events"]
            if e["event_type"] == "generated_output.registered"
        )
        assert event["output"]["output_id"] == output["output_id"] == setup["output"]["id"]
        assert output["family"] == "generated_public_research_output_v1"
        expected_disclosure = (
            None
            if actor == setup["owner"]
            else setup["disclosures"][0 if actor == setup["author"] else 1]["id"]
        )
        assert output["recipient_disclosure_id"] == expected_disclosure
        assert output["content_digest"] == "sha256:" + hashlib.sha256(setup["content"]).hexdigest()
        assert output["pins"]["task_id"] is None and output["pins"]["operation_id"] is None
        assert (
            output["scientific_validity"] == "not_assessed"
            and output["execution_established"] is False
        )
        assert record["harness_capture"]["source_completeness"] == "reported_source_gaps"
        assert "content_base64" not in canonical(projection)
        assert setup["content"].decode() not in canonical(projection)
        assert not any(k in output for k in ("output_manifest_id", "job_uid", "lease_id"))
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM agent_provenance").fetchone()[0] == 1
        assert conn.execute("SELECT used_outputs FROM generated_output_grants").fetchone()[0] == 1
    box.close()


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "writer",
        "model",
        "session",
        "context",
        "config",
        "source",
        "sequence",
        "worker_family",
        "missing_family",
        "wrong_output",
        "receiver",
        "wrong_family",
        "wrong_tenant_claim",
        "recipient_disclosure",
        "content_digest",
    ],
)
def test_registration_correlation_and_family_fences_atomic(registered, change):
    setup = registered
    envelope = deepcopy(setup["envelope"])
    record = envelope["record"]
    ref = record["generated_output"]
    headers = None
    if change == "digest":
        ref["sha256"] = "d" * 64
    elif change == "writer":
        ref["writer_actor_id"] = "other_writer"
    elif change == "model":
        record["model_id"] = "other_model"
    elif change == "session":
        record["harness"]["session_id"] = "other_session"
    elif change == "context":
        record["harness"]["context_id"] = "other_context"
    elif change == "config":
        record["harness"]["config_digest"] = "d" * 64
    elif change == "source":
        record["harness"]["source_event_id"] = "other_source"
    elif change == "sequence":
        record["harness"].update(source_sequence=4, source_watermark=4)
    elif change == "worker_family":
        record["references"]["output_manifest_id"] = ref["output_id"]
    elif change == "missing_family":
        del record["generated_output"]
    elif change == "wrong_output":
        ref["output_id"] = "artifact_manifest_not_research"
    elif change == "receiver":
        headers = setup["receiver"]
    elif change == "wrong_family":
        ref["family"] = "worker_output"
    elif change == "wrong_tenant_claim":
        ref["writer_org_id"] = "other_tenant"
    elif change == "recipient_disclosure":
        ref["disclosure_id"] = setup["disclosures"][1]["id"]
    elif change == "content_digest":
        record["content_digest"] = "sha256:" + "d" * 64
    response = append(setup, envelope, headers=headers)
    assert response.status_code in {403, 404, 409, 422}, response.text
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_provenance WHERE record_id=?", (record["record_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM events WHERE event_type='agent_provenance.recorded'"
            ).fetchone()[0]
            == 0
        )


def test_current_recipient_disclosure_redacts_record_and_registered_event(registered):
    setup = registered
    assert append(setup).status_code == 201
    recipient_disclosure = setup["disclosures"][1]
    post(
        setup["client"],
        setup["owner"],
        f"/generated-output-disclosures/{recipient_disclosure['id']}/revoke",
        {"expected_revision": 1},
        expected=200,
    )
    restricted = notebook(setup, setup["receiver"])
    record = restricted["provenance"][0]
    assert "generated_output" not in record and record["content_digest"] is None
    assert record["output"]["visibility"] == "withheld_current_disclosure"
    assert restricted["completeness"] == "permission_filtered_recorded_project_metadata_snapshot"
    event = next(
        e for e in restricted["control_events"] if e["event_type"] == "generated_output.registered"
    )
    assert event["output"]["visibility"] == "withheld_current_disclosure"
    assert setup["output"]["sha256"] not in canonical(restricted)
    assert (
        notebook(setup, setup["owner"])["provenance"][0]["output"]["visibility"]
        == "authorized_metadata"
    )


@pytest.mark.parametrize(
    "fence", ["disclosure_revoke", "disclosure_expiry", "grant_revoke", "grant_expiry"]
)
def test_collector_replay_rechecks_current_research_authority(registered, fence):
    setup = registered
    first = append(setup)
    assert first.status_code == 201, first.text
    assert append(setup).json() == first.json()
    if fence.endswith("revoke"):
        ident = (
            setup["disclosures"][0]["id"]
            if fence.startswith("disclosure")
            else setup["grant"]["id"]
        )
        family = (
            "generated-output-disclosures"
            if fence.startswith("disclosure")
            else "generated-output-grants"
        )
        post(
            setup["client"],
            setup["owner"],
            f"/{family}/{ident}/revoke",
            {"expected_revision": 1},
            expected=200,
        )
    else:
        table = (
            "generated_output_disclosures"
            if fence.startswith("disclosure")
            else "generated_output_grants"
        )
        ident = (
            setup["disclosures"][0]["id"]
            if fence.startswith("disclosure")
            else setup["grant"]["id"]
        )
        with sqlite3.connect(setup["state"] / "tessera.sqlite3") as conn:
            bounds = json.loads(
                conn.execute(f"SELECT bounds_json FROM {table} WHERE id=?", (ident,)).fetchone()[0]
            )
            bounds["expires_at"] = "2020-01-01T00:00:00+00:00"
            conn.execute(f"UPDATE {table} SET bounds_json=? WHERE id=?", (canonical(bounds), ident))
    assert append(setup).status_code in {403, 409}
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_provenance WHERE record_id=?",
                (setup["envelope"]["record"]["record_id"],),
            ).fetchone()[0]
            == 1
        )


def test_research_output_reference_cannot_cross_project_or_tenant(registered):
    setup = registered
    other_project = post(setup["client"], setup["owner"], "/projects", {"name": "other project"})[
        "id"
    ]
    peer, _ = enroll(
        setup["client"],
        setup["owner"],
        other_project,
        ["provenance:append", "context:read", "notebook:read"],
    )
    result = setup["client"].post(
        f"/projects/{other_project}/agent-provenance",
        headers={**peer, "Idempotency-Key": "cross-project"},
        json=setup["envelope"],
    )
    assert result.status_code in {403, 404}

    result = setup["client"].get(
        f"/projects/{setup['project']}/agent-notebook/export", headers=peer
    )
    assert result.status_code in {403, 404}


def test_registered_event_projection_rejects_unsafe_pin_values(registered):
    setup = registered
    grant_body = deepcopy(setup["grant"]["bounds"])
    grant_body["pins"]["model_id"] = "RAW_PRIVATE_PIN_SENTINEL is not an opaque model ID"
    grant = post(
        setup["client"],
        setup["owner"],
        f"/projects/{setup['project']}/generated-output-grants",
        grant_body,
    )
    registration = {
        "grant_id": grant["id"],
        "expected_revision": 1,
        "pins": grant_body["pins"],
        "source_event_id": "other_source",
        "source_sequence": 1,
        "output_kind": "critic_review",
        "visibility": "public",
        "sha256": hashlib.sha256(setup["content"]).hexdigest(),
        "content_base64": base64.b64encode(setup["content"]).decode(),
    }
    post(
        setup["client"],
        setup["author"],
        f"/projects/{setup['project']}/generated-outputs",
        registration,
    )
    projection = notebook(setup, setup["owner"])
    assert "RAW_PRIVATE_PIN_SENTINEL" not in canonical(projection)
    outputs = [
        e["output"]
        for e in projection["control_events"]
        if e["event_type"] == "generated_output.registered"
    ]
    assert sum(o["visibility"] == "authorized_metadata" for o in outputs) == 1
    assert sum(o["visibility"] == "withheld_current_disclosure" for o in outputs) == 1
