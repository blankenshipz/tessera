"""Isolated workload contract tests; never import real torch or call providers."""

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).parents[1] / "workloads/salad_gpu_smoke/smoke.py"
SPEC = importlib.util.spec_from_file_location("tessera_gpu_smoke_test", PATH)
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def request():
    return smoke.Request.parse(
        [
            "v1",
            "public-integers-v1",
            "operation",
            "1",
            "custody.example",
            "a" * 64,
            "3",
            "4096",
            "1",
            "4",
        ]
    )


class Custody:
    def __init__(self):
        self.consumed = False
        self.outputs = []

    def begin_once(self, req):
        if self.consumed:
            return False
        self.consumed = True
        return True

    def retain(self, req, output, digest):
        self.outputs.append((output, digest))
        return True


def test_claim_precedes_effect_and_reallocation_never_repeats():
    custody = Custody()
    effects = []

    def runner(req, deadline):
        assert custody.consumed
        effects.append(1)
        return b'{"result":1}'

    digest = smoke.execute(request(), custody, time.monotonic() + 3, runner)
    assert len(digest) == 64 and len(custody.outputs) == 1
    with pytest.raises(smoke.Failure):
        smoke.execute(request(), custody, time.monotonic() + 3, runner)
    assert effects == [1]


def test_crash_after_claim_does_not_allow_second_effect():
    custody = Custody()
    calls = []

    def crash(req, deadline):
        calls.append(1)
        raise smoke.Failure()

    for _ in range(2):
        with pytest.raises(smoke.Failure):
            smoke.execute(request(), custody, time.monotonic() + 3, crash)
    assert calls == [1] and custody.outputs == []


@pytest.mark.parametrize("output", [b"", b"x" * 4097, "not-bytes"])
def test_output_caps(output):
    custody = Custody()
    with pytest.raises(smoke.Failure):
        smoke.execute(request(), custody, time.monotonic() + 3, lambda *_: output)
    assert custody.outputs == []


def test_expired_deadline_has_no_claim_or_effect():
    custody = Custody()
    with pytest.raises(smoke.Failure):
        smoke.execute(request(), custody, time.monotonic() - 1)
    assert not custody.consumed


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "v2"),
        (1, "private-input"),
        (2, "../x"),
        (3, "0"),
        (4, "https://evil"),
        (4, "evil..host"),
        (5, "not-a-digest"),
        (6, "301"),
        (7, "65537"),
        (8, "2"),
        (9, "33"),
    ],
)
def test_closed_argv(index, value):
    args = [str(v) for v in vars(request()).values()]
    args[index] = value
    with pytest.raises(smoke.Failure):
        smoke.Request.parse(args)


def test_cuda_absent_fails_before_tensor_creation():
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    with pytest.raises(smoke.Failure):
        smoke.compute(request(), torch)


def test_rejected_and_uncertain_claim_never_compute():
    for result in [False, None, 1]:
        custody = SimpleNamespace(begin_once=lambda req, result=result: result)
        with pytest.raises(smoke.Failure):
            smoke.execute(request(), custody, time.monotonic() + 3)


def test_upload_failure_never_repeats_effect():
    custody = Custody()
    custody.retain = lambda *_: False
    calls = []

    def runner(*_):
        calls.append(1)
        return b"{}"

    for _ in range(2):
        with pytest.raises(smoke.Failure):
            smoke.execute(request(), custody, time.monotonic() + 3, runner)
    assert calls == [1]


class Tensor:
    def __init__(self, values):
        self.values = values
        self.device = SimpleNamespace(type="cuda")

    def reshape(self, n, m):
        return Tensor([self.values[i * m : (i + 1) * m] for i in range(n)])

    @property
    def T(self):
        return Tensor(list(zip(*self.values, strict=True)))

    def __matmul__(self, other):
        columns = list(zip(*other.values, strict=True))
        return Tensor(
            [
                [sum(a * b for a, b in zip(row, col, strict=True)) for col in columns]
                for row in self.values
            ]
        )

    def sum(self):
        return SimpleNamespace(item=lambda: sum(map(sum, self.values)))


def test_synthetic_cuda_computation_and_typed_proof():
    sync = []
    torch = SimpleNamespace(
        float64="float64",
        cuda=SimpleNamespace(is_available=lambda: True, synchronize=lambda: sync.append(1)),
        arange=lambda n, **kw: Tensor(list(range(n))),
    )
    output = smoke.compute(request(), torch)
    obj = json.loads(output)
    assert obj["sum"] == 3680 and obj["cuda"]["result_device"] == "cuda"
    assert sync == [1] and len(output) < 4096


def test_cpu_tensor_is_not_cuda_proof():
    tensor = Tensor(list(range(16)))
    tensor.reshape = lambda *_: tensor
    tensor.device.type = "cpu"
    torch = SimpleNamespace(
        float64="float64",
        cuda=SimpleNamespace(is_available=lambda: True),
        arange=lambda *args, **kw: tensor,
    )
    # A CPU input must be rejected before attempting the CUDA calculation.
    with pytest.raises(smoke.Failure):
        smoke.compute(request(), torch)


def test_bootstrap_failure_is_sanitized_and_no_compute(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET must not escape")

    monkeypatch.setattr(smoke, "HTTPSCustody", fail)
    args = [str(v) for v in vars(request()).values()]
    assert smoke.main(args) == 1
    captured = capsys.readouterr()
    assert captured.err == "" and "SECRET" not in captured.out
    assert json.loads(captured.out)["status"] == "failed"


@pytest.mark.parametrize("ready,payload", [(False, b""), (True, b""), (True, b"x" * 4097)])
def test_supervisor_terminates_child_and_bounds_pipe(monkeypatch, ready, payload):
    events = []

    class Pipe:
        def close(self):
            events.append("close")

        def poll(self, timeout):
            assert 0 <= timeout <= 3
            return ready

        def recv_bytes(self, cap):
            assert cap == 4096
            if len(payload) > cap:
                raise OSError()
            return payload

    class Process:
        def start(self):
            events.append("start")

        def is_alive(self):
            return True

        def kill(self):
            events.append("kill")

        def join(self):
            events.append("join")

        def close(self):
            events.append("process-close")

    context = SimpleNamespace(Pipe=lambda **kw: (Pipe(), Pipe()), Process=lambda **kw: Process())
    monkeypatch.setattr(smoke.multiprocessing, "get_context", lambda _: context)
    with pytest.raises(smoke.Failure):
        smoke.bounded_compute(request(), time.monotonic() + 3)
    assert events[-3:] == ["kill", "join", "process-close"]


@pytest.mark.parametrize("status,body", [(302, b"{}"), (200, b"x" * 4097), (200, b"[]")])
def test_https_response_caps_and_no_redirect(monkeypatch, status, body):
    events = []
    client = object.__new__(smoke.HTTPSCustody)
    client.request = request()
    client.deadline = time.monotonic() + 3
    client.context = object()
    client.token = "synthetic.header.signature"

    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host == "custody.example" and port == 443
            assert kwargs["context"] is client.context

        def request(self, method, path, **kwargs):
            events.append((method, path))

        def getresponse(self):
            def read(cap):
                assert cap == 4097
                return body

            return SimpleNamespace(status=status, read=read)

        def close(self):
            events.append("closed")

    monkeypatch.setattr(smoke.http.client, "HTTPSConnection", Connection)
    with pytest.raises(smoke.Failure):
        client.begin_once(request())
    assert events == [("POST", "/v1/workload/begin-once"), "closed"]


def synthetic_jwt(expiry=None):
    payload = (
        base64.urlsafe_b64encode(
            smoke.encode({"exp": int(time.time()) + 300 if expiry is None else expiry})
        )
        .decode()
        .rstrip("=")
    )
    return "header." + payload + ".signature"


def imds(monkeypatch, body, status=200):
    events = []
    # http.client uses direct sockets and does not inspect any proxy environment.
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://untrusted.invalid:8080")

    class Connection:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ("169.254.169.254", 80)
            assert 0 < kwargs["timeout"] <= 5

        def request(self, method, path, **kwargs):
            assert kwargs == {"headers": {"Metadata": "true"}}
            events.append((method, path))

        def getresponse(self):
            def read(cap):
                assert cap == 4097
                return body

            return SimpleNamespace(status=status, read=read)

        def close(self):
            events.append("closed")

    monkeypatch.setattr(smoke.http.client, "HTTPConnection", Connection)
    return events


def test_imds_direct_fixed_endpoint_and_default_ca(monkeypatch):
    token = synthetic_jwt()
    events = imds(monkeypatch, smoke.encode({"jwt": token}))
    context = object()
    monkeypatch.setattr(smoke.ssl, "create_default_context", lambda: context)
    client = smoke.HTTPSCustody(request(), time.monotonic() + 3)
    assert client.context is context and client.token == token
    assert events == [("GET", "/v1/token"), "closed"]


@pytest.mark.parametrize(
    "body,status",
    [
        (b"{}", 302),
        (b"x" * 4097, 200),
        (b"not-json", 200),
        (b"[]", 200),
        (b' {"jwt": 2}', 200),
        (b'{"jwt":""}', 200),
        (smoke.encode({"jwt": "a" * 1001}), 200),
        (smoke.encode({"jwt": "header.payload.signature\r\nInjected: true"}), 200),
        (smoke.encode({"jwt": "header.payload.signature"}), 200),
        (smoke.encode({"jwt": synthetic_jwt(1)}), 200),
    ],
)
def test_imds_rejects_malformed_unavailable_and_oversized_without_compute(
    monkeypatch, capsys, body, status
):
    events = imds(monkeypatch, body, status)
    effects = []
    monkeypatch.setattr(smoke, "execute", lambda *_: effects.append(1))
    args = [str(v) for v in vars(request()).values()]
    assert smoke.main(args) == 1
    assert effects == [] and events == [("GET", "/v1/token"), "closed"]
    assert capsys.readouterr().out == '{"schema":"tessera.smoke-status.v1","status":"failed"}\n'


def test_bearer_only_in_https_header_and_errors_sanitized(monkeypatch, capsys):
    token = synthetic_jwt()
    imds(monkeypatch, smoke.encode({"jwt": token}))
    calls = []

    class Connection:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ("custody.example", 443)
            assert kwargs["context"].verify_mode == smoke.ssl.CERT_REQUIRED
            assert kwargs["context"].check_hostname is True

        def request(self, method, path, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer " + token
            assert token.encode() not in kwargs["body"] and token not in path
            calls.append(path)
            raise RuntimeError(token)

        def close(self):
            pass

    monkeypatch.setattr(smoke.http.client, "HTTPSConnection", Connection)
    assert smoke.main([str(v) for v in vars(request()).values()]) == 1
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert calls == ["/v1/workload/begin-once"]


def test_upload_refresh_only_near_expiry_no_claim_retry(monkeypatch):
    client = smoke.HTTPSCustody.__new__(smoke.HTTPSCustody)
    client.token = "old"
    client.token_expires = 100
    client.deadline = time.monotonic() + 3
    calls = []
    monkeypatch.setattr(smoke.time, "time", lambda: 90)
    monkeypatch.setattr(client, "identity", lambda: calls.append("refresh") or ("new", 400))

    def post(path, body):
        calls.append(path)
        assert client.token == "new"
        return {
            "schema": "tessera.retained.v1",
            "sha256": "digest",
            "operation": "operation",
            "epoch": "1",
            "retained": True,
        }

    monkeypatch.setattr(client, "post", post)
    assert client.retain(request(), b"{}", "digest")
    assert calls == ["refresh", "/v1/workload/output"]


def test_https_expired_deadline_never_connects(monkeypatch):
    client = smoke.HTTPSCustody.__new__(smoke.HTTPSCustody)
    client.deadline = time.monotonic() - 1
    with pytest.raises(smoke.Failure):
        client.begin_once(request())


def test_https_deadline_expiring_during_response_is_terminal(monkeypatch):
    client = smoke.HTTPSCustody.__new__(smoke.HTTPSCustody)
    client.request = request()
    client.context = object()
    client.token = synthetic_jwt()
    client.deadline = 100
    clock = iter([99, 101])
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock))
    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            assert kwargs["timeout"] == 1

        def request(self, method, path, **kwargs):
            calls.append(path)

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda cap: b"{}")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(smoke.http.client, "HTTPSConnection", Connection)
    with pytest.raises(smoke.Failure):
        client.begin_once(request())
    assert calls == ["/v1/workload/begin-once", "closed"]


def test_fresh_upload_identity_is_not_refetched(monkeypatch):
    client = smoke.HTTPSCustody.__new__(smoke.HTTPSCustody)
    client.token_expires = time.time() + 300
    calls = []
    monkeypatch.setattr(client, "identity", lambda: calls.append("unexpected-refresh"))
    monkeypatch.setattr(client, "post", lambda path, body: calls.append(path) or {})
    assert not client.retain(request(), b"{}", "digest")
    assert calls == ["/v1/workload/output"]
