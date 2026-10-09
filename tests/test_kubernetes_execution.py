"""Fake-client integration evidence for the admitted Kubernetes adapter slice."""

from __future__ import annotations

import sqlite3
import threading
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_agent_authority import assert_empty_runtime_v55, strip_runtime_v55
from test_execution import _grant_payload, _owner, _snapshot_release, _worker
from test_goal_to_decision import _command, _design

from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state
from tessera_worker.kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesConnectivityEvidence,
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
    now = datetime.now(UTC)
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
    }
    values.update(changes)
    return KubernetesClusterConfig(**values)  # type: ignore[arg-type]


_PRE_V35_HISTORY = (
    "principals",
    "credentials",
    "projects",
    "events",
    "idempotency",
    "control_recovery",
    "source_snapshots",
    "snapshot_files",
    "agent_authority_grants",
)


def _old_v35_state(state: Path):
    """Disposable historical fixture: remove empty55 additions, not authority rows."""
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        strip_runtime_v55(connection)
        connection.execute("DROP TRIGGER kubernetes_dispatches_fence_cancel")
        connection.execute("DROP TABLE kubernetes_dispatches")
        connection.execute("DROP TABLE execution_runtime_packages")
        connection.execute("UPDATE schema_meta SET schema_version=35 WHERE singleton=1")
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 35
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in _PRE_V35_HISTORY
        }


def _admitted_package(
    tmp_path: Path,
    *,
    migrate_from_v35: bool = False,
    max_duration_seconds: int = 300,
    grant_seconds: int | None = None,
    argv: list[str] | None = None,
) -> tuple[dict, dict, KubernetesExecutionPackage]:
    state = tmp_path / "state"
    initialize_state(state)
    before = None
    if migrate_from_v35:
        before = _old_v35_state(state)
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert_empty_runtime_v55(connection)
        if before is not None:
            assert all(
                connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows
                for table, rows in before.items()
            )
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
            "max_duration_seconds": max_duration_seconds,
        },
    )
    revision = {
        "expected_revision": task["revision"],
        "title": "Kubernetes",
        "purpose": "Exercise admitted Kubernetes execution.",
        "budget_cents": 100,
        "max_duration_seconds": max_duration_seconds,
        "execution_spec": {
            "argv": argv if argv is not None else ["opencode", "serve"],
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
    if grant_seconds is not None:
        grant_payload["expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=grant_seconds)
        ).isoformat()
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

    def create(self, manifest: object) -> None:
        self.creates += 1
        observed = deepcopy(manifest)
        assert isinstance(observed, dict)
        observed["metadata"]["uid"] = "job-fixture"
        observed["status"] = {"active": 1}
        self.jobs[self.package.binding.job_name] = observed
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

    def delete(self, name: str, uid: str) -> None:
        current = self.jobs.get(name)
        if current is None or current["metadata"]["uid"] != uid:
            raise RuntimeError("Kubernetes UID precondition rejected replacement")
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


def test_submission_step_rejects_stale_authority_before_final_job_create(tmp_path):
    _, _, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "jobs.sqlite3")
    adapter._prepare_output = lambda _: None
    generation = 0

    def current():
        nonlocal generation
        generation += 1
        if generation == 3:
            raise PermissionError("cancelled/stale generation at final Job boundary")
        return 2.0

    # Journal conservatively records uncertain create intent before invoking the
    # final authority fence; denial cannot be relabelled as a known native reply.
    with pytest.raises(KubernetesUnresolvedError, match="inspect only"):
        adapter.submit(package, before_step=current)
    assert fake.creates == 0
    assert adapter.failure_evidence(package)["stage"] == "job_create"


def test_submission_step_timeout_resident_callback_never_overlaps_or_creates(tmp_path):
    import threading

    _, _, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "jobs.sqlite3")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def resident(_):
        calls.append("inspect")
        entered.set()
        release.wait(3)
        return None

    adapter._inspect_job = resident
    try:
        with pytest.raises(KubernetesExecutionError, match="deadline exceeded"):
            adapter.submit(package, before_step=lambda: 0.02)
        assert entered.is_set()
        with pytest.raises(KubernetesExecutionError, match="still in progress"):
            adapter.submit(package, before_step=lambda: 1.0)
        assert calls == ["inspect"] and fake.creates == 0
    finally:
        release.set()
        adapter._inflight.result(timeout=3)


def test_submission_step_thread_start_failure_never_creates(tmp_path, monkeypatch):
    import threading

    _, _, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "jobs.sqlite3")

    def unavailable(_):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    with pytest.raises(KubernetesExecutionError, match="callback is unavailable"):
        adapter.submit(package, before_step=lambda: 2.0)
    assert adapter._inflight is None and fake.creates == 0


@pytest.mark.parametrize(
    ("job_state", "pod_states"),
    [
        ("failed", []),
        ("failed", ["Running"]),
        ("failed", ["Failed", "Running"]),
        ("running", ["Failed"]),
        ("succeeded", ["Running"]),
        ("succeeded", []),
    ],
)
def test_incomplete_terminal_evidence_remains_unknown(tmp_path, job_state, pod_states):
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "kubernetes.sqlite3")
    adapter.submit(package)
    fake.jobs[package.binding.job_name] = fake.job(job_state)
    fake.pods = [fake.pod(state) for state in pod_states]
    # Distinct Pod identities make mixed running/failed observations realistic.
    for index, pod in enumerate(fake.pods):
        pod["metadata"]["uid"] = f"pod-{index}"
    status = adapter.reconcile(package)
    assert status.observed_state == "unknown"
    assert status.termination_confirmed is False
    assert fake.creates == 1


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


def test_replacement_uid_is_rejected_and_cannot_replace_collection_identity(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "kubernetes.sqlite3")
    fake.jobs[package.binding.job_name] = fake.job("succeeded")
    fake.pods = [fake.pod("Succeeded")]
    assert adapter.submit(package).observed_state == "succeeded"
    original = adapter.artifact_collection_handoff(package)

    replacement = fake.job("succeeded")
    replacement["metadata"]["uid"] = "replacement-job"
    fake.jobs[package.binding.job_name] = replacement
    with pytest.raises(KubernetesContractError, match="UID conflicts"):
        adapter.reconcile(package)
    with pytest.raises(KubernetesExecutionError, match="observed successful"):
        adapter.artifact_collection_handoff(package)
    assert original.job_uid == "job-fixture"


def test_cancellation_rejects_a_replacement_job_uid(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "kubernetes.sqlite3"
    fake.jobs[package.binding.job_name] = fake.job()
    adapter = fake.adapter(journal)
    assert adapter.submit(package).job is not None

    def replace_on_delete(name: str, uid: str) -> None:
        replacement = fake.job()
        replacement["metadata"]["uid"] = "replacement-job"
        fake.jobs[name] = replacement
        fake.delete(name, uid)

    cancelling = KubernetesExecutionAdapter(
        journal,
        fake.cluster,
        inspect_job=fake.jobs.get,
        create_job=fake.create,
        delete_job=replace_on_delete,
        observe_pods=lambda _uid: fake.pods,
    )
    cancelled = cancelling.cancel(package)
    assert cancelled.termination_confirmed is False
    assert fake.jobs[package.binding.job_name]["metadata"]["uid"] == "replacement-job"


def test_cancel_persists_both_fences_before_failed_inspection_and_restart(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    journal = tmp_path / "kubernetes.sqlite3"

    def failed_inspection(_name: str) -> None:
        import sqlite3

        with sqlite3.connect(journal) as connection:
            assert connection.execute(
                "SELECT cancellation_requested FROM kubernetes_execution_bindings "
                "WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT cancellation_requested FROM kubernetes_jobs WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone() == (1,)
        raise RuntimeError("inspection failed")

    adapter = KubernetesExecutionAdapter(
        journal,
        _cluster(),
        inspect_job=failed_inspection,
        create_job=lambda _manifest: pytest.fail("must not create"),
        delete_job=lambda _name, _uid: pytest.fail("must not delete"),
        observe_pods=lambda _uid: pytest.fail("must not observe Pods"),
    )
    with pytest.raises(RuntimeError, match="inspection failed"):
        adapter.cancel(package)
    with pytest.raises(KubernetesUnresolvedError, match="durable cancellation"):
        adapter.submit(package)


def test_missing_contradictory_and_zero_pod_observations_block_collection(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    adapter = fake.adapter(tmp_path / "kubernetes.sqlite3")
    fake.jobs[package.binding.job_name] = fake.job("succeeded")
    fake.pods = [fake.pod("Succeeded")]
    assert adapter.submit(package).observed_state == "succeeded"

    fake.jobs.clear()
    assert adapter.reconcile(package).observed_state == "unknown"
    with pytest.raises(KubernetesExecutionError, match="observed successful"):
        adapter.artifact_collection_handoff(package)

    fake.jobs[package.binding.job_name] = fake.job("succeeded")
    fake.jobs[package.binding.job_name]["status"] = {"active": 1, "succeeded": 1}
    with pytest.raises(KubernetesContractError, match="contradictory"):
        adapter.reconcile(package)
    with pytest.raises(KubernetesExecutionError, match="observed successful"):
        adapter.artifact_collection_handoff(package)

    fake.jobs[package.binding.job_name] = fake.job("succeeded")
    fake.pods = []
    assert adapter.reconcile(package).observed_state == "unknown"
    with pytest.raises(KubernetesExecutionError, match="observed successful"):
        adapter.artifact_collection_handoff(package)


def test_missing_job_reconciles_recorded_owned_pods_across_restart_without_create(
    tmp_path: Path,
) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "kubernetes.sqlite3"
    adapter = fake.adapter(journal)
    assert adapter.submit(package).observed_state == "running"

    fake.jobs.clear()
    fake.pods = [fake.pod("Running")]
    missing_with_pod = adapter.reconcile(package)
    assert missing_with_pod.observed_state == "unknown"
    assert [pod.pod_uid for pod in missing_with_pod.pods] == ["pod-fixture"]

    restarted = fake.adapter(journal)
    fake.pods.clear()
    eventual_absence = restarted.reconcile(package)
    assert eventual_absence.observed_state == "unknown" and not eventual_absence.pods
    proof = restarted.cancel(package)
    assert proof.termination_confirmed is True
    assert fake.creates == 1
    with pytest.raises(KubernetesExecutionError, match="observed successful"):
        restarted.artifact_collection_handoff(package)


def test_repeated_read_failures_keep_unknown_and_recorded_job_recovery_identity(
    tmp_path: Path,
) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "kubernetes.sqlite3"

    def unreadable_pods(_job_uid: str) -> list[dict]:
        raise RuntimeError("temporary Pod read failure")

    adapter = KubernetesExecutionAdapter(
        journal,
        fake.cluster,
        inspect_job=fake.jobs.get,
        create_job=fake.create,
        delete_job=fake.delete,
        observe_pods=unreadable_pods,
    )
    assert adapter.submit(package).observed_state == "unknown"
    assert adapter.reconcile(package).observed_state == "unknown"

    with sqlite3.connect(journal) as connection:
        assert connection.execute(
            "SELECT observed_state,job_uid FROM kubernetes_execution_bindings WHERE operation_id=?",
            (package.operation_id,),
        ).fetchone() == ("unknown", "job-fixture")
    assert fake.creates == 1
    with pytest.raises(KubernetesExecutionError, match="observed successful"):
        adapter.artifact_collection_handoff(package)


def test_provider_callback_deadline_returns_unknown_and_prevents_overlap(tmp_path: Path) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def blocked_inspection(_name: str) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(1)
        return None

    adapter = KubernetesExecutionAdapter(
        tmp_path / "kubernetes.sqlite3",
        _cluster(),
        inspect_job=blocked_inspection,
        create_job=lambda _manifest: pytest.fail("must not create"),
        delete_job=lambda _name, _uid: pytest.fail("must not delete"),
        observe_pods=lambda _uid: (),
    )
    started = time.monotonic()
    assert adapter.reconcile(package, timeout_seconds=0.03).observed_state == "unknown"
    assert time.monotonic() - started < 0.5 and entered.is_set()
    assert adapter.reconcile(package, timeout_seconds=0.03).observed_state == "unknown"
    assert calls == 1
    release.set()
    adapter._inflight.result(timeout=0.5)


@pytest.mark.parametrize(
    "new_observation",
    ("job_read_error", "pod_read_error", "running_job", "pending_job", "unknown_job"),
)
def test_new_contradictory_observation_supersedes_old_cancellation_confirmation(
    tmp_path: Path, new_observation: str
) -> None:
    _response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "kubernetes.sqlite3"
    adapter = fake.adapter(journal)
    assert adapter.submit(package).observed_state == "running"
    assert adapter.cancel(package).termination_confirmed is True
    assert adapter.cancellation_proof(package)["termination_confirmed"] is True

    if new_observation in {"running_job", "pending_job", "unknown_job"}:
        fake.jobs[package.binding.job_name] = fake.job(new_observation.removesuffix("_job"))
        if new_observation == "unknown_job":
            fake.jobs[package.binding.job_name]["status"] = {"active": "unavailable"}

    failed = KubernetesExecutionAdapter(
        journal,
        fake.cluster,
        inspect_job=(
            (lambda _name: (_ for _ in ()).throw(RuntimeError("new failed Job read")))
            if new_observation == "job_read_error"
            else fake.jobs.get
        ),
        create_job=fake.create,
        delete_job=fake.delete,
        observe_pods=(
            (lambda _uid: (_ for _ in ()).throw(RuntimeError("new failed Pod read")))
            if new_observation == "pod_read_error"
            else lambda _uid: fake.pods
        ),
    )
    status = failed.reconcile(package)
    expected = {
        "running_job": "running",
        "pending_job": "pending",
        "unknown_job": "unknown",
    }.get(new_observation, "unknown")
    assert status.observed_state == expected
    proof = failed.cancellation_proof(package)
    assert proof["termination_confirmed"] is False
    assert proof["reconciliation_required"] is True
