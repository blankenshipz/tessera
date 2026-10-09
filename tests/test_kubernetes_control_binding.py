"""Control-issued admitted identities bind to the pure Kubernetes contract."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from test_execution import _grant_payload, _owner, _snapshot_release, _task, _worker
from test_goal_to_decision import _command, _design

from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state
from tessera_worker.kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesConnectivityEvidence,
    KubernetesJobBinding,
    KubernetesNetworkPolicyEvidence,
    domain_identifier_from_resource_name,
    job_manifest,
    parse_job,
)


def _control_issued_delivery(tmp_path: Path):
    state = tmp_path / "state"
    initialize_state(state)
    migrate_state(state)
    owner = _owner(state)
    store = Store(state)
    _, project = store.create_project(
        owner, "project", canonical_digest({"name": "Pilot"}), {"name": "Pilot"}
    )
    worker = _worker(state, owner, project["id"], "worker-token")
    snapshot, _release = _snapshot_release(state, owner, project["id"], worker["id"])
    task = _task(state, owner, project["id"], snapshot["digest"], "First")
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    assert (
        execution.configure_project_execution(
            owner, project["id"], "limits", canonical_digest(limits), limits
        )[0]
        == 201
    )
    grant_payload = _grant_payload(task, worker, {"id": snapshot["id"]})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(grant_payload), grant_payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    return state, owner, worker, grant, lease


def test_control_issued_admitted_attempt_and_lease_bind_without_renaming(tmp_path: Path) -> None:
    state, owner, worker, grant, lease = _control_issued_delivery(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    admitted = _command(
        records.admit_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
            "expected_design_revision": experiment["current_design_revision"],
            "grant_id": grant["id"],
        },
    )
    package_response = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "kubernetes-identity-package",
        canonical_digest({"expected_epoch": lease["epoch"]}),
        lease["epoch"],
    )[1]
    package = package_response["package"]
    assert package["experiment"]["admitted_attempt_id"] == admitted["id"]
    assert admitted["id"].startswith("experiment_admitted_attempt_") and "_" in admitted["id"]
    assert package["lease_id"] == lease["id"] and "_" in lease["id"]
    binding = KubernetesJobBinding(
        operation_id=package["operation_id"],
        attempt_id=admitted["id"],
        lease_id=lease["id"],
        epoch=lease["epoch"],
        package_sha256=package_response["package_digest"],
        image=package["pinned_image"],
        command=("opencode", "serve"),
        active_deadline_seconds=300,
    )
    now = datetime.now(UTC)
    cluster = KubernetesClusterConfig(
        kubeconfig="/private/fixture/kubeconfig",
        kubeconfig_sha256="a" * 64,
        context="minikube-fixture",
        profile="development-minikube-opencode-v1",
        server="https://127.0.0.1:8443",
        server_sha256="b" * 64,
        ca_sha256="c" * 64,
        namespace="tessera-fixture",
        cluster_uid="cluster-fixture",
        namespace_uid="namespace-fixture",
        network_none_evidence=KubernetesNetworkPolicyEvidence(
            "network-policy-enforced-v1",
            "tessera-deny-all",
            "policy-fixture",
            "f" * 64,
            "cluster-fixture",
            "namespace-fixture",
            now,
            KubernetesConnectivityEvidence("allowed-pod", "server-pod", "allowed", 3, now),
            KubernetesConnectivityEvidence("denied-pod", "server-pod", "denied", 3, now),
        ),
    )
    manifest = job_manifest(binding, cluster)
    annotations = manifest["metadata"]["annotations"]
    labels = manifest["metadata"]["labels"]
    assert annotations["tessera.dev/attempt-id"] == admitted["id"]
    assert annotations["tessera.dev/lease-id"] == lease["id"]
    assert domain_identifier_from_resource_name(labels["tessera.dev/attempt"]) == admitted["id"]
    assert domain_identifier_from_resource_name(labels["tessera.dev/lease"]) == lease["id"]
    observed = deepcopy(manifest)
    observed["metadata"]["uid"] = "job-fixture"
    observed["status"] = {"active": 1}
    assert parse_job(observed, binding, cluster).state == "running"
