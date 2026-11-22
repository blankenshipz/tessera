"""Disposable config bytes/fake TLS+HTTP only; no native credentials or network."""

import base64
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tessera_operator import maintenance_transport as transport
from tessera_operator import upgrade_support as auth
from tessera_operator.schema_supervised import SupervisedDenied, digest


def request():
    return dict(
        method="POST",
        path=f"/api/v1/namespaces/{auth.NAMESPACE}/pods",
        query={"dryRun": "All", "fieldValidation": "Strict"},
        body=dict(
            apiVersion="v1",
            kind="Pod",
            metadata=dict(
                name="tessera-quota-probe-" + digest("test-operation")[:16],
                namespace=auth.NAMESPACE,
                annotations={"tessera-maintenance-operation": "test-operation"},
            ),
            spec=dict(
                restartPolicy="Never",
                automountServiceAccountToken=False,
                schedulingGates=[{"name": "tessera.invalid/never-schedule"}],
                containers=[dict(name="probe", image="registry.k8s.io/pause:3.10")],
            ),
        ),
    )


def rejection():
    name = request()["body"]["metadata"]["name"]
    return dict(
        apiVersion="v1",
        kind="Status",
        code=403,
        status="Failure",
        reason="Forbidden",
        details=dict(name=name, kind="pods"),
        message=f'pods "{name}" is forbidden: exceeded quota: tessera-maintenance-hold, '
        "requested: pods=1, used: pods=1, limited: pods=0",
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    # All auth material below is deliberately non-credential test text. Fake TLS
    # consumes handles; a real TLS/client constructor must never be reached.
    def encode(s):
        return base64.b64encode(s.encode()).decode()

    config = dict(
        contexts=[dict(name=auth.CONTEXT, context=dict(cluster="local", user="operator"))],
        clusters=[
            dict(
                name="local",
                cluster={
                    "server": "https://127.0.0.1:6443",
                    "certificate-authority-data": encode("fake-ca"),
                },
            )
        ],
        users=[
            dict(
                name="operator",
                user={
                    "client-certificate-data": encode("fake-cert"),
                    "client-key-data": encode("fake-key"),
                },
            )
        ],
    )
    config_path = tmp_path / "disposable-config"
    config_path.write_text(yaml.safe_dump(config))
    config_path.chmod(0o600)
    monkeypatch.setattr(auth, "KUBECONFIG", config_path)
    monkeypatch.setattr(auth, "SOURCE", tmp_path)
    (tmp_path / "private").mkdir(mode=0o700)
    state = SimpleNamespace(
        config=config,
        path=config_path,
        client_entries=0,
        client_exits=0,
        streams=0,
        stream_exits=0,
        status=403,
        headers={},
        chunks=None,
        tls_files=[],
        tls_error=False,
        stream_error=False,
        legacy_calls=[],
        client_kwargs=None,
    )

    class TLS:
        def load_cert_chain(self, cert, key):
            for path, expected in ((Path(cert), b"fake-cert"), (Path(key), b"fake-key")):
                assert path.read_bytes() == expected
                assert path.stat().st_mode & 0o777 == 0o600
                state.tls_files.append(path)
            if state.tls_error:
                raise ValueError("sensitive-test-error")

    tls = TLS()

    def create_tls(*, cadata):
        assert cadata == "fake-ca"
        return tls

    monkeypatch.setattr(auth.ssl, "create_default_context", create_tls)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == dict(verify=tls, trust_env=False, follow_redirects=False, timeout=10)
            assert all(not path.exists() for path in state.tls_files)
            state.client_kwargs = kwargs

        def __enter__(self):
            state.client_entries += 1
            return self

        def __exit__(self, *args):
            state.client_exits += 1

        def request(self, *args, **kwargs):
            state.legacy_calls.append((args, kwargs))
            return SimpleNamespace(status_code=202)

        @contextmanager
        def stream(self, method, url, *, params, json):
            state.streams += 1
            assert method == "POST"
            assert url == "https://127.0.0.1:6443" + request()["path"]
            assert params == request()["query"] and json == request()["body"]
            if state.stream_error:
                raise OSError("sensitive-test-error")

            def chunks():
                yield from (
                    state.chunks
                    if state.chunks is not None
                    else [__import__("json").dumps(rejection()).encode()]
                )

            try:
                yield SimpleNamespace(
                    status_code=state.status, headers=state.headers, iter_raw=chunks
                )
            finally:
                state.stream_exits += 1

    monkeypatch.setattr(auth.httpx, "Client", Client)
    yield state
    assert all(not p.exists() for p in state.tls_files)
    assert not list((tmp_path / "private").iterdir())


def test_exact_status_and_legacy_call_share_existing_auth(isolated):
    result = transport.FixedQuotaDryRunTransport()(request())
    assert result == dict(code=403, body=rejection(), classification="quota_admission_rejection")
    assert isolated.streams == isolated.stream_exits == 1
    assert isolated.client_entries == isolated.client_exits == 1
    assert auth._api_request("DELETE", "/legacy", {"uid": "pin"}) == 202
    assert isolated.legacy_calls[-1] == (
        ("DELETE", "https://127.0.0.1:6443/legacy"),
        dict(json={"uid": "pin"}, content=None, headers=None),
    )
    assert (
        auth._api_request(
            "PATCH", "/legacy", {"uid": "pin"}, content_type="application/json-patch+json"
        )
        == 202
    )
    assert isolated.legacy_calls[-1][1] == dict(
        json=None,
        content=b'{"uid": "pin"}',
        headers={"Content-Type": "application/json-patch+json"},
    )


@pytest.mark.parametrize("fault", ["method", "path", "query", "body", "extra", "name", "number"])
def test_request_allowlist_before_any_auth_or_io(monkeypatch, fault):
    def forbidden():
        pytest.fail("auth must not be reached")

    monkeypatch.setattr(auth, "_api_client", forbidden)
    value = request()
    if fault in {"method", "path"}:
        value[fault] = "arbitrary"
    elif fault == "query":
        value["query"].pop("dryRun")
    elif fault == "body":
        value["body"]["spec"]["containers"][0]["image"] = "other"
    elif fault == "extra":
        value["extra"] = "arbitrary"
    elif fault == "name":
        value["body"]["metadata"]["name"] = "other"
    else:
        value["body"]["spec"]["automountServiceAccountToken"] = 0
    with pytest.raises(SupervisedDenied, match="only exact"):
        transport.FixedQuotaDryRunTransport()(value)


@pytest.mark.parametrize("fault", ["remote", "insecure", "ca", "user", "server", "public"])
def test_auth_and_target_invariants_remain_failclosed(isolated, monkeypatch, fault):
    cluster = isolated.config["clusters"][0]["cluster"]
    if fault == "remote":
        cluster["server"] = "https://example.invalid:6443"
    elif fault == "insecure":
        cluster["insecure-skip-tls-verify"] = True
    elif fault == "ca":
        cluster.pop("certificate-authority-data")
    elif fault == "user":
        isolated.config["users"][0]["user"].pop("client-key-data")
    elif fault == "server":
        original = auth._kubeconfig_identity
        monkeypatch.setattr(
            auth, "_kubeconfig_identity", lambda: original() | {"server": "changed"}
        )
    isolated.path.write_text(yaml.safe_dump(isolated.config))
    if fault == "public":
        isolated.path.chmod(0o644)
    with pytest.raises(SupervisedDenied) as exc:
        transport.FixedQuotaDryRunTransport()(request())
    assert "fake-key" not in str(exc.value)
    assert isolated.client_entries == isolated.streams == 0


@pytest.mark.parametrize("fault", ["tls", "network", "oversize", "encoding", "deadline"])
def test_cleanup_on_exception_and_no_retry(isolated, monkeypatch, fault):
    if fault == "tls":
        isolated.tls_error = True
    elif fault == "network":
        isolated.stream_error = True
    elif fault == "oversize":
        isolated.chunks = [b"x" * 32768, b"x" * 32768, b"x"]
    elif fault == "encoding":
        isolated.headers = {"content-encoding": "gzip"}
    else:
        times = iter([0, 11])
        monkeypatch.setattr(transport.time, "monotonic", lambda: next(times))
    with pytest.raises(SupervisedDenied) as exc:
        transport.FixedQuotaDryRunTransport()(request())
    assert str(exc.value) == "quota transport unavailable or response bound exceeded"
    assert isolated.streams <= 1
    assert isolated.client_entries == isolated.client_exits


@pytest.mark.parametrize(
    "fault,category",
    [
        ("json", "malformed_status"),
        ("list", "malformed_status"),
        ("code", "malformed_status"),
        ("auth", "authentication_denied"),
        ("rbac", "authorization_denied"),
        ("message", "authorization_denied"),
        ("details", "authorization_denied"),
        ("redirect", "redirect_denied"),
        ("success", "malformed_status"),
    ],
)
def test_nonquota_responses_are_sanitized_not_proof(isolated, fault, category):
    value = copy.deepcopy(rejection())
    if fault == "json":
        isolated.chunks = [b"not-json-sensitive-test-error"]
    elif fault == "list":
        value = [value]
    elif fault == "code":
        value["code"] = 401
    elif fault == "auth":
        isolated.status = value["code"] = 401
        value["reason"] = "Unauthorized"
    elif fault in {"rbac", "message"}:
        value["message"] = "sensitive-test-error"
    elif fault == "details":
        value["details"]["name"] = "different"
    elif fault == "redirect":
        isolated.status = 302
        isolated.headers = {"location": "https://secret.invalid/"}
    else:
        isolated.status = 201
        value = dict(kind="Pod", apiVersion="v1")
    if isolated.chunks is None:
        isolated.chunks = [json.dumps(value).encode()]
    result = transport.FixedQuotaDryRunTransport()(request())
    assert result == dict(code=isolated.status, body={}, classification=category)
    assert isolated.streams == 1
