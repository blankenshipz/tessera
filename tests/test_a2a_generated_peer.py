"""Executable official SDK routes; synthetic control registry, no model/live peers."""

import hashlib
import json

import httpx
import pytest
from test_a2a_generated_store import ORIGIN, pins

pytest.importorskip("a2a", reason="optional official A2A SDK required")

from a2a.utils.errors import InvalidParamsError  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from test_a2a_peer import envelope as legacy_envelope  # noqa: E402

from tessera_control.a2a_peer import create_peer  # noqa: E402
from tessera_control.a2a_store import A2AStore  # noqa: E402


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    p = pins()
    harness = {
        key: p[key]
        for key in (
            "task_id",
            "context_id",
            "run_id",
            "session_id",
            "harness_instance_id",
            "context_digest",
            "skill_digest",
            "config_digest",
            "source_event_id",
            "source_sequence",
        )
    }
    harness.update(
        schema_version="tessera-public-harness-metadata-v1",
        public_event="generated_output",
        capture_state="bounded_public_stream",
        source_watermark=1,
        gaps=[],
    )
    record = {
        "record_id": p["record_id"],
        "sequence": 5,
        "actor_principal_id": "critic",
        "credential_id": p["critic_credential_id"],
        "model_id": p["model_id"],
        "kind": "attempt",
        "outcome": "succeeded",
        "content_digest": p["content_digest"],
        "harness": harness,
        "references": {
            "task_id": p["task_id"],
            "context_id": p["context_id"],
            "artifact_id": p["artifact_id"],
            "operation_id": p["operation_id"],
            "output_manifest_id": p["manifest_id"],
        },
    }
    context = {
        "id": p["recipient_context_id"],
        "project_id": "project",
        "credential_id": ORIGIN["credential_id"],
        "destination": ORIGIN["destination"],
        "disclosure_approved": True,
        "artifact_id": p["artifact_id"],
        "manifest_id": p["manifest_id"],
        "expected_manifest_digest": p["manifest_digest"],
        "sha256": "d" * 64,
        "size": 20,
        "content_base64": "unsafe-content-must-not-enter-peer-ledger",
    }
    state = {"record": record, "context": context, "revoked": False, "available": True, "calls": []}

    def control(request):
        state["calls"].append(request.url.path)
        assert request.headers["Authorization"] == "Bearer fixture-control"
        assert request.headers["X-Agent-Destination"] == ORIGIN["destination"]
        if not state["available"]:
            return httpx.Response(404)
        if request.url.path.endswith("agent-notebook"):
            assert (
                request.url.params["after_sequence"] == "4" and request.url.params["limit"] == "1"
            )
            return httpx.Response(200, json={"provenance": [state["record"]]})
        return httpx.Response(200, json=state["context"])

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(control), **kwargs),
    )
    monkeypatch.setenv("TESSERA_GENERATED_TEST_TOKEN", "fixture-control")
    config = {
        "public_url": "http://127.0.0.1:8771/",
        "control_url": "http://127.0.0.1:8765",
        "database": str(tmp_path / "peer.sqlite"),
        "agent_id": "planner",
        "credential_id": ORIGIN["credential_id"],
        "destination": ORIGIN["destination"],
        "control_token_env": "TESSERA_GENERATED_TEST_TOKEN",
        "identities": {
            hashlib.sha256(actor.encode()).hexdigest(): {
                "tenant": "tenant",
                "agent_id": actor,
                "project_id": "project",
            }
            for actor in ("critic", "planner", "stranger")
        },
    }

    async def authority(identity, fence):
        if state["revoked"] or identity["agent_id"] != "critic":
            raise InvalidParamsError("synthetic delegated grant denied")
        assert fence["binding"]["task_id"] == "central-task"

    def send(client, actor, operation, task_id="", message_id=None, generated=None):
        envelope = legacy_envelope()
        envelope["authority"]["binding"]["task_id"] = "central-task"
        envelope.update(operation=operation, reference=p["artifact_id"], generated=generated or p)
        return client.post(
            "/",
            headers={"Authorization": "Bearer " + actor, "A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "request",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": message_id or operation,
                        "role": "ROLE_USER",
                        "taskId": task_id,
                        "parts": [{"data": envelope}],
                    }
                },
            },
        ).json()

    return state, config, authority, send


def test_sdk_registered_result_planner_ack_restart_replay_and_no_embedded_content(sdk):
    state, config, authority, send = sdk
    with TestClient(create_peer(config, authority)) as client:
        task = send(client, "critic", "submit")["result"]["task"]
        result = send(client, "critic", "result", task["id"])["result"]["task"]
        assert result["metadata"]["generated_delivery_state"] == "awaiting_planner_consumption"
        assert result["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert "error" in send(client, "critic", "consume", task["id"])
        assert "error" in send(client, "stranger", "consume", task["id"])
    with TestClient(create_peer(config, authority)) as restarted:
        observed = restarted.post(
            "/",
            headers={"Authorization": "Bearer planner", "A2A-Version": "1.0"},
            json={"jsonrpc": "2.0", "id": "get", "method": "GetTask", "params": {"id": task["id"]}},
        ).json()
        assert observed["result"]["id"] == task["id"]
        ack = send(restarted, "planner", "consume", task["id"])["result"]["task"]
        assert ack["status"]["state"] == "TASK_STATE_COMPLETED"
        assert ack["metadata"]["planner_consumption"]["planner_id"] == "planner"
        assert send(restarted, "planner", "consume", task["id"])["result"]["task"] == ack
        state["revoked"] = True
        assert "error" in send(restarted, "planner", "consume", task["id"])
    store = A2AStore(config["database"])
    assert len(store.provenance_outbox("tenant", "critic", task["id"])) == 5
    assert "unsafe-content-must-not-enter-peer-ledger" not in json.dumps(
        store.get("tenant", "critic", task["id"])
    )
    assert state["calls"]


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("record", "record_id", "another"),
        ("record", "actor_principal_id", "other"),
        ("record", "credential_id", "another"),
        ("record", "model_id", None),
        ("record", "content_digest", "sha256:" + "f" * 64),
        ("record", "sequence", 6),
        ("record", "outcome", "unknown"),
        ("record", "kind", "message"),
        ("harness", "public_event", "message"),
        ("harness", "capture_state", "unknown"),
        ("harness", "session_id", "another"),
        ("harness", "run_id", "another"),
        ("harness", "config_digest", "f" * 64),
        ("harness", "context_digest", "f" * 64),
        ("harness", "skill_digest", "f" * 64),
        ("harness", "context_id", "another"),
        ("refs", "operation_id", "another"),
        ("refs", "output_manifest_id", "another"),
        ("refs", "artifact_id", "another"),
        ("context", "disclosure_approved", False),
        ("context", "credential_id", "other"),
        ("context", "project_id", "other"),
        ("context", "destination", "other"),
        ("context", "artifact_id", "other"),
        ("context", "sha256", "f" * 64),
        ("context", "manifest_id", "other"),
        ("context", "expected_manifest_digest", "f" * 64),
        ("context", "size", 65537),
    ],
)
def test_unregistered_mismatched_not_public_or_withdrawn_fails_closed(sdk, target, key, value):
    state, config, authority, send = sdk
    item = (
        state["record"]["harness"]
        if target == "harness"
        else state["record"]["references"]
        if target == "refs"
        else state[target]
    )
    item[key] = value
    with TestClient(create_peer(config, authority)) as client:
        assert "error" in send(client, "critic", "submit")
    assert A2AStore(config["database"]).list("tenant", "critic")[1] == 0


def test_unavailable_registration_and_postdelivery_withdrawal_block_ack(sdk):
    state, config, authority, send = sdk
    with TestClient(create_peer(config, authority)) as client:
        state["available"] = False
        assert "error" in send(client, "critic", "submit")
        state["available"] = True
        task = send(client, "critic", "submit")["result"]["task"]
        send(client, "critic", "result", task["id"])
        state["context"]["disclosure_approved"] = False
        assert "error" in send(client, "planner", "consume", task["id"])
    assert (
        A2AStore(config["database"]).get("tenant", "critic", task["id"])["metadata"][
            "generated_delivery_state"
        ]
        == "awaiting_planner_consumption"
    )


def test_generated_cancel_through_sdk_prevents_planner_ack(sdk):
    _, config, authority, send = sdk
    with TestClient(create_peer(config, authority)) as client:
        task = send(client, "critic", "submit")["result"]["task"]
        send(client, "critic", "result", task["id"])
        canceled = client.post(
            "/",
            headers={"Authorization": "Bearer critic", "A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "cancel",
                "method": "CancelTask",
                "params": {"id": task["id"]},
            },
        ).json()
        assert canceled["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        assert "error" in send(client, "planner", "consume", task["id"])
