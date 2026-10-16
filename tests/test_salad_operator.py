import asyncio
import json
import os
import stat
import subprocess
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer
from tessera_operator import salad, salad_enroll


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


def test_account_only_setup_real_stdio_reconnect(directories):
    state, credentials = directories

    async def roundtrip(create):
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
                if create:
                    result = await session.call_tool(
                        "salad_configure",
                        {
                            "organization": "luxai",
                            "credential_reference": "credential-ref:salad-test",
                            "credential_version": 1,
                        },
                    )
                    assert (
                        json.loads(result.content[0].text)["state"]
                        == "project_creation_requires_portal"
                    )
                result = await session.call_tool("salad_inspect", {})
                data = json.loads(result.content[0].text)
                assert data["state"] == "project_creation_requires_portal"
                assert data["configuration"]["project"] is None
                assert data["configuration"]["management_host"] is None
                assert data["next_action"]["project_created"] is False
                assert data["next_action"]["proposed_project"] == "tessera-research"
                result = await session.call_tool("salad_readiness", {})
                ready = json.loads(result.content[0].text)
                assert ready["provider"] == "unrun"
                assert ready["project"] == "requires_portal"
                assert ready["billing"] == "no_cash_hard_cap_unverified"
                assert ready["execution_enabled"] is False
                return data["revision"]

    assert asyncio.run(roundtrip(True)) == asyncio.run(roundtrip(False))


def test_account_setup_survives_credential_expiry(directories, monkeypatch):
    state, credentials = directories
    store = salad.ConfigurationStore(state)
    config = salad.SaladConfiguration(
        organization="luxai", credential_reference="credential-ref:salad-test", credential_version=1
    )
    result = store.configure(config, None)
    resolver = PrivateFileSecretResolver(credentials)
    resolver.enroll(
        "credential-ref:salad-test",
        1,
        SecretBuffer(bytearray(b"synthetic-only-marker")),
        expires_at=time.time() + 60,
    )
    resolver.close()
    server = salad.create_server(store, credentials)
    now = time.time()
    monkeypatch.setattr(salad.time, "time", lambda: now + 120)
    ready = asyncio.run(call(server, "salad_readiness", {"validate_provider": True}))
    assert ready["provider"] == "credential_unavailable"
    inspected = asyncio.run(call(server, "salad_inspect"))
    assert inspected["revision"] == result["revision"]
    assert inspected["state"] == "project_creation_requires_portal"
    assert store.configure(config, None)["reason"] == "stale_revision"
    store.close()


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
        (200, b'{"items":[]}', "project_readable"),
        (302, b"", "redirect_denied"),
        (401, b"private error", "credential_rejected"),
        (403, b"private error", "provider_access_denied"),
        (404, b"private error", "scope_not_found"),
        (429, b"private error", "provider_rate_limited"),
        (200, b'{"items":[{"project_name":"other"}]}', "project_identity_mismatch"),
        (200, b"synthetic-only-marker", "malformed_response"),
        (200, b"x" * 65537, "malformed_response"),
    ],
)
def test_fixed_https_get_bounded_response(monkeypatch, status, body, expected):
    calls = []

    class Connection:
        sock = None

        def __init__(self, host, **kwargs):
            context = kwargs.pop("context")
            assert context.check_hostname and context.verify_mode == salad.ssl.CERT_REQUIRED
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
    assert calls == [
        ("GET", "/api/public/organizations/existing-org/projects/existing-project/containers")
    ]


@pytest.mark.parametrize(
    "error,reason",
    [
        (salad.ssl.SSLError("sensitive exception details"), "provider_tls_failed"),
        (salad.socket.gaierror("sensitive exception details"), "provider_dns_failed"),
        (TimeoutError("sensitive exception details"), "provider_timeout"),
    ],
)
def test_transport_diagnostics_redacted(monkeypatch, error, reason):
    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(salad.http.client, "HTTPSConnection", unavailable)
    with SecretBuffer(bytearray(b"synthetic-only-marker")) as secret:
        result = salad._provider_get("/api/public/organizations/luxai/quotas", secret)
    assert result == (reason, None)
    assert "sensitive" not in json.dumps(result)


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
                    "salad_discover_projects",
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


def test_discovery_fixed_luxai_no_invented_project_listing(directories, monkeypatch):
    state, credentials = directories
    resolver = PrivateFileSecretResolver(credentials)
    resolver.enroll(
        "credential-ref:salad-test",
        1,
        SecretBuffer(bytearray(b"synthetic-only-marker")),
        expires_at=time.time() + 60,
    )
    resolver.close()
    requests = []

    def get(path, secret):
        requests.append(path)
        return "readable", {"items": []}

    monkeypatch.setattr(salad, "_provider_get", get)
    store = salad.ConfigurationStore(state)
    server = salad.create_server(store, credentials)
    args = {"credential_reference": "credential-ref:salad-test", "credential_version": 1}
    result = asyncio.run(call(server, "salad_discover_projects", args))
    assert result["project_discovery"] == "unsupported_by_public_api"
    assert result["provider"] == "organization_readable"
    result = asyncio.run(
        call(server, "salad_discover_projects", args | {"candidate_project": "existing-project"})
    )
    assert result["provider"] == "project_readable"
    assert requests == [
        "/api/public/organizations/luxai/quotas",
        "/api/public/organizations/luxai/projects/existing-project/containers",
    ]
    assert store.read() is None
    assert "synthetic-only-marker" not in json.dumps(result)
    requests.clear()
    invalid = asyncio.run(
        call(server, "salad_discover_projects", args | {"candidate_project": "../escape"})
    )
    assert invalid["state"] == "blocked"
    assert requests == []
    store.close()


def test_enrollment_refuses_non_tty_and_secret_arguments(directories):
    _, credentials = directories
    cmd = [
        sys.executable,
        "-m",
        "tessera_operator.salad_enroll",
        "--credential-directory",
        str(credentials),
        "--reference",
        "credential-ref:salad-test",
    ]
    result = subprocess.run(cmd, input="synthetic-only-marker\n", capture_output=True, text=True)
    assert result.returncode == 2
    assert "synthetic-only-marker" not in result.stdout + result.stderr
    assert not (credentials / "salad-test.credential").exists()
    result = subprocess.run(
        cmd + ["--key", "synthetic-only-marker"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "synthetic-only-marker" not in result.stdout + result.stderr


def test_enrollment_no_overwrite_or_echo_fallback(directories, monkeypatch, capsys):
    _, credentials = directories
    resolver = PrivateFileSecretResolver(credentials)
    resolver.enroll(
        "credential-ref:salad-test",
        1,
        SecretBuffer(bytearray(b"synthetic-only-marker")),
        expires_at=time.time() + 60,
    )
    resolver.close()
    before = (credentials / "salad-test.credential").read_bytes()
    monkeypatch.setattr(salad_enroll, "hidden_secret", lambda: pytest.fail("must not prompt"))
    assert (
        salad_enroll.main(
            ["--credential-directory", str(credentials), "--reference", "credential-ref:salad-test"]
        )
        == 2
    )
    assert (credentials / "salad-test.credential").read_bytes() == before
    assert "synthetic-only-marker" not in capsys.readouterr().err


def test_hidden_input_rejects_echo_warning(monkeypatch):
    import warnings

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    def unsupported(prompt):
        warnings.warn("echo unavailable", salad_enroll.getpass.GetPassWarning, stacklevel=1)
        pytest.fail("must stop before fallback")

    monkeypatch.setattr(salad_enroll.getpass, "getpass", unsupported)
    with pytest.raises(salad_enroll.getpass.GetPassWarning):
        salad_enroll.hidden_secret()


def test_real_hidden_terminal_enrollment_no_echo(directories):
    import pty
    import select

    _, credentials = directories
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tessera_operator.salad_enroll",
            "--credential-directory",
            str(credentials),
            "--reference",
            "credential-ref:salad-test",
            "--expires-in-hours",
            "24",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = bytearray()
    try:
        deadline = time.monotonic() + 10
        while b"local entry only): " not in output:
            assert time.monotonic() < deadline
            if select.select([master], [], [], 0.2)[0]:
                output.extend(os.read(master, 4096))
        # The prompt is emitted after getpass has disabled terminal echo.
        os.write(master, b"synthetic-only-marker\n")
        while process.poll() is None:
            assert time.monotonic() < deadline
            if select.select([master], [], [], 0.2)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
        assert process.wait(timeout=5) == 0
        assert b"synthetic-only-marker" not in output
        assert b'"reference": "credential-ref:salad-test"' in output
        resolver = PrivateFileSecretResolver(credentials)
        with resolver.resolve("credential-ref:salad-test", 1) as secret:
            assert secret._buffer == bytearray(b"synthetic-only-marker")
        resolver.close()
        assert stat.S_IMODE((credentials / "salad-test.credential").stat().st_mode) == 0o600
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
