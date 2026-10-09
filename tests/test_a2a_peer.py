"""SDK-local adapter checks; injected verifier is synthetic, not live authority proof."""

import hashlib

import pytest

pytest.importorskip("a2a", reason="optional pinned A2A SDK is not installed")

from a2a.types import a2a_pb2 as pb  # noqa: E402
from a2a.utils.errors import InvalidParamsError  # noqa: E402
from google.protobuf.json_format import ParseDict  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from tessera_control.a2a_peer import create_peer, envelope_from  # noqa: E402


def envelope():
    return {
        "operation": "submit",
        "reference": "context-reference",
        "authority": {
            "grant_id": "grant-1",
            "revision": 1,
            "epoch": 1,
            "binding": {
                "variant_id": "variant-1",
                "expected_variant_revision": 1,
                "expected_design_revision": 1,
                "execution_grant_id": "execution-1",
                "expected_execution_grant_revision": 1,
                "expected_execution_epoch": 1,
                "task_id": "task-1",
                "expected_task_revision": 1,
                "plan_digest": "a" * 64,
                "target_digest": "b" * 64,
            },
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "must not persist"},
        {"reference": "https://not-a-reference?secret=x"},
        {"authority": {"grant_id": "../escape", "revision": 1, "epoch": 1, "binding": {}}},
    ],
)
def test_reject_prompt_url_and_authority_path(change):
    message = ParseDict({"parts": [{"data": {**envelope(), **change}}]}, pb.Message())
    with pytest.raises(InvalidParamsError):
        envelope_from(message)


def test_sdk_route_auth_tenant_replay_and_revoked_verifier(tmp_path):
    revoked = False

    async def synthetic_verifier(identity, fence):
        if revoked:
            raise InvalidParamsError("fixture revoked")

    config = {
        "public_url": "http://127.0.0.1:8771/",
        "control_url": "http://127.0.0.1:8765",
        "database": str(tmp_path / "peer.db"),
        "agent_id": "critic",
        "identities": {
            hashlib.sha256(b"fixture").hexdigest(): {
                "tenant": "tenant",
                "agent_id": "agent",
                "project_id": "project",
            }
        },
    }
    with TestClient(create_peer(config, synthetic_verifier)) as client:
        card = client.get("/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert not card["capabilities"].get("streaming", False)
        request = {
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "message-1",
                    "role": "ROLE_USER",
                    "parts": [{"data": envelope()}],
                }
            },
        }
        assert client.post("/", json=request).status_code == 401
        client.headers.update({"Authorization": "Bearer fixture", "A2A-Version": "1.0"})
        response = client.post("/", json=request).json()
        task = response["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert client.post("/", json=request).json()["result"]["task"]["id"] == task["id"]
        get = {
            "jsonrpc": "2.0",
            "id": "get",
            "method": "GetTask",
            "params": {"id": task["id"], "tenant": "other"},
        }
        assert "error" in client.post("/", json=get).json()
        revoked = True
        assert "error" in client.post("/", json=request).json()


def test_off_host_cleartext_rejected(tmp_path):
    with pytest.raises(ValueError, match="TLS"):
        create_peer({"public_url": "http://host.example/", "control_url": "http://127.0.0.1/"})


def test_empty_reference_rejected():
    with pytest.raises(InvalidParamsError):
        envelope_from(pb.Message())


def test_authenticated_body_limit_before_sdk_parsing(tmp_path):
    async def synthetic_verifier(identity, fence):
        raise AssertionError("oversized request must not reach verifier")

    config = {
        "public_url": "http://127.0.0.1:8771/",
        "control_url": "http://127.0.0.1:8765",
        "database": str(tmp_path / "peer.db"),
        "agent_id": "critic",
        "identities": {
            hashlib.sha256(b"fixture").hexdigest(): {
                "tenant": "tenant",
                "agent_id": "agent",
                "project_id": "project",
            }
        },
    }
    with TestClient(create_peer(config, synthetic_verifier)) as client:
        response = client.post(
            "/", headers={"Authorization": "Bearer fixture"}, content=b"x" * 32769
        )
        assert response.status_code == 413
