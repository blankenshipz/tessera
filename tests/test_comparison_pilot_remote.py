"""Remote pilot transport and restart faults, without any network or credentials."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes
from test_local_api_workflow import complete

from tessera_worker.comparison_pilot import PilotError, PilotJournal
from tessera_worker.comparison_pilot_remote import (
    PilotRemoteExecutionAdapter,
    RemoteKubernetes,
    validate_output_storage_evidence,
)
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_execution import KubernetesOutputBinding


def remote_fixture(tmp_path):
    root = tmp_path / "pilot"
    root.mkdir(mode=0o700)
    journal = PilotJournal(root)
    remote = RemoteKubernetes.__new__(RemoteKubernetes)
    remote.cluster = _cluster()
    remote.journal = journal
    remote.jobs_path = "/apis/batch/v1/namespaces/synthetic/jobs"
    remote.pods_path = "/api/v1/namespaces/synthetic/pods"
    remote.sequence = 0
    remote.credential_file = tmp_path / "synthetic-credential"
    remote.credential_file.write_text("synthetic-secret-never-log")
    remote.credential_file.chmod(0o600)
    remote.credential_fingerprint = hashlib.sha256(b"synthetic-secret-never-log").digest()
    remote.credential_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    remote.cost_expires_at = remote.credential_expires_at
    remote.output_storage = {"expires_at": remote.credential_expires_at.isoformat()}
    return remote, journal


def test_output_storage_evidence_requires_real_byte_and_inode_enforcement() -> None:
    cluster = _cluster()
    output = KubernetesOutputBinding("outputs", "/outputs", "pvc-fixture", "d" * 64)
    evidence = {
        "version": 1,
        "target_digest": cluster_identity_digest(cluster),
        "project_id": "project-fixture",
        "pvc_name": "outputs",
        "pvc_uid": "pvc-fixture",
        "pvc_spec_sha256": "d" * 64,
        "pvc_labels": {
            "app.kubernetes.io/managed-by": "tessera",
            "tessera.io/output-role": "dedicated-pilot-output",
            "tessera.io/project-id": "project-fixture",
        },
        "state": "filesystem_quota_enforced_v1",
        "max_bytes": 4096,
        "max_inodes": 1,
        "byte_probe": {
            "classification": "enforced",
            "limit": 4096,
            "written_bytes": 4096,
            "failure_errno": 28,
        },
        "inode_probe": {
            "classification": "enforced",
            "limit": 1,
            "created_files": 1,
            "failure_errno": 122,
        },
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }

    assert (
        validate_output_storage_evidence(evidence, cluster, output, "project-fixture", 1024)
        == evidence
    )
    with pytest.raises(PilotError, match="inode quota"):
        validate_output_storage_evidence(
            {
                **evidence,
                "inode_probe": {**evidence["inode_probe"], "classification": "unsupported"},
            },
            cluster,
            output,
            "project-fixture",
            1024,
        )
    with pytest.raises(PilotError, match="live execution is blocked"):
        validate_output_storage_evidence(
            {
                key: value
                for key, value in evidence.items()
                if key not in {"max_bytes", "max_inodes", "byte_probe", "inode_probe"}
            }
            | {
                "state": "operator_attested_unknown",
                "operator_attestation": "Docker Desktop local storage has no verified quota",
                "expires_at": evidence["expires_at"],
            },
            cluster,
            output,
            "project-fixture",
            1024,
        )


@pytest.mark.parametrize("mode", ["success", "timeout", "not_found", "oversized"])
def test_bounded_one_request_transport_never_logs_credentials(tmp_path, mode):
    remote, journal = remote_fixture(tmp_path)
    calls = []

    class Response(io.BytesIO):
        headers = {"Content-Type": "application/json"}

        def getcode(self):
            return 200

    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 5
        if mode == "timeout":
            raise TimeoutError("synthetic upstream secret diagnostic")
        if mode == "not_found":
            raise HTTPError(request.full_url, 404, "not found", {}, None)
        if mode == "oversized":
            return Response(b"x" * 33)
        return Response(b'{"observed": true}')

    remote.opener = SimpleNamespace(open=open_request)
    try:
        if mode in {"timeout", "oversized"}:
            with pytest.raises(PilotError):
                remote.request(
                    "GET", remote.jobs_path, log_limit=32 if mode == "oversized" else None
                )
        else:
            result = remote.request("GET", remote.jobs_path)
            assert result == (None if mode == "not_found" else {"observed": True})
        assert len(calls) == 1
        text = " ".join(row[0] for row in journal.db.execute("SELECT payload_json FROM events"))
        assert "synthetic-secret" not in text and "upstream secret" not in text
    finally:
        journal.close()


def test_missing_job_after_uncertain_create_stays_unknown_across_restart(tmp_path):
    remote, journal = remote_fixture(tmp_path)
    journal.event("remote_submit_intent", {"name": "tessera-op-example"})
    journal.close()
    remote.journal = PilotJournal(tmp_path / "pilot")
    remote.request = lambda *_: None
    try:
        with pytest.raises(PilotError, match="manual reconciliation"):
            remote.inspect("tessera-op-example")
        assert remote.inspect("unsubmitted-name") is None
        remote.request = lambda *_: {"metadata": {"uid": "unvalidated-object"}}
        assert remote.inspect("tessera-op-example") is not None
        assert not remote.journal.contains("remote_job_seen", "tessera-op-example")
        remote.request = lambda *_: None
        with pytest.raises(PilotError, match="manual reconciliation"):
            remote.inspect("tessera-op-example")
    finally:
        remote.journal.close()


@pytest.mark.parametrize("fence", ["deadline", "cancel"])
def test_preflight_delay_or_cancel_fences_actual_post(tmp_path, fence):
    remote, journal = remote_fixture(tmp_path)
    remote.submission_deadline = datetime.now(UTC) + timedelta(seconds=30)
    remote.request = lambda *_: pytest.fail("external POST after fence")

    def preflight(**_):
        if fence == "deadline":
            remote.submission_deadline = datetime.now(UTC) - timedelta(seconds=1)
        else:
            journal.event("remote_cancel_intent", {"name": "job"})

    remote.verify_target = preflight
    try:
        with pytest.raises(PilotError, match="fenced"):
            remote.create({"metadata": {"name": "job"}})
    finally:
        journal.close()


def test_remote_callbacks_use_existing_adapter_threads_and_uid_fences(tmp_path):
    _response, _worker, package = _admitted_package(tmp_path)
    remote, journal = remote_fixture(tmp_path)
    fake = _FakeKubernetes(package, remote.cluster)
    remote.isolation = SimpleNamespace(image=package.binding.image)
    remote.scope = {field: package.package[field] for field in ("org_id", "project_id")}
    remote.verify_target = lambda **_: None
    deleted = []

    def request(method, path, body=None, **kwargs):
        if method == "POST":
            result = fake.create(body)
            complete(fake, "succeeded")
            return result
        if method == "DELETE":
            uid = body["preconditions"]["uid"]
            deleted.append(uid)
            fake.delete(package.binding.job_name, uid)
            return {}
        if path.startswith(remote.pods_path):
            return {"items": fake.pods}
        return fake.jobs.get(package.binding.job_name)

    remote.request = request
    adapter = PilotRemoteExecutionAdapter(tmp_path / "adapter.sqlite3", remote)
    try:
        status = adapter.submit(package)
        assert status.observed_state == "succeeded"
        assert adapter.reconcile(package).termination_confirmed is True
        cancelled = adapter.cancel(package)
        assert cancelled.termination_confirmed is True
        assert len(deleted) == 1 and fake.creates == 1
        assert journal.contains("remote_cancel_intent", package.binding.job_name)
        # Any JSON custody/audit record remains non-secret and serializable.
        for row in journal.db.execute("SELECT payload_json FROM events"):
            assert isinstance(json.loads(row[0]), dict)
    finally:
        journal.close()


@pytest.mark.parametrize(
    "reason", ["expired", "cost_expired", "storage_expired", "wrong_subject", "changed_credential"]
)
def test_compute_credential_scope_fences_before_job_creation(tmp_path, reason):
    remote, journal = remote_fixture(tmp_path)
    calls = []
    remote.scope = {"service_account": "selected"}
    try:
        if reason == "changed_credential":
            remote.credential_file.write_text("different-synthetic-account")
            with pytest.raises(PilotError, match="changed"):
                remote.request("POST", remote.jobs_path, {})
        else:
            if reason == "expired":
                remote.credential_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif reason == "cost_expired":
                remote.cost_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif reason == "storage_expired":
                remote.output_storage["expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()

            def request(method, path, body=None):
                calls.append((method, path))
                return {"status": {"userInfo": {"username": "system:serviceaccount:other:account"}}}

            remote.request = request
            with pytest.raises(PilotError, match="expired|namespace service account"):
                remote.verify_target(submission=True)
            assert len(calls) == (0 if reason != "wrong_subject" else 1)
    finally:
        journal.close()
