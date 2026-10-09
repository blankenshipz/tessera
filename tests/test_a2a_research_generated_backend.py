"""Real isolated backend HTTP/SQLite + SDK routes; output bytes/model source synthetic."""

import base64
import hashlib
import json
import socket
import sqlite3
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from test_agent_authority import enroll, post, prepared  # noqa: F401

pytest.importorskip("a2a", reason="optional official A2A SDK required")

from starlette.testclient import TestClient  # noqa: E402

from tessera_control.a2a_peer import create_peer, drain_peer_outbox  # noqa: E402
from tessera_control.a2a_store import A2AStore  # noqa: E402
from tessera_control.api import create_app  # noqa: E402
from tessera_control.store import Store  # noqa: E402


@pytest.fixture
def backend(request, tmp_path, monkeypatch):
    state, setup, owner, _, project, initial_bounds, _ = request.getfixturevalue("prepared")
    scopes = ["context:read", "authority:consume", "provenance:append", "notebook:read"]
    critic, credential = enroll(setup, owner, project, scopes)
    planner, recipient = enroll(setup, owner, project, scopes)
    actors = [
        Store(state).authenticate(headers["Authorization"].removeprefix("Bearer "))
        for headers in (critic, planner)
    ]
    bounds = {
        **deepcopy(initial_bounds),
        "subject_credential_id": credential["credential_id"],
        "actions": ["attempt:status", "authority:delegate"],
        "delegation_depth": 1,
    }
    parent = post(setup, owner, f"/projects/{project}/agent-authority-grants", {"bounds": bounds})
    child = post(
        setup,
        critic,
        f"/agent-authority-grants/{parent['id']}/delegate",
        {
            "expected_revision": 1,
            "expected_epoch": 1,
            "bounds": {
                **bounds,
                "subject_credential_id": recipient["credential_id"],
                "actions": ["attempt:status"],
                "delegation_depth": 0,
                "budget_cents": 0,
            },
        },
    )
    context = post(
        setup,
        owner,
        f"/projects/{project}/agent-contexts",
        {
            "credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "title": "synthetic public output fixture",
            "body": json.dumps(
                {
                    "schema_version": "tessera-public-harness-disclosure-v1",
                    "run_id": "fixture-critic-run",
                    "task_id": child["bounds"]["bindings"][0]["task_id"],
                    "context_digest": "a" * 64,
                    "skill_digest": "b" * 64,
                    "config_digest": "c" * 64,
                    "allowed_public_events": ["generated_output"],
                }
            ),
            "classification": "public",
            "disclosure_approved": True,
        },
    )
    output_pins = {
        "model_id": "included-fixture-model",
        "run_id": "fixture-critic-run",
        "session_id": "fixture-session",
        "harness_instance_id": "fixture-instance",
        "context_id": context["id"],
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "task_id": child["bounds"]["bindings"][0]["task_id"],
        "operation_id": None,
    }
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    admission = post(
        setup,
        owner,
        f"/projects/{project}/generated-output-grants",
        {
            "action": "generated-output:register",
            "output_kind": "critic_review",
            "credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "pins": output_pins,
            "public_output_admitted": True,
            "max_outputs": 1,
            "max_total_bytes": 1024,
            "expires_at": expiry,
        },
    )
    content = b"Synthetic public critic recommendation; human decision remains separate."
    registration = {
        "grant_id": admission["id"],
        "expected_revision": 1,
        "pins": output_pins,
        "source_event_id": "fixture-public-output-event",
        "source_sequence": 1,
        "output_kind": "critic_review",
        "visibility": "public",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }
    output = post(
        setup,
        critic,
        f"/projects/{project}/generated-outputs",
        registration,
        key="fixture-output-stable",
    )
    disclosure = post(
        setup,
        owner,
        f"/generated-outputs/{output['id']}/disclosures",
        {
            "credential_id": recipient["credential_id"],
            "destination": "openai:personal",
            "disclosure_approved": True,
            "expires_at": expiry,
        },
    )
    pins = {
        "family": "research-generated-v1",
        "output_id": output["id"],
        "disclosure_id": disclosure["id"],
        "actor_id": actors[0]["id"],
        "critic_credential_id": credential["credential_id"],
        "org_id": actors[0]["org_id"],
        "project_id": project,
        **output_pins,
        "source_event_id": registration["source_event_id"],
        "source_sequence": 1,
        "content_digest": output["content_digest"],
        "registration_grant_id": admission["id"],
        "registration_revision": 1,
        "output_kind": "critic_review",
    }
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    origin = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(create_app(state), log_level="critical", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                raise AssertionError("owned collector failed startup")
            time.sleep(0.01)
        assert server.started
        monkeypatch.setenv(
            "TESSERA_RESEARCH_A2A_FIXTURE_TOKEN", planner["Authorization"].removeprefix("Bearer ")
        )
        identities = {
            hashlib.sha256(name.encode()).hexdigest(): {
                "tenant": actor["org_id"],
                "agent_id": actor["id"],
                "project_id": project,
            }
            for name, actor in zip(("critic", "planner"), actors, strict=True)
        }
        config = {
            "public_url": "http://127.0.0.1:8771/",
            "control_url": origin,
            "control_token_env": "TESSERA_RESEARCH_A2A_FIXTURE_TOKEN",
            "database": str(tmp_path / "research-peer.sqlite"),
            "agent_id": actors[1]["id"],
            "credential_id": recipient["credential_id"],
            "destination": "openai:personal",
            "model": bounds["model"],
            "identities": identities,
        }

        def send(client, actor, operation, task_id="", message_id=None, change=None):
            value = {
                "operation": operation,
                "reference": output["id"],
                "authority": {
                    "grant_id": child["id"],
                    "revision": 1,
                    "epoch": 1,
                    "binding": child["bounds"]["bindings"][0],
                },
                "generated": {**pins, **(change or {})},
            }
            return client.post(
                "/",
                headers={"Authorization": "Bearer " + actor, "A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": "fixture-request",
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": message_id or operation,
                            "role": "ROLE_USER",
                            "taskId": task_id,
                            "parts": [{"data": value}],
                        }
                    },
                },
            ).json()

        yield {
            "state": state,
            "setup": setup,
            "owner": owner,
            "project": project,
            "critic": critic,
            "planner": planner,
            "actors": actors,
            "config": config,
            "send": send,
            "pins": pins,
            "registration": registration,
            "output": output,
            "disclosure": disclosure,
            "child": child,
            "origin": origin,
        }
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_real_registry_sdk_result_ack_restart_replay_and_metadata_outbox(backend):
    b = backend
    with TestClient(create_peer(b["config"])) as client:
        task = b["send"](client, "critic", "submit")["result"]["task"]
        result = b["send"](client, "critic", "result", task["id"])["result"]["task"]
        assert result["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert (
            result["artifacts"][0]["parts"][0]["data"]["registered_public_output"]["operation_id"]
            is None
        )
        assert "error" in b["send"](client, "critic", "consume", task["id"])
    # Registration rights are distinct from transport/read rights: revoking new
    # registration retains the immutable historical output and current disclosure.
    post(
        b["setup"],
        b["owner"],
        f"/generated-output-grants/{b['output']['grant_id']}/revoke",
        {"expected_revision": 1},
        expected=200,
    )
    post(
        b["setup"],
        b["critic"],
        f"/projects/{b['project']}/generated-outputs",
        b["registration"],
        key="fixture-output-stable",
        expected=409,
    )
    with TestClient(create_peer(b["config"])) as restarted:
        assert (
            b["send"](restarted, "critic", "result", task["id"])["result"]["task"]["id"]
            == task["id"]
        )
        ack = b["send"](restarted, "planner", "consume", task["id"])["result"]["task"]
        assert ack["status"]["state"] == "TASK_STATE_COMPLETED"
        assert b["send"](restarted, "planner", "consume", task["id"])["result"]["task"] == ack
    identity = {
        "tenant": b["actors"][0]["org_id"],
        "agent_id": b["actors"][0]["id"],
        "project_id": b["project"],
    }
    assert drain_peer_outbox(b["config"], identity, task["id"])["acknowledged"] == 5
    assert drain_peer_outbox(b["config"], identity, task["id"])["pending"] == 0
    store = A2AStore(b["config"]["database"])
    assert len(store.provenance_outbox(identity["tenant"], identity["agent_id"], task["id"])) == 5
    with httpx.Client(trust_env=False, timeout=5) as client:
        metadata = client.get(
            b["origin"] + f"/generated-outputs/{b['output']['id']}/metadata",
            params={"disclosure_id": b["disclosure"]["id"]},
            headers=b["planner"],
        )
        notebook = client.get(
            b["origin"] + f"/projects/{b['project']}/agent-notebook/export", headers=b["planner"]
        )
    assert metadata.status_code == 200 and "content_base64" not in metadata.json()
    assert notebook.status_code == 200
    transport = [
        row for row in notebook.json()["provenance"] if row["remote"]["task_id"] == task["id"]
    ]
    assert len(transport) == 5
    assert all(row["actor_principal_id"] == b["actors"][1]["id"] for row in transport)
    assert "content_base64" not in json.dumps(
        store.get(identity["tenant"], identity["agent_id"], task["id"])
    )
    with sqlite3.connect(b["state"] / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM generated_public_outputs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM generated_output_grants").fetchone()[0] == 1


def test_real_lookup_denies_every_mismatched_research_pin(backend):
    b = backend
    fields = [
        "output_id",
        "disclosure_id",
        "actor_id",
        "critic_credential_id",
        "org_id",
        "project_id",
        "model_id",
        "run_id",
        "session_id",
        "harness_instance_id",
        "context_id",
        "context_digest",
        "skill_digest",
        "config_digest",
        "task_id",
        "operation_id",
        "source_event_id",
        "source_sequence",
        "content_digest",
        "registration_grant_id",
        "registration_revision",
        "output_kind",
    ]
    with TestClient(create_peer(b["config"])) as client:
        for field in fields:
            old = b["pins"][field]
            value = (
                old + 1
                if isinstance(old, int)
                else "sha256:" + "f" * 64
                if field == "content_digest"
                else "f" * 64
                if field.endswith("digest")
                else "mismatch"
            )
            assert "error" in b["send"](client, "critic", "submit", change={field: value}), field
    assert (
        A2AStore(b["config"]["database"]).list(b["actors"][0]["org_id"], b["actors"][0]["id"])[1]
        == 0
    )


@pytest.mark.parametrize("revoke", ["disclosure", "transport"])
def test_real_revocation_after_result_blocks_ack_and_preserves_pending(backend, revoke):
    b = backend
    with TestClient(create_peer(b["config"])) as client:
        task = b["send"](client, "critic", "submit")["result"]["task"]
        b["send"](client, "critic", "result", task["id"])
        route = (
            f"/generated-output-disclosures/{b['disclosure']['id']}/revoke"
            if revoke == "disclosure"
            else f"/agent-authority-grants/{b['child']['id']}/revoke"
        )
        body = (
            {"expected_revision": 1}
            if revoke == "disclosure"
            else {"expected_revision": 1, "expected_epoch": 1}
        )
        post(b["setup"], b["owner"], route, body, expected=200)
        assert "error" in b["send"](client, "planner", "consume", task["id"])
    store = A2AStore(b["config"]["database"])
    task = store.get(b["actors"][0]["org_id"], b["actors"][0]["id"], task["id"])
    assert task["metadata"]["generated_delivery_state"] == "awaiting_planner_consumption"
    assert (
        len(store.provenance_outbox(b["actors"][0]["org_id"], b["actors"][0]["id"], task["id"]))
        == 4
    )


def test_real_research_cancel_and_expired_recipient_disclosure_fail_closed(backend):
    b = backend
    with TestClient(create_peer(b["config"])) as client:
        task = b["send"](client, "critic", "submit")["result"]["task"]
        b["send"](client, "critic", "result", task["id"])
        response = client.post(
            "/",
            headers={"Authorization": "Bearer critic", "A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "cancel",
                "method": "CancelTask",
                "params": {"id": task["id"]},
            },
        ).json()
        assert response["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        assert "error" in b["send"](client, "planner", "consume", task["id"])
        expires = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        disclosure = post(
            b["setup"],
            b["owner"],
            f"/generated-outputs/{b['output']['id']}/disclosures",
            {
                "credential_id": b["config"]["credential_id"],
                "destination": b["config"]["destination"],
                "disclosure_approved": True,
                "expires_at": expires,
            },
        )
        time.sleep(1.1)
        assert "error" in b["send"](
            client,
            "critic",
            "submit",
            message_id="expired-submit",
            change={"disclosure_id": disclosure["id"]},
        )
    store = A2AStore(b["config"]["database"])
    assert store.list(b["actors"][0]["org_id"], b["actors"][0]["id"])[1] == 1
    assert (
        len(store.provenance_outbox(b["actors"][0]["org_id"], b["actors"][0]["id"], task["id"]))
        == 5
    )


def capture_registered_source(b, directory):
    from tessera_agent.provenance_client import ProvenanceClient
    from tessera_control.public_capture import PublicCaptureAdapter
    from tessera_control.public_capture_outbox import PublicCaptureOutbox

    p = b["pins"]
    author_disclosure = post(
        b["setup"],
        b["owner"],
        f"/generated-outputs/{p['output_id']}/disclosures",
        {
            "credential_id": p["critic_credential_id"],
            "destination": "openai:personal",
            "disclosure_approved": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    binding = {
        key: p[key]
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
            "org_id",
            "project_id",
        )
    }
    binding.update(
        schema_version="tessera-public-capture-binding-v1",
        actor_principal_id=p["actor_id"],
        credential_id=p["critic_credential_id"],
        destination_digest="sha256:" + hashlib.sha256(b"openai:personal").hexdigest(),
        allowed_public_events=["generated_output"],
    )
    with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as reader:

        def context(bound):
            result = reader.get(
                b["origin"] + "/agent-contexts/" + p["context_id"], headers=b["critic"]
            )
            assert result.status_code == 200
            row = result.json()
            return {
                **{
                    key: bound[key]
                    for key in (
                        "context_id",
                        "org_id",
                        "project_id",
                        "credential_id",
                        "destination_digest",
                    )
                },
                "issuer_role": "human",
                "disclosure_approved": row["disclosure_approved"],
                "classification": row["classification"],
                "body": row["body"],
            }

        def output(ident, bound):
            result = reader.get(
                b["origin"] + f"/generated-outputs/{ident}/metadata",
                params={"disclosure_id": author_disclosure["id"]},
                headers=b["critic"],
            )
            assert result.status_code == 200
            row = result.json()
            return {
                "family": "generated_public_research_output_v1",
                "output_id": row["id"],
                "artifact_id": row["artifact_id"],
                "disclosure_id": author_disclosure["id"],
                "output_kind": row["output_kind"],
                "writer_actor_id": row["actor_id"],
                "writer_credential_id": row["credential_id"],
                "writer_org_id": row["org_id"],
                "binding": bound,
                "source_event_id": row["source_event_id"],
                "source_sequence": row["source_sequence"],
                "operation_id": row["pins"]["operation_id"],
                "sha256": row["sha256"],
            }

        adapter = PublicCaptureAdapter(binding, context, output)
        observation = {
            key: p[key]
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
                "operation_id",
                "source_event_id",
                "source_sequence",
            )
        }
        observation.update(
            schema_version="tessera-normalized-public-observation-v1",
            public_event="generated_output",
            source_watermark=1,
            capture_state="bounded_public_stream",
            gaps=[],
            outcome="succeeded",
            registered_output_id=p["output_id"],
        )
        box = PublicCaptureOutbox(directory, adapter)
        rid = box.capture(observation)
        box.close()
        box = PublicCaptureOutbox(directory, adapter)
        try:
            with ProvenanceClient(
                b["origin"],
                b["critic"]["Authorization"].removeprefix("Bearer "),
                "openai:personal",
                expected_actor={
                    key: binding[key] for key in ("org_id", "actor_principal_id", "credential_id")
                },
            ) as client:
                receipt = box.deliver_next(client)
                assert receipt["delivery"] == "accepted" and receipt["record_id"] == rid
                envelope = adapter.envelope(observation)
                forged_author = b["setup"].post(
                    f"/projects/{b['project']}/agent-provenance",
                    headers={**b["planner"], "Idempotency-Key": "wrong-receiver-author"},
                    json=envelope,
                )
                assert forged_author.status_code == 403
                replay = client.append(
                    b["project"],
                    envelope["record"],
                    envelope["grant_proof"],
                    idempotency_key="provenance-" + hashlib.sha256(rid.encode()).hexdigest(),
                )
                assert (
                    replay.status == "accepted" and replay.sequence == receipt["collector_sequence"]
                )
            assert box.watermarks()["acknowledged_source_prefix"] == 1
        finally:
            box.close()
    p.update(capture_record_id=rid, capture_sequence=receipt["collector_sequence"])
    return author_disclosure


def test_typed_author_capture_real_collector_sdk_ack_and_independent_recipient_projection(
    backend, tmp_path
):
    b = backend
    author_disclosure = capture_registered_source(b, tmp_path / "capture")
    with TestClient(create_peer(b["config"])) as client:
        for change in (
            {"capture_record_id": "not-registered"},
            {"capture_sequence": b["pins"]["capture_sequence"] + 1},
        ):
            assert "error" in b["send"](client, "critic", "submit", change=change)
        task = b["send"](client, "critic", "submit")["result"]["task"]
        b["send"](client, "critic", "result", task["id"])
    with TestClient(create_peer(b["config"])) as restarted:
        ack = b["send"](restarted, "planner", "consume", task["id"])["result"]["task"]
        assert ack["status"]["state"] == "TASK_STATE_COMPLETED"
        assert b["send"](restarted, "planner", "consume", task["id"])["result"]["task"] == ack
        identity = {
            "tenant": b["actors"][0]["org_id"],
            "agent_id": b["actors"][0]["id"],
            "project_id": b["project"],
        }
        assert drain_peer_outbox(b["config"], identity, task["id"])["acknowledged"] == 5
        assert drain_peer_outbox(b["config"], identity, task["id"])["pending"] == 0
        with httpx.Client(trust_env=False, timeout=5) as reader:
            result = reader.get(
                b["origin"] + f"/projects/{b['project']}/agent-notebook/export",
                headers=b["planner"],
            )
            assert result.status_code == 200
            records = result.json()["provenance"]
            registered_event = next(
                e
                for e in result.json()["control_events"]
                if e["event_type"] == "generated_output.registered"
            )
            assert registered_event["output"]["output_id"] == b["pins"]["output_id"]
            assert (
                registered_event["output"]["recipient_disclosure_id"] == b["pins"]["disclosure_id"]
            )
            captured = next(r for r in records if r["record_id"] == b["pins"]["capture_record_id"])
            assert captured["actor_principal_id"] == b["actors"][0]["id"]
            assert (
                captured["generated_output"]["writer_credential_id"]
                == b["pins"]["critic_credential_id"]
            )
            assert captured["generated_output"]["disclosure_id"] == author_disclosure["id"]
            assert captured["output"]["recipient_disclosure_id"] == b["pins"]["disclosure_id"]
            assert captured["output"]["output_id"] == b["pins"]["output_id"]
            assert captured["content_digest"] == b["pins"]["content_digest"]
            transport = [r for r in records if r["remote"]["task_id"] == task["id"]]
            assert (
                len(transport) == 5
                and len([r for r in records if r["record_id"] == captured["record_id"]]) == 1
            )
            assert all(
                r["actor_principal_id"] == b["actors"][1]["id"]
                and r["references"]["provenance_record_id"] == captured["record_id"]
                and r.get("generated_output") is None
                for r in transport
            )
            assert "content_base64" not in json.dumps(records)
        post(
            b["setup"],
            b["owner"],
            f"/generated-output-disclosures/{b['disclosure']['id']}/revoke",
            {"expected_revision": 1},
            expected=200,
        )
        assert "error" in b["send"](restarted, "planner", "consume", task["id"])
        hidden = (
            b["setup"]
            .get(f"/projects/{b['project']}/agent-notebook/export", headers=b["planner"])
            .json()
        )
        withheld = next(r for r in hidden["provenance"] if r["record_id"] == captured["record_id"])
        assert "generated_output" not in withheld and withheld["content_digest"] is None
        assert withheld["output"]["visibility"] == "withheld_current_disclosure"
