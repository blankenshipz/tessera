"""Fake socket conformance; no DNS, socket or external TLS handshake is performed."""

import hashlib
import ssl
from datetime import timedelta

import pytest
from test_capability_activation import NOW, candidate  # noqa: F401

from tessera_worker.capability_tls import DownloadDenied, PinnedHTTPSDownload


class Socket:
    def __init__(self):
        self.content = b"fixture"
        self.parts = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n",
            self.content,
            b"",
        ]
        self.peer = "8.8.8.8"
        self.der = b"fixture-der"
        self.certificate = {
            "subjectAltName": (("DNS", "artifacts.example.org"),),
            "issuer": (("organizationName", "Fixture CA"),),
        }
        self.requests, self.timeouts, self.connects = [], [], []
        self.closed = False
        self.on_recv = lambda: None

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, endpoint):
        self.connects.append(endpoint)

    def getpeername(self):
        return self.peer, 443

    def getpeercert(self, binary_form=False):
        return self.der if binary_form else self.certificate

    def sendall(self, value):
        self.requests.append(value)

    def recv(self, count):
        self.on_recv()
        part = self.parts.pop(0)
        if len(part) > count:
            self.parts.insert(0, part[count:])
        return part[:count]

    def close(self):
        self.closed = True


@pytest.fixture
def download(candidate, monkeypatch):  # noqa: F811
    sock = Socket()
    clock = [0.0]
    contexts = []

    def no_dns(*args, **kwargs):
        pytest.fail("DNS must never be resolved by the transport")

    monkeypatch.setattr("socket.getaddrinfo", no_dns)
    monkeypatch.setattr("socket.socket", lambda *args: sock)

    def load(context, **kwargs):
        assert kwargs == {"cadata": "explicit-fixture-CA"}
        contexts.append(context)

    def wrap(context, raw, *, server_hostname):
        assert raw is sock and server_hostname == "artifacts.example.org"
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
        return sock

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", load)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)
    client = PinnedHTTPSDownload(
        candidate.manifest.destination,
        ca_pem="explicit-fixture-CA",
        expected_size=7,
        expected_sha256=hashlib.sha256(b"fixture").hexdigest(),
        content_type="application/octet-stream",
        max_seconds=10,
        now=lambda: NOW,
        monotonic=lambda: clock[0],
    )
    return client, sock, clock, contexts


def drain(client):
    data = b""
    while True:
        chunk = client.read(len(data), limit=3, timeout=1.0)
        if not chunk:
            return data
        data += chunk


def test_pinned_get_hash_before_verified_no_ambient_bypass(download, monkeypatch):
    client, sock, _, contexts = download
    monkeypatch.setenv("HTTPS_PROXY", "http://secret-canary.example:8888")
    monkeypatch.setenv("SSL_CERT_FILE", "/no/ambient/CA")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret-canary")
    response = client.open(timeout=1.0)
    assert not client.verified and response.size == 7
    assert drain(client) == b"fixture"
    assert client.verified and sock.closed
    assert sock.connects == [("8.8.8.8", 443)]
    assert len(contexts) == 1
    assert sock.requests == [
        b"GET /approved/fixture HTTP/1.1\r\nHost: artifacts.example.org\r\n"
        b"Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
    ]
    assert all(0 < t <= 1 for t in sock.timeouts)
    with pytest.raises(DownloadDenied):
        client.open(timeout=1.0)


@pytest.mark.parametrize("failure", ["ip", "certificate", "san", "issuer", "chain"])
def test_tls_and_ip_mismatch_no_get(download, monkeypatch, failure):
    client, sock, _, _ = download
    if failure == "ip":
        sock.peer = "8.8.4.4"
    elif failure == "certificate":
        sock.der = b"other certificate"
    elif failure == "san":
        sock.certificate["subjectAltName"] = (("DNS", "*.example.org"),)
    elif failure == "issuer":
        sock.certificate["issuer"] = (("organizationName", "Other CA"),)
    else:

        def bad_chain(*args, **kwargs):
            raise ssl.SSLCertVerificationError("secret-canary upstream error")

        monkeypatch.setattr(ssl.SSLContext, "wrap_socket", bad_chain)
    with pytest.raises(DownloadDenied) as error:
        client.open(timeout=1.0)
    assert not sock.requests and sock.closed
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 302 Found\r\nLocation: https://secret-canary.example/\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nContent-Length: 7\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nContent-Type: text/html\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n X-Folded: value\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 16384 + b"\r\n\r\n",
    ],
)
def test_redirect_headers_and_wire_bounds(download, response):
    client, sock, _, _ = download
    sock.parts = [response, b""]
    with pytest.raises(DownloadDenied) as error:
        client.open(timeout=1)
    assert sock.closed and not client.verified and len(sock.connects) == 1
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize("body", [b"fixturX", b"short", b"fixture-plus-one"])
def test_hash_size_and_quota_breach(download, body):
    client, sock, _, _ = download
    sock.parts[1] = body
    client.open(timeout=1)
    with pytest.raises(DownloadDenied):
        drain(client)
    assert not client.verified and sock.closed


def test_global_deadline_not_extended_by_each_read(download):
    client, sock, clock, _ = download
    client.open(timeout=1)
    sock.on_recv = lambda: clock.__setitem__(0, clock[0] + 4)
    with pytest.raises(DownloadDenied):
        drain(client)
    assert not client.verified and sock.closed


@pytest.mark.parametrize("age", [-1, 6])
def test_dns_snapshot_must_be_fresh_before_any_connection(download, age):
    client, sock, _, _ = download
    client.now = lambda: NOW + timedelta(seconds=age)
    with pytest.raises(DownloadDenied):
        client.open(timeout=1)
    assert not sock.connects


def test_failed_open_cannot_retry(download):
    client, sock, _, _ = download
    sock.peer = "127.0.0.1"
    with pytest.raises(DownloadDenied):
        client.open(timeout=1)
    sock.peer = "8.8.8.8"
    with pytest.raises(DownloadDenied):
        client.open(timeout=1)
    assert len(sock.connects) == 1


@pytest.mark.parametrize("offset,limit", [(1, 2), (0, 65537), (0, 0)])
def test_stream_sequence_and_chunk_bound(download, offset, limit):
    client, sock, _, _ = download
    client.open(timeout=1)
    with pytest.raises(DownloadDenied):
        client.read(offset, limit=limit, timeout=1)
    assert sock.closed
