import asyncio
import json
import os
import stat
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer
from tessera_operator import salad


@pytest.fixture
def directories(tmp_path):
    # macOS /var is a symlink; the backend deliberately requires canonical paths.
    root = tmp_path.resolve()
    state, credentials = root / "state", root / "credentials"
    state.mkdir(mode=0o700)
    credentials.mkdir(mode=0o700)
    return state, credentials


def configuration(**updates):
    return salad.SaladConfiguration(
        **{
            "organization": "existing-org",
            "project": "existing-project",
            "management_host": "trusted.example.invalid",
            "credential_reference": "credential-ref:salad-test",
            "credential_version": 1,
            **updates,
        }
    )


def test_persist_cas_restart(directories):
    state, _ = directories
    store = salad.ConfigurationStore(state)
    first = store.configure(configuration(), None)
    assert first["state"] == "configured"
    assert (
        store.configure(configuration(project="different-project"), None)["reason"]
        == "stale_revision"
    )
    second = store.configure(configuration(project="different-project"), first["revision"])
    store.close()
    store = salad.ConfigurationStore(state)
    assert store.read().project == "different-project"
    assert salad._digest(store.read()) == second["revision"]
    assert stat.S_IMODE((state / "salad.json").stat().st_mode) == 0o600
    store.close()


@pytest.mark.parametrize(
    "update",
    [
        {"organization": "../escape"},
        {"project": "a?query=bad"},
        {"credential_reference": "raw-secret"},
        {"credential_version": True},
        {"management_host": "https://untrusted.invalid/path"},
        {"integration": "invented-provider"},
        {"endpoint": "https://untrusted.invalid"},
    ],
)
def test_closed_schema(update):
    with pytest.raises(ValidationError):
        configuration(**update)


def test_private_paths_symlinks_hardlinks(directories, tmp_path):
    state, _ = directories
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    with pytest.raises(OSError):
        salad.ConfigurationStore(alias)
    state.chmod(0o755)
    with pytest.raises(ValueError):
        salad.ConfigurationStore(state)
    state.chmod(0o700)
    store = salad.ConfigurationStore(state)
    store.configure(configuration(), None)
    os.link(state / "salad.json", state / "other")
    with pytest.raises(ValueError):
        store.read()
    store.close()


async def call(server, name, arguments=None):
    result = await server.call_tool(name, arguments or {})
    return json.loads(result[0].text)


def test_metadata_default_no_credentials_no_network(directories, monkeypatch):
    state, credentials = directories
    store = salad.ConfigurationStore(state)
    server = salad.create_server(store, credentials)

    def forbidden(*args, **kwargs):
        raise AssertionError("credential/provider access forbidden")

    monkeypatch.setattr(salad.PrivateFileSecretResolver, "metadata", forbidden)
    monkeypatch.setattr(salad.PrivateFileSecretResolver, "resolve", forbidden)
    monkeypatch.setattr(salad, "provider_project", forbidden)

    async def run():
        assert (await call(server, "salad_inspect"))["state"] == "unconfigured"
        result = await call(
            server,
            "salad_configure",
            configuration().model_dump(exclude={"schema_version", "integration"}),
        )
        assert result["state"] == "configured"
        assert (await call(server, "salad_readiness"))["provider"] == "unrun"

    asyncio.run(run())
    store.close()


def test_explicit_readiness_version_expiry_and_redaction(directories, monkeypatch):
    state, credentials = directories
    store = salad.ConfigurationStore(state)
    store.configure(configuration(), None)
    resolver = PrivateFileSecretResolver(credentials)
    resolver.enroll(
        "credential-ref:salad-test",
        1,
        SecretBuffer(bytearray(b"synthetic-only-marker")),
        expires_at=time.time() + 60,
    )
    resolver.close()
    buffers = []

    def provider(config, secret):
        buffers.append(secret)
        return "project_readable"

    monkeypatch.setattr(salad, "provider_project", provider)
    server = salad.create_server(store, credentials)
    result = asyncio.run(call(server, "salad_readiness", {"validate_provider": True}))
    assert result["provider"] == "project_readable"
    assert result["execution_enabled"] is False
    assert not any(buffers[0]._buffer)
    assert "synthetic-only-marker" not in json.dumps(result)
    now = time.time()
    with monkeypatch.context() as patch:
        patch.setattr(salad.time, "time", lambda: now + 120)
        expired = asyncio.run(call(server, "salad_readiness", {"validate_provider": True}))
        assert expired["provider"] == "credential_unavailable"
        assert len(buffers) == 1
    resolver = PrivateFileSecretResolver(credentials)
    resolver.revoke("credential-ref:salad-test")
    resolver.close()
    assert (
        asyncio.run(call(server, "salad_readiness", {"validate_provider": True}))["provider"]
        == "credential_unavailable"
    )
    revision = salad._digest(store.read())
    store.configure(configuration(credential_version=2), revision)
    assert (
        asyncio.run(call(server, "salad_readiness", {"validate_provider": True}))["provider"]
        == "credential_unavailable"
    )
    store.close()


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"name":"existing-project"}', "project_readable"),
        (302, b"", "redirect_denied"),
        (403, b"private error", "provider_read_failed"),
        (200, b'{"name":"other"}', "project_identity_mismatch"),
        (200, b"synthetic-only-marker", "malformed_response"),
        (200, b"x" * 65537, "malformed_response"),
    ],
)
def test_fixed_https_get_bounded_response(monkeypatch, status, body, expected):
    calls = []

    class Connection:
        sock = None

        def __init__(self, host, **kwargs):
            assert host == "api.salad.com" and kwargs == {"port": 443, "timeout": 3}

        def request(self, method, path, *, headers):
            calls.append((method, path))
            assert headers["Salad-Api-Key"] == "synthetic-only-marker"

        def getresponse(self):
            self.status = status
            self.remaining = body
            return self

        def read1(self, limit):
            chunk, self.remaining = self.remaining[:limit], self.remaining[limit:]
            return chunk

        def close(self):
            pass

    monkeypatch.setattr(salad.http.client, "HTTPSConnection", Connection)
    with SecretBuffer(bytearray(b"synthetic-only-marker")) as secret:
        assert salad.provider_project(configuration(), secret) == expected
    assert calls == [("GET", "/api/public/organizations/existing-org/projects/existing-project")]


def test_real_stdio_handshake_configure_reconnect(directories):
    state, credentials = directories

    async def session_round(configure):
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                "-m",
                "tessera_operator.salad",
                "--state-directory",
                str(state),
                "--credential-directory",
                str(credentials),
            ],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "salad_inspect",
                    "salad_configure",
                    "salad_readiness",
                }
                if configure:
                    result = await session.call_tool(
                        "salad_configure",
                        configuration().model_dump(exclude={"schema_version", "integration"}),
                    )
                    assert not result.isError
                result = await session.call_tool("salad_inspect", {})
                assert json.loads(result.content[0].text)["state"] == "configured"
                ready = await session.call_tool("salad_readiness", {})
                assert json.loads(ready.content[0].text)["provider"] == "unrun"

    asyncio.run(session_round(True))
    asyncio.run(session_round(False))
