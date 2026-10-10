"""Isolated transport and target-fencing checks; no Kubernetes effects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from tessera_worker import kubernetes_remote_service as remote_service
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesConnectivityEvidence,
    KubernetesNetworkPolicyEvidence,
    canonical_json,
)
from tessera_worker.kubernetes_dispatch_service import KubernetesClaimIntentJournal
from tessera_worker.kubernetes_execution import KubernetesOutputBinding
from tessera_worker.kubernetes_remote_service import (
    NamespacedKubernetesClient,
    RemoteTargetError,
    _private_json,
)


def test_cli_passes_two_cycle_bound_to_dispatch_service(tmp_path, monkeypatch, capsys):
    calls = []
    cycles = []

    class Service:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *, max_cycles):
            calls.append(max_cycles)
            for _ in range(max_cycles):
                cycles.append("observed")
            return {"status": "running", "dispatch_id": "bounded"}

    monkeypatch.setattr(remote_service, "configure", lambda *_: (object(), object(), object()))
    monkeypatch.setattr(remote_service.ssl, "create_default_context", lambda **_: object())
    monkeypatch.setattr(remote_service, "read_private_credential", lambda _: "test-credential")
    monkeypatch.setattr(remote_service, "KubernetesDispatchWorkerService", Service)
    paths = [
        "--target-file",
        "--ca-file",
        "--kubernetes-token-file",
        "--credential-file",
        "--control-ca-file",
    ]
    argv = [arg for flag in paths for arg in (flag, str(tmp_path / flag[2:]))]
    argv += [
        "--control-url",
        "https://control.example.test",
        "--workspace",
        str(tmp_path / "work"),
        "--max-cycles",
        "2",
    ]
    assert remote_service.main(argv) == 0
    assert calls == [2]
    assert len(cycles) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "running", "dispatch_id": "bounded"}


def test_idle_outage_replays_journal_key_with_bounded_backoff(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(remote_service.time, "sleep", sleeps.append)

    class Service:
        claims = KubernetesClaimIntentJournal(tmp_path, "next")

        def __init__(self):
            self.keys = []

        def run(self, *, max_cycles):
            assert max_cycles is None
            self.claims.action("heartbeat", {})
            self.keys.append(self.claims.pending_action("heartbeat")[1])
            if len(self.keys) < 3:
                raise ControlChannelError("control response deadline exceeded")
            self.claims.complete_heartbeat(self.claims.pending_action("heartbeat")[2])
            return {"status": "idle"}

    service = Service()
    assert remote_service._run_with_idle_recovery(service, 0) == {"status": "idle"}
    assert len(set(service.keys)) == 1
    assert sleeps == [1, 2]


@pytest.mark.parametrize("status", [401, 403, 409, 200, 302, 429])
def test_idle_semantic_or_protocol_failure_is_not_retried(tmp_path, monkeypatch, status):
    sleeps = []
    monkeypatch.setattr(remote_service.time, "sleep", sleeps.append)

    class Service:
        claims = KubernetesClaimIntentJournal(tmp_path, "next")
        calls = 0

        def run(self, *, max_cycles):
            self.calls += 1
            raise ControlChannelError("control rejected request", status)

    service = Service()
    with pytest.raises(ControlChannelError) as error:
        remote_service._run_with_idle_recovery(service, 0)
    assert error.value.status == status
    assert service.calls == 1
    assert sleeps == []


def test_idle_retry_exhaustion_and_active_descriptor_do_not_reenter(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(remote_service.time, "sleep", sleeps.append)

    class Service:
        claims = KubernetesClaimIntentJournal(tmp_path, "next")
        calls = 0

        def run(self, *, max_cycles):
            self.calls += 1
            raise ControlChannelError("control transport unavailable", 503)

    service = Service()
    with pytest.raises(ControlChannelError):
        remote_service._run_with_idle_recovery(service, 0)
    assert service.calls == 4
    assert sleeps == [1, 2, 4]
    # An owned provider descriptor is never retried by the idle admission loop.
    service.calls = 0
    service.claims.descriptor = lambda: {"operation_id": "existing"}
    with pytest.raises(ControlChannelError):
        remote_service._run_with_idle_recovery(service, 0)
    assert service.calls == 1
    assert sleeps == [1, 2, 4]


def _fixture(tmp_path: Path):
    now = datetime.now(UTC)
    kubeconfig = tmp_path / "explicit-kubeconfig"
    kubeconfig.write_text("test binding")
    spec = {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    pvc_spec = {"accessModes": ["ReadWriteOnce"]}
    policy_sha = hashlib.sha256(canonical_json(spec)).hexdigest()
    pvc_sha = hashlib.sha256(canonical_json(pvc_spec)).hexdigest()
    proof = KubernetesNetworkPolicyEvidence(
        "network-policy-enforced-v1",
        "default-deny",
        "policy-uid",
        policy_sha,
        "cluster-uid",
        "namespace-uid",
        now,
        KubernetesConnectivityEvidence("allowed-uid", "server-uid", "allowed", 3, now),
        KubernetesConnectivityEvidence("denied-uid", "server-uid", "denied", 3, now),
    )
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"fixture CA bytes")
    token = tmp_path / "token"
    token.write_text("fixture-token")
    cluster = KubernetesClusterConfig(
        kubeconfig=str(kubeconfig),
        kubeconfig_sha256=hashlib.sha256(kubeconfig.read_bytes()).hexdigest(),
        context="test-context",
        profile="development-minikube-opencode-v1",
        server="https://kubernetes.example.invalid",
        server_sha256=hashlib.sha256(b"https://kubernetes.example.invalid").hexdigest(),
        ca_sha256=hashlib.sha256(ca.read_bytes()).hexdigest(),
        namespace="tessera-workloads",
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
        network_none_evidence=proof,
    )
    output = KubernetesOutputBinding("outputs", "/outputs", "pvc-uid", pvc_sha)
    resources = {
        ("namespace", "kube-system"): {"metadata": {"uid": "cluster-uid"}},
        ("namespace", "tessera-workloads"): {"metadata": {"uid": "namespace-uid"}},
        ("pvc", "outputs"): {
            "metadata": {"uid": "pvc-uid"},
            "spec": pvc_spec,
            "status": {"phase": "Bound"},
        },
        ("networkpolicy", "default-deny"): {"metadata": {"uid": "policy-uid"}, "spec": spec},
    }
    client = NamespacedKubernetesClient.__new__(NamespacedKubernetesClient)
    client.cluster, client.ca, client.token = cluster, ca, token
    client.get = lambda kind, name: resources.get((kind, name))
    return client, output, resources


def test_bound_target_rechecks_namespace_pvc_policy_and_admission_freshness(tmp_path):
    client, output, resources = _fixture(tmp_path)
    client.verify(output, admission=True)
    resources[("namespace", "tessera-workloads")]["metadata"]["uid"] = "other-uid"
    with pytest.raises(RemoteTargetError, match="namespace identity"):
        client.verify(output)
    resources[("namespace", "tessera-workloads")]["metadata"]["uid"] = "namespace-uid"
    resources[("pvc", "outputs")]["spec"]["accessModes"] = ["ReadWriteMany"]
    with pytest.raises(RemoteTargetError, match="PVC binding"):
        client.verify(output)
    resources[("pvc", "outputs")]["spec"]["accessModes"] = ["ReadWriteOnce"]
    resources[("networkpolicy", "default-deny")]["metadata"]["uid"] = "other-policy"
    with pytest.raises(RemoteTargetError, match="policy identity"):
        client.verify(output)
    resources[("networkpolicy", "default-deny")]["metadata"]["uid"] = "policy-uid"
    proof = client.cluster.network_none_evidence
    assert proof is not None
    from dataclasses import replace

    client.cluster = replace(
        client.cluster,
        network_none_evidence=replace(
            proof,
            observed_at=datetime.now(UTC) - timedelta(minutes=16),
            allowed_connectivity=replace(
                proof.allowed_connectivity, observed_at=datetime.now(UTC) - timedelta(minutes=16)
            ),
            denied_connectivity=replace(
                proof.denied_connectivity, observed_at=datetime.now(UTC) - timedelta(minutes=16)
            ),
        ),
    )
    client.verify(output)  # Recovery/cancellation must still be possible.
    with pytest.raises(RemoteTargetError, match="fresh witnessed"):
        client.verify(output, admission=True)


def test_target_files_reject_links_and_oversized_json(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"cluster": "untrusted"}))
    assert _private_json(target)["cluster"] == "untrusted"
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(RemoteTargetError, match="regular target"):
        _private_json(link)
    target.write_bytes(b"x" * 16385)
    with pytest.raises(RemoteTargetError, match="16 KiB"):
        _private_json(target)


def test_bound_adapter_never_creates_after_stale_witness(tmp_path):
    client, output, _ = _fixture(tmp_path)
    recorded = []
    client.request = lambda method, path, body=None: recorded.append((method, path, body))
    adapter = client.adapter(tmp_path / "jobs.sqlite3", output)
    assert adapter._create_job({"kind": "Job"}) is None
    assert recorded == [("POST", client.path("job"), {"kind": "Job"})]
    proof = client.cluster.network_none_evidence
    assert proof is not None
    from dataclasses import replace

    client.cluster = replace(
        client.cluster,
        network_none_evidence=replace(
            proof,
            observed_at=datetime.now(UTC) - timedelta(minutes=16),
            allowed_connectivity=replace(
                proof.allowed_connectivity, observed_at=datetime.now(UTC) - timedelta(minutes=16)
            ),
            denied_connectivity=replace(
                proof.denied_connectivity, observed_at=datetime.now(UTC) - timedelta(minutes=16)
            ),
        ),
    )
    with pytest.raises(RemoteTargetError, match="fresh witnessed"):
        adapter._create_job({"kind": "Job"})
    assert len(recorded) == 1


def test_raw_typed_pod_list_supplies_omitted_item_type_but_rejects_conflicts(tmp_path):
    client, output, _ = _fixture(tmp_path)
    item = {
        "metadata": {
            "name": "owned",
            "uid": "pod-uid",
            "ownerReferences": [{"kind": "Job", "uid": "job-uid"}],
        }
    }
    client.request = lambda *_args: {
        "apiVersion": "v1",
        "kind": "PodList",
        "metadata": {},
        "items": [item],
    }
    adapter = client.adapter(tmp_path / "jobs.sqlite3", output)
    assert adapter._observe_pods("job-uid") == [item | {"apiVersion": "v1", "kind": "Pod"}]
    assert "kind" not in item  # The Kubernetes list response is not modified.
    client.request = lambda *_args: {
        "apiVersion": "v1",
        "kind": "PodList",
        "metadata": {},
        "items": [item | {"kind": "Job"}],
    }
    with pytest.raises(RemoteTargetError, match="invalid resource type"):
        adapter._observe_pods("job-uid")


def test_adapter_discovers_owned_pod_after_unrelated_namespace_page(tmp_path):
    client, output, _ = _fixture(tmp_path)
    calls = []
    item = {
        "metadata": {
            "name": "owned",
            "uid": "pod-uid",
            "ownerReferences": [{"kind": "Job", "uid": "job-uid"}],
        }
    }

    def request(method, path):
        calls.append(path)
        return {
            "apiVersion": "v1",
            "kind": "PodList",
            "metadata": {"continue": "next" if len(calls) == 1 else ""},
            "items": [{"metadata": {"uid": "other"}}] * 100 if len(calls) == 1 else [item],
        }

    client.request = request
    adapter = client.adapter(tmp_path / "jobs.sqlite3", output)
    assert adapter._observe_pods("job-uid") == [item | {"apiVersion": "v1", "kind": "Pod"}]
    assert len(calls) == 2
    assert calls[1].endswith("?limit=100&continue=next")


def test_kubernetes_transport_uses_scoped_token_and_bounded_request(tmp_path):
    client, _, _ = _fixture(tmp_path)
    requests = []

    class Response:
        def __init__(self):
            self.content = BytesIO(b'{"metadata":{"uid":"real-uid"}}')

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return self.content.read(limit)

        def read1(self, limit):
            assert limit <= 65536
            return self.content.read(limit)

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response()

    client.opener = Opener()
    observed = client.request("GET", client.path("namespace", "kube-system"))
    assert observed == {"metadata": {"uid": "real-uid"}}
    request, timeout = requests[0]
    assert request.full_url == "https://kubernetes.example.invalid/api/v1/namespaces/kube-system"
    assert request.headers["Authorization"] == "Bearer fixture-token"
    assert timeout == 5
    with pytest.raises(RemoteTargetError, match="fixed API"):
        client.request("GET", "https://elsewhere.invalid/")
    assert len(requests) == 1
