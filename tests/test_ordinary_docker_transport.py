"""Fixed Docker HTTP contract tests; fake socket/HTTP objects, never a daemon."""

import hashlib
import json
import ssl

import pytest

from tessera_worker import ordinary_docker_transport as module


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://host:2375",
        "http://host:2375",
        "ssh://host",
        "unix://host/socket",
        "https://user:password@host",
        "https://host/arbitrary",
        "unix:///socket?query=x",
    ],
)
def test_no_ambient_plaintext_credential_or_proxy_endpoint(endpoint):
    with pytest.raises(ValueError):
        module.FixedDockerTransport(endpoint, tls=ssl.create_default_context())


def test_https_requires_hostname_verified_tls():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified"):
        module.FixedDockerTransport("https://engine:2376", tls=context)


class FakeResponse:
    def __init__(self, status=200, content=b"{}"):
        self.status, self.content = status, content

    def read(self, count):
        return self.content[:count]


class FakeConnection:
    def __init__(self):
        self.calls, self.closed = [], False
        self.response = FakeResponse()

    def request(self, *args):
        self.calls.append(args)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    connections = []

    def connect(path, timeout):
        assert path == "/private/worker/engine.sock" and 0 < timeout <= 10
        connection = FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(module, "_UnixConnection", connect)
    endpoint = "unix:///private/worker/engine.sock"
    return module.FixedDockerTransport(endpoint), connections, endpoint


def test_explicit_endpoint_pin_finite_versioned_api_and_no_auth_environment(transport):
    client, connections, endpoint = transport
    assert client.endpoint_digest == hashlib.sha256(endpoint.encode()).hexdigest()
    client.info()
    client.image("image@sha256:" + "f" * 64)
    client.create("tessera-docker-" + "a" * 32, {"Image": "fixed"})
    client.inspect("tessera-docker-" + "a" * 32)
    client.start("c" * 64)
    client.signal("c" * 64, "SIGKILL")
    client.archive("c" * 64, "/outputs", limit=1024)
    assert all(conn.closed for conn in connections)
    assert all(conn.calls[0][1].startswith("/v1.47/") for conn in connections)
    assert connections[2].calls[0][0] == "POST"
    assert json.loads(connections[2].calls[0][2]) == {"Image": "fixed"}
    assert all(conn.calls[0][3] == {"Content-Type": "application/json"} for conn in connections)


@pytest.mark.parametrize(
    "operation,args",
    [
        ("inspect", ("all",)),
        ("start", ("short-id",)),
        ("signal", ("c" * 64, "SIGUSR1")),
        ("archive", ("c" * 64, "/etc/shadow")),
        ("create", ("unrelated-container", {})),
        ("image", ("latest",)),
    ],
)
def test_invalid_selectors_and_arbitrary_paths_never_reach_transport(transport, operation, args):
    client, connections, _ = transport
    with pytest.raises(ValueError):
        getattr(client, operation)(*args, **({"limit": 1024} if operation == "archive" else {}))
    assert connections == []


@pytest.mark.parametrize(
    "status,body", [(500, b"secret daemon exception"), (200, b"x" * 65537), (200, b"not-json")]
)
def test_error_oversize_and_malformed_json_are_closed_unknown(monkeypatch, status, body):
    connection = FakeConnection()
    connection.response = FakeResponse(status, body)
    monkeypatch.setattr(module, "_UnixConnection", lambda *_: connection)
    with pytest.raises(module.DockerTransportUnknown) as error:
        module.FixedDockerTransport("unix:///fixed/socket").info()
    assert str(error.value) == "" and connection.closed


def test_inspect_absence_is_unknown_not_provider_termination(monkeypatch):
    connection = FakeConnection()
    connection.response = FakeResponse(404, b"absent")
    monkeypatch.setattr(module, "_UnixConnection", lambda *_: connection)
    assert module.FixedDockerTransport("unix:///fixed/socket").inspect("c" * 64) is None
