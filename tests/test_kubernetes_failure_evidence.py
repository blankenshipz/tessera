"""Isolated failure diagnostics; no real credentials or provider effects."""

from __future__ import annotations

import json
import sqlite3
import ssl
import urllib.error
from io import BytesIO

import pytest
from test_kubernetes_execution import _admitted_package, _cluster
from test_kubernetes_output_delivery import creation_fixture
from test_kubernetes_remote_service import _fixture

from tessera_worker.kubernetes_execution import KubernetesExecutionAdapter
from tessera_worker.kubernetes_failure_evidence import KubernetesFailureEvidence
from tessera_worker.kubernetes_remote_service import RemoteTargetError


@pytest.mark.parametrize("quota", [False, True])
def test_http_diagnostics_classify_bounded_status_without_retaining_body_or_headers(
    tmp_path, quota
):
    client, _, _ = _fixture(tmp_path)
    sentinel = "sensitive-synthetic-marker"
    body = {
        "kind": "Status",
        "reason": "Forbidden",
        "message": ("exceeded quota: " if quota else "RBAC denied: ") + sentinel,
    }

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://fixture.invalid",
                403,
                sentinel,
                {"Authorization": sentinel},
                BytesIO(json.dumps(body).encode()),
            )

    client.opener = Opener()
    with pytest.raises(RemoteTargetError) as raised:
        client.request("POST", client.path("pod"), {})
    failures = KubernetesFailureEvidence(tmp_path / "failures.sqlite3")
    failures.record("a" * 32, "prepare.create", raised.value)
    evidence = failures.get("a" * 32)
    assert evidence["http_status"] == 403
    assert evidence["error_code"] == (
        "kubernetes_quota_exceeded" if quota else "kubernetes_http_rejected"
    )
    assert sentinel not in json.dumps(evidence) and len(json.dumps(evidence)) < 512
    assert sentinel not in str(raised.value)


def test_wrapped_tls_error_retains_only_typed_category(tmp_path):
    client, _, _ = _fixture(tmp_path)

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError(ssl.SSLError("sensitive-synthetic-marker"))

    client.opener = Opener()
    with pytest.raises(RemoteTargetError) as raised:
        client.request("GET", client.path("pod"))
    assert raised.value.diagnostic_code == "kubernetes_tls_error"
    assert "sensitive-synthetic-marker" not in str(raised.value)


def test_first_error_survives_cancellation_restart_and_untrusted_exception_fields(tmp_path):
    path = tmp_path / "failures.sqlite3"
    error = RuntimeError("sensitive-synthetic-marker")
    error.diagnostic_code = "sensitive-synthetic-marker"
    error.http_status = "sensitive-synthetic-marker"
    KubernetesFailureEvidence(path).record("a" * 32, "preparation", error)
    first = KubernetesFailureEvidence(path).get("a" * 32)
    KubernetesFailureEvidence(path).record("a" * 32, "cancellation", TimeoutError("later"))
    assert KubernetesFailureEvidence(path).get("a" * 32) == first
    assert first["error_code"] == "exception" and first["http_status"] is None
    assert "sensitive-synthetic-marker" not in json.dumps(first)


def test_collector_records_exact_create_stage_before_exception_escapes(tmp_path):
    descriptor, collector, client, _ = creation_fixture(tmp_path)

    def rejected(*_args, **_kwargs):
        raise RemoteTargetError(
            "sensitive-synthetic-marker",
            diagnostic_code="kubernetes_quota_exceeded",
            http_status=403,
        )

    client.request = rejected
    with pytest.raises(RemoteTargetError):
        collector.execute("prepare", descriptor)
    evidence = collector.failures.get(descriptor.operation_id)
    assert evidence["stage"] == "prepare.create"
    assert evidence["error_code"] == "kubernetes_quota_exceeded"
    assert "sensitive-synthetic-marker" not in json.dumps(evidence)
    with sqlite3.connect(collector.journal) as db:
        assert db.execute("SELECT uid,result FROM collectors").fetchone() == (None, None)


def test_preparation_failure_is_durable_before_unknown_and_no_start_is_only_recovery_hint(tmp_path):
    _, _, package = _admitted_package(tmp_path)
    cluster, path = _cluster(), tmp_path / "jobs.sqlite3"

    def rejected(_):
        raise RemoteTargetError(
            "sensitive-synthetic-marker",
            diagnostic_code="kubernetes_quota_exceeded",
            http_status=403,
        )

    def adapter():
        return KubernetesExecutionAdapter(
            path,
            cluster,
            inspect_job=lambda _: None,
            create_job=lambda _: pytest.fail("preparation never authorized workload creation"),
            delete_job=lambda *_: pytest.fail("no workload to delete"),
            observe_pods=lambda _: [],
            prepare_output=rejected,
        )

    worker = adapter()
    original_unknown = worker._record_unknown

    def inspect_evidence_before_unknown(descriptor):
        assert worker.failure_evidence(descriptor)["stage"] == "preparation"
        return original_unknown(descriptor)

    worker._record_unknown = inspect_evidence_before_unknown
    with pytest.raises(RemoteTargetError):
        worker.submit(package)
    before = worker.failure_evidence(package)
    assert worker.no_start_recovery_candidate(package) is False
    assert worker.cancel(package).termination_confirmed is False
    restarted = adapter()
    assert restarted.failure_evidence(package) == before
    assert restarted.no_start_recovery_candidate(package) is True
    with sqlite3.connect(path) as db:
        db.execute("UPDATE kubernetes_jobs SET create_intent=1")
    assert restarted.no_start_recovery_candidate(package) is False


def test_no_start_service_waits_for_human_recovery_without_busy_cancellation_loop(
    tmp_path, monkeypatch, capsys
):
    from tessera_worker import kubernetes_remote_service as remote

    sleeps = []
    monkeypatch.setattr(remote, "configure", lambda *_: (object(), object(), object()))
    monkeypatch.setattr(remote.ssl, "create_default_context", lambda **_: object())
    monkeypatch.setattr(remote, "read_private_credential", lambda _: "fixture-credential")
    monkeypatch.setattr(remote.time, "sleep", sleeps.append)

    class Service:
        calls = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("bounded fixture stop")
            return {
                "status": "cancel_requested",
                "reconciliation_required": True,
                "no_start_attestation_required": True,
                "dispatch_id": "fixture",
            }

    monkeypatch.setattr(remote, "KubernetesDispatchWorkerService", Service)
    flags = [
        "--target-file",
        "--ca-file",
        "--kubernetes-token-file",
        "--credential-file",
        "--control-ca-file",
    ]
    argv = [arg for flag in flags for arg in (flag, str(tmp_path / flag[2:]))]
    argv += [
        "--control-url",
        "https://control.example.test",
        "--workspace",
        str(tmp_path / "worker"),
    ]
    assert remote.main(argv) == 1
    assert sleeps == [5]
    assert json.loads(capsys.readouterr().out)["no_start_attestation_required"] is True
