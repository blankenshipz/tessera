"""Actual SDK ClientSession/JSON-RPC memory transport and actual FastAPI/SQLite.

Public bytes/account/model claims are fixtures, not inference or stdio/host proof.
Human setup/revoke/disclosure use separately authenticated ordinary backend API.
"""

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_generated_outputs import output_setup as output_setup

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from tessera_mcp.client import ControlClient, ControlError  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.generated_outputs import decode_public_bytes, output_path  # noqa: E402
from tessera_mcp.harness_binding import PERMITTED_TOOLS, catalog_sha256  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def adapter(setup, actor, tmp_path):
    _, http, _, project, *_ = setup
    reference = tmp_path / "synthetic-agent-token"
    reference.write_text(actor["Authorization"].split("Bearer ", 1)[1])
    reference.chmod(0o600)
    return create_server(
        ControlClient(
            Config("http://127.0.0.1:8765", reference, project, actor["X-Agent-Destination"]),
            httpx.ASGITransport(app=http.app),
        )
    )


async def call(session, tool, arguments, denied=False):
    result = await session.call_tool(tool, arguments)
    assert bool(result.isError) == denied
    return result if denied else result.structuredContent


def disclose(setup, output_id):
    _, http, owner, _, _, _, recipient, _, _ = setup
    result = http.post(
        f"/generated-outputs/{output_id}/disclosures",
        headers={**owner, "Idempotency-Key": "human-disclose"},
        json={
            "credential_id": recipient["credential_id"],
            "destination": "openai:personal",
            "disclosure_approved": True,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
    )
    assert result.status_code == 201
    return result.json()


def test_sdk_registration_reconnect_disclosure_metadata_read_revoke(output_setup, tmp_path):
    state, http, owner, _, sender, receiver, _, grant, body = output_setup

    async def journey():
        async with create_connected_server_and_client_session(
            adapter(output_setup, sender, tmp_path)
        ) as sdk:
            tools = (await sdk.list_tools()).tools
            assert {tool.name for tool in tools} == set(PERMITTED_TOOLS)
            assert len(catalog_sha256([tool.model_dump(mode="json") for tool in tools])) == 64
            schema = next(
                tool.inputSchema
                for tool in tools
                if tool.name == "tessera_register_generated_output"
            )
            assert schema["$defs"]["RegisterGeneratedOutput"]["additionalProperties"] is False
            assert "content_base64" in schema["$defs"]["RegisterGeneratedOutput"]["required"]
            assert "context_digest" in schema["$defs"]["OutputPins"]["required"]
            registered = await call(
                sdk,
                "tessera_register_generated_output",
                {"payload": body, "idempotency_key": "persistent-public-source"},
            )
            assert "content_base64" not in registered
            assert registered["execution_established"] is False
            await call(sdk, "tessera_issue_generated_output_grant", {}, denied=True)
            await call(sdk, "tessera_disclose_generated_output", {}, denied=True)
            await call(sdk, "tessera_revoke_generated_output", {}, denied=True)
        async with create_connected_server_and_client_session(
            adapter(output_setup, sender, tmp_path)
        ) as sdk:
            assert (
                await call(
                    sdk,
                    "tessera_register_generated_output",
                    {"payload": body, "idempotency_key": "persistent-public-source"},
                )
                == registered
            )
            await call(
                sdk,
                "tessera_register_generated_output",
                {
                    "payload": {**body, "source_event_id": "changed"},
                    "idempotency_key": "persistent-public-source",
                },
                denied=True,
            )
            await call(
                sdk,
                "tessera_read_generated_output",
                {"output_id": registered["id"], "disclosure_id": "not-issued"},
                denied=True,
            )
        disclosure = disclose(output_setup, registered["id"])
        async with create_connected_server_and_client_session(
            adapter(output_setup, sender, tmp_path)
        ) as sdk:
            author_args = {"output_id": registered["id"], "disclosure_id": disclosure["id"]}
            await call(sdk, "tessera_read_generated_output", author_args, denied=True)
            await call(sdk, "tessera_generated_output_metadata", author_args, denied=True)
        async with create_connected_server_and_client_session(
            adapter(output_setup, receiver, tmp_path)
        ) as sdk:
            args = {"output_id": registered["id"], "disclosure_id": disclosure["id"]}
            metadata = await call(sdk, "tessera_generated_output_metadata", args)
            assert "content_base64" not in metadata
            read = await call(sdk, "tessera_read_generated_output", args)
            assert read["content_base64"] == body["content_base64"]
            assert read["content_digest"] == "sha256:" + body["sha256"]
            assert read["pins"] == body["pins"]
            response = http.post(
                f"/generated-output-disclosures/{disclosure['id']}/revoke",
                headers={**owner, "Idempotency-Key": "human-withdraw"},
                json={"expected_revision": 1},
            )
            assert response.status_code == 200
            await call(sdk, "tessera_read_generated_output", args, denied=True)
            await call(sdk, "tessera_generated_output_metadata", args, denied=True)
        response = http.post(
            f"/generated-output-grants/{grant['id']}/revoke",
            headers={**owner, "Idempotency-Key": "human-revoke"},
            json={"expected_revision": 1},
        )
        assert response.status_code == 200
        async with create_connected_server_and_client_session(
            adapter(output_setup, sender, tmp_path)
        ) as sdk:
            await call(
                sdk,
                "tessera_register_generated_output",
                {"payload": body, "idempotency_key": "persistent-public-source"},
                denied=True,
            )

    asyncio.run(journey())
    import sqlite3

    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM generated_public_outputs").fetchone()[0] == 1
        event = db.execute(
            "SELECT payload_json FROM events WHERE event_type='generated_output.registered'"
        ).fetchone()[0]
        assert "content_base64" not in event
        assert body["source_event_id"] in event
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change", ["wrong_pin", "raw_prompt", "hidden", "digest", "file", "destination"]
)
def test_sdk_typed_and_backend_registration_denials(output_setup, tmp_path, change):
    _, _, _, _, sender, *_ = output_setup
    body = json.loads(json.dumps(output_setup[-1]))
    if change == "wrong_pin":
        body["pins"]["config_digest"] = "0" * 64
    elif change == "raw_prompt":
        body["raw_prompt"] = "fixture-must-never-be-logged"
    elif change == "hidden":
        body["visibility"] = "hidden_reasoning"
    elif change == "digest":
        body["sha256"] = "0" * 64
    elif change == "file":
        body["path"] = "/arbitrary/host/file"
    else:
        sender = {**sender, "X-Agent-Destination": "different"}

    async def check():
        async with create_connected_server_and_client_session(
            adapter(output_setup, sender, tmp_path)
        ) as sdk:
            await call(
                sdk,
                "tessera_register_generated_output",
                {"payload": body, "idempotency_key": "denied-source"},
                denied=True,
            )

    asyncio.run(check())


@pytest.mark.parametrize("content", [b"", b"x" * 65537, b"\xff", b"public\x00unsafe"])
def test_local_byte_fences(content):
    with pytest.raises(ControlError, match="bytes/size/digest invalid"):
        decode_public_bytes(base64.b64encode(content).decode(), hashlib.sha256(content).hexdigest())


def test_output_ids_are_not_paths_or_arbitrary_forwarding():
    for output, disclosure in [("../credentials", "disclosure"), ("output", ""), ("output", "a/b")]:
        with pytest.raises(ControlError):
            output_path(output, disclosure)
    assert (
        output_path("output", "disclosure", metadata=True)
        == "/generated-outputs/output/metadata?disclosure_id=disclosure"
    )


def test_maximum_public_utf8_bytes_are_exactly_bounded():
    content = b"p" * 65536
    assert (
        decode_public_bytes(
            base64.b64encode(content).decode(), hashlib.sha256(content).hexdigest(), size=65536
        )
        == content
    )
    with pytest.raises(ControlError):
        decode_public_bytes(
            base64.b64encode(content).decode(), hashlib.sha256(content).hexdigest(), size=65535
        )
