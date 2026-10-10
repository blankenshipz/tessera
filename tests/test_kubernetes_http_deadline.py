"""Real loopback HTTP error streams; synthetic credentials/Kubernetes authority.

This exercises urllib's actual HTTPError/HTTPResponse/socket read path, not TLS,
the cluster, or provider isolation. The production HTTPS composition is unchanged.
"""

import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_agent_sandbox import create, setup
from test_kubernetes_remote_service import _fixture

from tessera_worker.kubernetes_remote_service import RemoteTargetError


@pytest.fixture
def slow_error_server():
    entered, stop = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            content = (
                b'{"kind":"Status","reason":"Forbidden","message":"exceeded quota: synthetic"}'
            )
            self.send_response(403)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            entered.set()
            try:
                for byte in content:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if stop.wait(0.02):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", entered
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(1)


def loopback_client(tmp_path, origin):
    client, _, _ = _fixture(tmp_path)
    # Test-only origin replacement, retaining the actual request/urllib opener.
    client.cluster = SimpleNamespace(**(client.cluster.__dict__ | {"server": origin}))
    client.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return client


def test_real_slow_drip_http_error_obeys_original_total_deadline(tmp_path, slow_error_server):
    origin, entered = slow_error_server
    client = loopback_client(tmp_path, origin)
    begin = time.monotonic()
    with pytest.raises(RemoteTargetError) as raised:
        client.request("POST", client.path("pod"), {}, timeout_seconds=0.2)
    elapsed = time.monotonic() - begin
    assert entered.is_set()
    assert 0.15 <= elapsed < 0.7
    assert raised.value.http_status == 403
    assert raised.value.diagnostic_code == "kubernetes_http_rejected"
    assert "synthetic" not in str(raised.value)


def test_final_effect_drip_error_releases_writer_and_retains_uncertain_pvc(
    tmp_path, slow_error_server, monkeypatch
):
    from tessera_worker import agent_sandbox_discovery

    origin, entered = slow_error_server
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    transport_root = tmp_path / "transport"
    transport_root.mkdir()
    client = loopback_client(transport_root, origin)
    original = f.cluster.request
    monkeypatch.setattr(agent_sandbox_discovery, "WRITE_SECONDS", 0.25)

    def request(method, path, body=None, **kwargs):
        if method == "PUT" and "/pods/" in path:
            # Use the real HTTP error socket while retaining synthetic discovery.
            return client.request("POST", path, body, **kwargs)
        return original(method, path, body, **kwargs)

    monkeypatch.setattr(f.cluster, "request", request)
    results, errors = [], []

    def release():
        try:
            results.append(
                f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)[1]
            )
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=release)
    thread.start()
    try:
        assert entered.wait(2)
        begin = time.monotonic()
        f.execution.revoke_execution_grant(
            f.owner, f.grant["id"], "revoke-drip", "revoke-drip", f.grant["revision"]
        )
        assert time.monotonic() - begin < 0.7
    finally:
        thread.join(2)
    assert not thread.is_alive() and not errors
    assert results[0]["phase"] == "uncertain"
    assert not results[0]["quiescent"]
    assert results[0]["release_attempted"] is True
    pvc = f.cluster.values[f.cluster.path("pvc", "outputs-tessera-sb-" + f.operation_id)]
    assert pvc["metadata"]["uid"] == "pvc-uid"
    assert not any(method == "DELETE" for method, _ in f.cluster.calls)


@pytest.mark.parametrize("has_read1", [False, True])
def test_error_without_bounded_reader_omits_diagnostics_and_closes(tmp_path, has_read1):
    import urllib.error

    client, _, _ = _fixture(tmp_path)

    class UnboundedBody:
        closed = False

        def read(self, *_args):
            pytest.fail("must not read a body without bounded chunk capability")

        def close(self):
            self.closed = True

    body = UnboundedBody()
    if has_read1:
        body.read1 = body.read

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError("https://synthetic.invalid", 403, "Forbidden", {}, body)

    client.opener = Opener()
    with pytest.raises(RemoteTargetError) as raised:
        client.request("POST", client.path("pod"), {})
    assert raised.value.diagnostic_code == "kubernetes_http_rejected"
    assert body.closed
