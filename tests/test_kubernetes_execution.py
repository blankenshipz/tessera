"""Fake-client integration evidence for the admitted Kubernetes adapter slice."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_execution import _grant_payload, _owner, _snapshot_release, _worker
from test_goal_to_decision import _command, _design

from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state
from tessera_worker.kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesContractError,
    KubernetesNetworkPolicyEvidence,
    job_manifest,
)
from tessera_worker.kubernetes_execution import (
    KubernetesExecutionAdapter,
    KubernetesExecutionError,
    KubernetesExecutionPackage,
    KubernetesOutputBinding,
)
from tessera_worker.kubernetes_provider import KubernetesUnresolvedError
from tessera_worker.opencode_harness import TesseraBinding


def _cluster(**changes: object) -> KubernetesClusterConfig:
    values: dict[str, object] = {
        "kubeconfig": "/private/fixture/kubeconfig",
        "kubeconfig_sha256": "a" * 64,
        "context": "minikube-fixture",
        "profile": "development-minikube-opencode-v1",
        "server": "https://127.0.0.1:8443",
        "server_sha256": "b" * 64,
        "ca_sha256": "c" * 64,
        "namespace": "tessera-fixture",
        "cluster_uid": "cluster-fixture",
        "namespace_uid": "namespace-fixture",
        "network_none_evidence": KubernetesNetworkPolicyEvidence(
            "network-policy-enforced-v1", "tessera-deny-all", "policy-fixture", "f" * 64
        ),
    }
    values.update(changes)
    return KubernetesClusterConfig(**values)  # type: ignore[arg-type]


def _admitted_package(tmp_path: Path) -> tuple[dict, dict, KubernetesExecutionPackage]:
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
    _, task = store.create_task(
        owner,
        project["id"],
        "task-kubernetes",
        canonical_digest({"name": "Kubernetes"}),
        {
            "title": "Kubernetes",
            "purpose": "Exercise admitted Kubernetes execution.",
            "budget_cents": 100,
            "max_duration_seconds": 300,
        },
    )
    revision = {
        "expected_revision": task["revision"],
        "title": "Kubernetes",
        "purpose": "Exercise admitted Kubernetes execution.",
        "budget_cents": 100,
        "max_duration_seconds": 300,
        "execution_spec": {
            "argv": ["opencode", "serve"],
            "input_snapshot_digest": snapshot["digest"],
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "kubernetes-opencode",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    _, task = store.revise_task_plan(
        owner, task["id"], "revise-kubernetes", canonical_digest(revision), revision
    )
    _, task = store.approve_task(
        owner,
        task["id"],
        "approve-kubernetes",
        canonical_digest({"expected_revision": task["revision"]}),
        task["revision"],
        task["current_plan_digest"],
    )
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    execution.configure_project_execution(
        owner, project["id"], "limits", canonical_digest(limits), limits
    )
    grant_payload = _grant_payload(task, worker, {"id": snapshot["id"]})
    _, grant = execution.issue_execution_grant(
        owner,
        project["id"],
        "grant",
        canonical_digest(grant_payload),
        grant_payload,
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, project["id"], payload=_design(grant=grant)
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
    response = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "kubernetes-package",
        canonical_digest({"expected_epoch": lease["epoch"], "execution_adapter": "kubernetes-job"}),
        lease["epoch"],
        admitted_attempt_id=admitted["id"],
        execution_adapter="kubernetes-job",
    )[1]
    package = KubernetesExecutionPackage.from_admitted_package(
        response,
        KubernetesOutputBinding("outputs", "/outputs", "pvc-fixture", "d" * 64),
    )
    return response, worker, package


class _FakeKubernetes:
    def __init__(
        self, package: KubernetesExecutionPackage, cluster: KubernetesClusterConfig
    ) -> None:
        self.package = package
        self.cluster = cluster
        self.jobs: dict[str, dict] = {}
        self.pods: list[dict] = []
        self.creates = 0

    def create(self, _manifest: object) -> None:
        self.creates += 1
        self.jobs[self.package.binding.job_name] = self.job()
        return None

    def job(self, state: str = "running") -> dict:
        observed = deepcopy(job_manifest(self.package.binding, self.cluster))
        observed["metadata"]["uid"] = "job-fixture"
        observed["status"] = {"active": 1} if state == "running" else {state: 1}
        return observed

    def pod(self, state: str = "Running", owner_uid: str = "job-fixture") -> dict:
        template = deepcopy(job_manifest(self.package.binding, self.cluster)["spec"]["template"])
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                **template["metadata"],
                "namespace": self.cluster.namespace,
                "uid": "pod-fixture",
                "ownerReferences": [{"kind": "Job", "uid": owner_uid, "controller": True}],
            },
            "spec": template["spec"],
            "status": {"phase": state},
        }

    def delete(self, name: str, _uid: str) -> None:
        self.jobs.pop(name, None)
        self.pods.clear()

    def adapter(self, path: Path, cluster: KubernetesClusterConfig | None = None):
        return KubernetesExecutionAdapter(
            path,
            cluster or self.cluster,
            inspect_job=self.jobs.get,
            create_job=self.create,
            delete_job=self.delete,
            observe_pods=lambda _uid: self.pods,
        )


def test_admitted_package_translates_only_allowlisted_fields_and_records_handoff(
    tmp_path: Path,
) -> None:
    response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "kubernetes.sqlite3")

    status = adapter.submit(package)

    assert response["package"]["provider_enabled"] is False
    assert status.job is not None and fake.creates == 1
    manifest = fake.jobs[package.binding.job_name]
    assert manifest["metadata"]["namespace"] == "tessera-fixture"
    assert manifest["spec"]["template"]["spec"]["volumes"][-1]["persistentVolumeClaim"] == {
        "claimName": "outputs"
    }
    fake.jobs[package.binding.job_name] = fake.job("succeeded")
    fake.pods = [fake.pod("Succeeded")]
    assert adapter.reconcile(package).observed_state == "succeeded"
    handoff = adapter.artifact_collection_handoff(package)
    assert handoff.pvc_uid == "pvc-fixture" and handoff.output_paths == ("result.txt",)


def test_target_spec_identity_and_pod_ownership_fail_closed(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "kubernetes.sqlite3"
    adapter = fake.adapter(journal)
    fake.jobs[package.binding.job_name] = fake.job()
    adapter.submit(package)

    with pytest.raises(KubernetesExecutionError, match="target or package"):
        fake.adapter(journal, _cluster(context="other-target")).reconcile(package)
    unsafe = fake.job()
    unsafe["spec"]["template"]["spec"]["containers"][0]["image"] = "unsafe:latest"
    fake.jobs[package.binding.job_name] = unsafe
    with pytest.raises(KubernetesContractError, match="immutable spec"):
        adapter.reconcile(package)
    fake.jobs[package.binding.job_name] = fake.job()
    fake.pods = [fake.pod(owner_uid="foreign-job")]
    with pytest.raises(KubernetesContractError, match="not owned"):
        adapter.reconcile(package)


def test_lost_create_cancel_before_create_and_restart_reconcile_are_fenced(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "kubernetes.sqlite3"
    adapter = fake.adapter(journal)

    def lost_create(_manifest: object) -> None:
        fake.creates += 1
        return None

    adapter = KubernetesExecutionAdapter(
        journal,
        fake.cluster,
        inspect_job=fake.jobs.get,
        create_job=lost_create,
        delete_job=fake.delete,
        observe_pods=lambda _uid: fake.pods,
    )
    with pytest.raises(KubernetesUnresolvedError, match="lost"):
        adapter.submit(package)
    with pytest.raises(KubernetesUnresolvedError, match="inspect only"):
        fake.adapter(journal).submit(package)
    assert fake.creates == 1

    (tmp_path / "second").mkdir()
    _response, _worker, second = _admitted_package(tmp_path / "second")
    second_fake = _FakeKubernetes(second, _cluster())
    second_journal = tmp_path / "cancel.sqlite3"
    cancelled = second_fake.adapter(second_journal).cancel(second)
    assert cancelled.requested and second_fake.creates == 0
    with pytest.raises(KubernetesUnresolvedError, match="durable cancellation"):
        second_fake.adapter(second_journal).submit(second)

    (tmp_path / "third").mkdir()
    _response, _worker, third = _admitted_package(tmp_path / "third")
    restart_fake = _FakeKubernetes(third, _cluster())
    restart_journal = tmp_path / "restart.sqlite3"
    restart_fake.adapter(restart_journal).submit(third)
    restarted = restart_fake.adapter(restart_journal)
    assert restarted.reconcile(third).observed_state == "running"
    assert restart_fake.creates == 1


def test_contradictory_status_and_opencode_session_association(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "kubernetes.sqlite3")
    fake.jobs[package.binding.job_name] = fake.job()
    adapter.submit(package)
    fake.jobs[package.binding.job_name]["status"] = {"active": 1, "succeeded": 1}
    with pytest.raises(KubernetesContractError, match="contradictory"):
        adapter.reconcile(package)
    fake.jobs[package.binding.job_name] = fake.job()
    binding = TesseraBinding(
        package.package["org_id"],
        package.package["project_id"],
        package.package["task_id"],
        package.operation_id,
        package.binding.epoch,
        "harness-fixture",
        package.binding.attempt_id,
        package.package["grant_id"],
        package.binding.attempt_id,
        session_id="session-fixture",
    )
    adapter.associate_opencode_session(package, binding)
    adapter.associate_opencode_session(package, binding)
    with pytest.raises(KubernetesExecutionError, match="session identity"):
        adapter.associate_opencode_session(
            package,
            replace(binding, session_id="other-session"),
        )
