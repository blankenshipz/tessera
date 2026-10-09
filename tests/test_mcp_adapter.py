"""Isolated synthetic adapter checks; live protocol/API acceptance is a separate script."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp")

from tessera_mcp.client import ControlClient, ControlError  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402


def config(tmp_path):
    path = tmp_path / "token"
    path.write_text("synthetic-agent-token")
    path.chmod(0o600)
    return Config("http://127.0.0.1:8765", path, "project", "test:destination")


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example.org",
        "https://user:secret@example.org",
        "https://example.org/path",
        "https://example.org?token=secret",
        "file:///private/token",
    ],
)
def test_endpoint_rejects_unsafe_origins(tmp_path, url):
    with pytest.raises(ValueError):
        Config(url, tmp_path / "token", "project", "destination")


def test_token_file_is_private_no_symlinks_and_reopened(tmp_path):
    cfg = config(tmp_path)
    assert cfg.token() == "synthetic-agent-token"
    cfg.token_file.write_text("replacement-agent-token")
    assert cfg.token() == "replacement-agent-token"
    cfg.token_file.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        cfg.token()
    link = tmp_path / "link"
    link.symlink_to(cfg.token_file)
    with pytest.raises(ValueError, match="unavailable"):
        Config(cfg.endpoint, link, "project", "destination").token()


def test_exact_scope_destination_and_idempotency_sent(tmp_path):
    cfg = config(tmp_path)
    observed = []

    def receive(request):
        observed.append(request)
        return httpx.Response(200, json={"authority_effect": "none"})

    client = ControlClient(cfg, httpx.MockTransport(receive))
    body = {"title": "synthetic"}
    asyncio.run(client.propose(body, {"client": "synthetic"}, "stable-key"))
    request = observed[0]
    assert request.url.path == "/projects/project/agent-proposals"
    assert request.headers["authorization"] == "Bearer synthetic-agent-token"
    assert request.headers["x-agent-destination"] == "test:destination"
    assert request.headers["idempotency-key"] == "stable-key"
    assert json.loads(request.content)["plan"] == body


@pytest.mark.parametrize("status", [301, 401, 403, 404, 409, 422, 500])
def test_error_details_are_not_disclosed_or_retried(tmp_path, status):
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "protected-secret-marker"})

    client = ControlClient(config(tmp_path), httpx.MockTransport(receive))
    with pytest.raises(ControlError) as exc:
        asyncio.run(client.status())
    assert "protected-secret-marker" not in str(exc.value)
    assert str(status) in str(exc.value)
    assert len(calls) == 1


def test_invalid_key_and_pagination_never_reach_api(tmp_path):
    client = ControlClient(config(tmp_path))
    for key in ["", "x" * 129, "a\nb", "has space"]:
        with pytest.raises(ControlError):
            asyncio.run(client.propose({}, {}, key))
    with pytest.raises(ControlError):
        asyncio.run(client.contexts(-1, 1))
    with pytest.raises(ControlError):
        asyncio.run(client.proposals(0, 101))


def test_tools_resources_and_typed_execution_spec(tmp_path):
    server = create_server(ControlClient(config(tmp_path)))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} >= {
        "tessera_status",
        "tessera_list_contexts",
        "tessera_read_context",
        "tessera_list_proposals",
        "tessera_read_proposal",
        "tessera_propose_task_plan",
    }
    proposal = next(tool for tool in tools if tool.name == "tessera_propose_task_plan")
    schema = proposal.inputSchema
    assert "execution_spec" in schema["required"]
    assert "input_snapshot_digest" in schema["$defs"]["ExecutionSpec"]["required"]
    assert proposal.annotations.idempotentHint is True
    assert len(asyncio.run(server.list_resources())) == 1
    assert len(asyncio.run(server.list_resource_templates())) == 2


def test_portable_skill_does_not_claim_new_protocol():
    text = (Path(__file__).parents[1] / "skills/tessera-research-bench/SKILL.md").read_text()
    assert text.startswith("---\nname: tessera-research-bench\n")
    assert "2026-07-28 stateless protocol is not implemented" in text
    assert "external_agent_caller_attested" in text


def test_cli_help_without_configuration_or_credentials():
    env = {key: value for key, value in os.environ.items() if not key.startswith("TESSERA_")}
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "tessera_mcp.server", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert "usage: tessera-mcp" in result.stdout
    assert "TESSERA_AGENT_TOKEN_FILE" in result.stdout
    assert "TESSERA_CONTROL_CA_FILE" in result.stdout
    assert "credential reference invalid" not in result.stderr


@pytest.mark.parametrize("size", [65536, 65537])
def test_artifact_serialization_bound(tmp_path, size):
    content = b"x" * size
    context = {
        "content_base64": base64.b64encode(content).decode(),
        "size": size,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    client = ControlClient(
        config(tmp_path), httpx.MockTransport(lambda request: httpx.Response(200, json=context))
    )
    if size == 65536:
        assert asyncio.run(client.context("context")) == context
    else:
        with pytest.raises(ControlError, match="oversized"):
            asyncio.run(client.context("context"))


def test_context_json_response_bound(tmp_path):
    client = ControlClient(
        config(tmp_path),
        httpx.MockTransport(lambda request: httpx.Response(200, json={"body": "x" * (128 * 1024)})),
    )
    with pytest.raises(ControlError, match="serialization bound"):
        asyncio.run(client.context("context"))


def test_explicit_public_ca_reference_preserves_verified_tls(tmp_path, monkeypatch):
    import ssl

    cfg = config(tmp_path)
    ca = tmp_path / "public-ca.pem"
    monkeypatch.setenv("TESSERA_CONTROL_URL", "https://control.example.org")
    monkeypatch.setenv("TESSERA_AGENT_TOKEN_FILE", str(cfg.token_file))
    monkeypatch.setenv("TESSERA_PROJECT_ID", cfg.project_id)
    monkeypatch.setenv("TESSERA_AGENT_DESTINATION", cfg.destination)
    monkeypatch.setenv("TESSERA_CONTROL_CA_FILE", str(ca))
    selected = Config.from_env()
    assert selected.ca_file == ca
    observed = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def trust_reference(*, cafile):
        observed.append(cafile)
        return context

    monkeypatch.setattr("tessera_mcp.client.ssl.create_default_context", trust_reference)
    client = ControlClient(selected, httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert asyncio.run(client.status()) == {}
    assert observed == [str(ca)]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
