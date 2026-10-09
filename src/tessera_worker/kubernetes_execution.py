"""Versioned fake-client Kubernetes execution adapter.

The adapter consumes only a control-issued, admitted Kubernetes package.  It
records a local immutable target/Job intent before invoking injected Kubernetes
callbacks.  It is intentionally not a Kubernetes client: target attachment,
credentials, provider enablement, collection, and network/storage enforcement
remain outside this fake-client conformance seam.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .execution_contracts import (
    AccountingObservation,
    CollectionHandoff,
    ExecutionCapabilities,
    ExecutionEnvelope,
    Observation,
    OperationHandle,
    PreflightRequest,
    PreflightResult,
    TargetBinding,
)
from .execution_contracts import CancellationObservation as NeutralCancellationObservation
from .kubernetes_contracts import (
    KubernetesCancellationObservation,
    KubernetesClusterConfig,
    KubernetesContractError,
    KubernetesJobBinding,
    KubernetesJobJournal,
    KubernetesJobObservation,
    KubernetesPodObservation,
    KubernetesProviderIdentity,
    canonical_json,
    cluster_identity_digest,
    parse_pod,
)
from .opencode_harness import TesseraBinding

MAX_PROVIDER_CALL_SECONDS = 30.0


class KubernetesExecutionError(RuntimeError):
    """A package, identity, or durable adapter binding is not safe to use."""

    def __init__(self, message, *, diagnostic_code=None):
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


@dataclass(frozen=True)
class KubernetesOutputBinding:
    """Observed PVC identity and optional per-operation output mount isolation."""

    pvc_name: str
    mount_path: str
    pvc_uid: str
    pvc_spec_sha256: str
    operation_scoped: bool = False

    def job_fields(self) -> dict[str, Any]:
        return {
            "output_pvc": self.pvc_name,
            "output_mount_path": self.mount_path,
            "output_pvc_uid": self.pvc_uid,
            "output_pvc_spec_sha256": self.pvc_spec_sha256,
            "output_operation_scoped": self.operation_scoped,
        }


@dataclass(frozen=True)
class KubernetesExecutionPackage:
    """A frozen Kubernetes view of one normal control package response."""

    package: dict[str, Any]
    package_digest: str
    output: KubernetesOutputBinding
    binding: KubernetesJobBinding
    approved_duration_seconds: int
    lease_issued_at: datetime
    lease_expires_at: datetime
    hard_expires_at: datetime
    dataset_resources: tuple[dict, ...] = ()

    @classmethod
    def from_admitted_package(
        cls,
        response: Mapping[str, object],
        output: KubernetesOutputBinding,
        *,
        input_bundle: dict | None = None,
        dataset_namespace: str | None = None,
    ) -> KubernetesExecutionPackage:
        package = response.get("package")
        package_digest = response.get("package_digest")
        operation = response.get("operation")
        lease = response.get("lease")
        admitted_attempt_id = response.get("admitted_attempt_id")
        if (
            not isinstance(package, dict)
            or not isinstance(operation, Mapping)
            or not isinstance(lease, Mapping)
        ):
            raise KubernetesExecutionError(
                "Kubernetes adapter requires a normal admitted package response."
            )
        if not isinstance(package_digest, str) or len(package_digest) != 64:
            raise KubernetesExecutionError("Kubernetes package digest is unavailable.")
        if hashlib.sha256(canonical_json(package)).hexdigest() != package_digest:
            raise KubernetesExecutionError("Kubernetes package digest does not match its content.")
        experiment = package.get("experiment")
        plan = package.get("plan")
        workload = package.get("kubernetes_workload")
        if (
            package.get("version") != 1
            or package.get("adapter_version") != "kubernetes-job-v1"
            or package.get("profile_version") != "development-minikube-opencode-v1"
            or package.get("provider_enabled") is not False
            or not isinstance(experiment, Mapping)
            or not isinstance(plan, Mapping)
            or not isinstance(workload, Mapping)
            or not isinstance(admitted_attempt_id, str)
            or experiment.get("admitted_attempt_id") != admitted_attempt_id
            or operation.get("operation_id") != package.get("operation_id")
            or operation.get("package_digest") != package_digest
        ):
            raise KubernetesExecutionError(
                "Kubernetes package is not the admitted provider-disabled contract."
            )
        if set(workload) != {"active_deadline_seconds", "command", "network_mode"}:
            raise KubernetesExecutionError("Kubernetes package has unallowlisted workload fields.")
        command = workload["command"]
        dataset_input, dataset_resources = None, ()
        if isinstance(command, list) and command[:3] == ["python", "-m", "tessera_starter"]:
            from .dataset_inputs import prepare_dataset_inputs

            spec = plan.get("execution_spec")
            if (
                not isinstance(spec, Mapping)
                or spec.get("input_snapshot_digest") != package.get("snapshot_digest")
                or type(spec.get("max_output_bytes")) is not int
                or not 1 <= spec["max_output_bytes"] <= 1024 * 1024
                or len(command) != 11
                or command[5] != "--manifest-digest"
                or input_bundle is None
                or dataset_namespace is None
            ):
                raise KubernetesExecutionError(
                    "Dataset plan requires actual lease inputs and exact bounded execution."
                )
            try:
                dataset_input, resources = prepare_dataset_inputs(
                    input_bundle,
                    snapshot_digest=_string(package, "snapshot_digest"),
                    manifest_digest=command[6],
                    lease_id=_string(package, "lease_id"),
                    epoch=_positive_int(package, "epoch"),
                    worker_id=_string(package, "worker_principal_id"),
                    operation_id=_string(package, "operation_id"),
                    namespace=dataset_namespace,
                    grant_id=_string(package, "grant_id"),
                    project_id=_string(package, "project_id"),
                )
                dataset_resources = tuple(resources)
            except (TypeError, ValueError) as error:
                raise KubernetesExecutionError(
                    "Dataset lease input verification failed."
                ) from error
        elif input_bundle is not None:
            raise KubernetesExecutionError("Dataset inputs supplied to a nonstarter workload.")
        output_paths = package.get("output_paths")
        if (
            not isinstance(command, list)
            or any(not isinstance(item, str) for item in command)
            or not isinstance(output_paths, list)
            or any(not isinstance(path, str) for path in output_paths)
        ):
            raise KubernetesExecutionError("Kubernetes workload command is malformed.")
        try:
            lease_issued_at = _utc_datetime(lease, "issued_at")
            lease_expires_at = _utc_datetime(lease, "expires_at")
            hard_expires_at = _utc_datetime(lease, "hard_expires_at")
            package_hard_expires_at = _utc_datetime(package, "hard_expires_at")
            binding = KubernetesJobBinding(
                operation_id=_string(package, "operation_id"),
                attempt_id=admitted_attempt_id,
                lease_id=_string(package, "lease_id"),
                epoch=_positive_int(package, "epoch"),
                package_sha256=package_digest,
                image=_string(package, "pinned_image"),
                command=tuple(command),
                active_deadline_seconds=_positive_int(workload, "active_deadline_seconds"),
                network_mode=_network_mode(workload),
                dataset_input=dataset_input,
                **output.job_fields(),
            )
            approved_duration_seconds = _positive_int(plan, "max_duration_seconds")
        except (TypeError, ValueError) as error:
            raise KubernetesExecutionError(
                "Kubernetes package workload violates the fixed Job contract."
            ) from error
        if (
            lease.get("id") != binding.lease_id
            or lease.get("grant_id") != package.get("grant_id")
            or lease.get("worker_principal_id") != package.get("worker_principal_id")
            or lease.get("epoch") != binding.epoch
            or package_hard_expires_at != hard_expires_at
            or lease_issued_at > lease_expires_at
            or lease_expires_at > hard_expires_at
        ):
            raise KubernetesExecutionError(
                "Kubernetes package lease identity or expiry is inconsistent."
            )
        return cls(
            dict(package),
            package_digest,
            output,
            binding,
            approved_duration_seconds,
            lease_issued_at,
            lease_expires_at,
            hard_expires_at,
            dataset_resources,
        )

    @property
    def operation_id(self) -> str:
        return self.binding.operation_id

    def execution_envelope(self, target: TargetBinding) -> ExecutionEnvelope:
        """Translate this admitted package into the neutral frozen envelope.

        The opaque payload intentionally remains this validated Kubernetes
        package.  The neutral contract never learns Job, Pod, PVC, kubeconfig,
        or network-policy fields from it.
        """
        return ExecutionEnvelope(
            operation_id=self.operation_id,
            package_digest=self.package_digest,
            target=target,
            lease_id=self.binding.lease_id,
            epoch=self.binding.epoch,
            deadline=min(
                self.hard_expires_at,
                self.lease_expires_at,
                self.lease_issued_at + timedelta(seconds=self.approved_duration_seconds),
            ),
            payload=self,
        )


@dataclass(frozen=True)
class KubernetesExecutionStatus:
    operation_id: str
    job: KubernetesJobObservation | None
    pods: tuple[KubernetesPodObservation, ...]
    observed_state: Literal["pending", "running", "succeeded", "failed", "unknown"]
    provider_id: str | None = None

    @property
    def termination_confirmed(self) -> bool:
        """Normal completion proof, not cancellation/deletion or scientific success."""
        return bool(
            self.provider_id
            and self.job is not None
            and self.job.state in {"succeeded", "failed"}
            and self.observed_state in {"succeeded", "failed"}
            and self.pods
            and all(pod.state in {"succeeded", "failed"} for pod in self.pods)
        )


@dataclass(frozen=True)
class ArtifactCollectionHandoff:
    """Durable collection intent; this adapter neither reads nor releases bytes."""

    operation_id: str
    admitted_attempt_id: str
    target_digest: str
    namespace: str
    namespace_uid: str
    pvc_name: str
    pvc_uid: str
    pvc_spec_sha256: str
    mount_path: str
    output_paths: tuple[str, ...]
    job_uid: str
    pod_uids: tuple[str, ...]
    state: Literal["collect_requested"] = "collect_requested"


class KubernetesExecutionAdapter:
    """Reconcile one deterministic Job through explicitly bound client callbacks."""

    def __init__(
        self,
        journal_path: Path,
        cluster: KubernetesClusterConfig,
        *,
        inspect_job: Callable[[str], Mapping[str, object] | None],
        create_job: Callable[[Mapping[str, object]], Mapping[str, object] | None],
        delete_job: Callable[[str, str], object],
        observe_pods: Callable[[str], Sequence[Mapping[str, object]]],
        prepare_output: Callable[[KubernetesExecutionPackage], None] | None = None,
        collect_output: Callable[[ArtifactCollectionHandoff, KubernetesExecutionPackage], tuple]
        | None = None,
        release_output: Callable[[KubernetesExecutionPackage], None] | None = None,
        fetch_inputs: Callable[[KubernetesExecutionPackage], dict] | None = None,
        prepare_dataset_resources: Callable[[KubernetesExecutionPackage], None] | None = None,
        dataset_client: object | None = None,
    ) -> None:
        self.cluster = cluster
        self._inspect_job = inspect_job
        self._create_job = create_job
        self._delete_job = delete_job
        self._observe_pods = observe_pods
        self._prepare_output = prepare_output
        self._collect_output = collect_output
        self._release_output = release_output
        self._fetch_inputs = fetch_inputs
        self._prepare_dataset_resources = prepare_dataset_resources
        self._prepare_dataset_steps = None
        self.dataset_client = dataset_client
        self._journal = KubernetesJobJournal(journal_path)
        self._database = Path(journal_path)
        self._inflight: Future[Any] | None = None
        self._inflight_lock = threading.Lock()
        self._initialize()
        from .kubernetes_failure_evidence import KubernetesFailureEvidence

        self.failures = KubernetesFailureEvidence(self._database)

    def submit(
        self,
        package: KubernetesExecutionPackage,
        *,
        timeout_seconds: float | None = None,
        before_step: Callable[[], float] | None = None,
    ) -> KubernetesExecutionStatus:
        """Persist target intent, then inspect/create only the deterministic Job."""
        self._bind(package)
        deadline = self._operation_deadline(package, timeout_seconds)

        def call(callback, *arguments):
            # The ordinary supervisor may refresh authenticated authority between
            # commands, never a running command's timeout or immutable hard bound.
            current_deadline = (
                self._operation_deadline(package, before_step())
                if before_step is not None
                else deadline
            )
            return self._call_before(current_deadline, callback, *arguments)

        stage = "job_admission"
        if package.binding.dataset_input is not None and (
            self._fetch_inputs is None or self._prepare_dataset_resources is None
        ):
            raise KubernetesExecutionError(
                "Dataset Job requires current authenticated input delivery."
            )

        def current_dataset():
            if package.binding.dataset_input is None:
                return
            from .dataset_inputs import prepare_dataset_inputs

            bundle = call(self._fetch_inputs, package)
            descriptor, resources = prepare_dataset_inputs(
                bundle,
                snapshot_digest=package.package["snapshot_digest"],
                manifest_digest=package.binding.dataset_input["manifest_digest"],
                lease_id=package.binding.lease_id,
                epoch=package.binding.epoch,
                worker_id=package.package["worker_principal_id"],
                operation_id=package.operation_id,
                namespace=self.cluster.namespace,
                grant_id=package.package["grant_id"],
                project_id=package.package["project_id"],
            )
            if (
                descriptor != package.binding.dataset_input
                or tuple(resources) != package.dataset_resources
            ):
                raise KubernetesExecutionError("Current lease input differs from the frozen Job.")

        def prepare():
            nonlocal stage
            stage = "preparation"
            try:
                current_dataset()
                if package.binding.dataset_input is not None:
                    if self._prepare_dataset_steps is None:
                        call(self._prepare_dataset_resources, package)
                    else:
                        steps = self._prepare_dataset_steps(package)
                        result = None
                        try:
                            while True:
                                try:
                                    callback, arguments = steps.send(result)
                                except StopIteration:
                                    break
                                result = call(callback, *arguments)
                        finally:
                            steps.close()
                call(self._prepare_output, package)
            except Exception as error:
                self.failures.record(package.operation_id, stage, error)
                raise

        def create(manifest):
            nonlocal stage
            stage = "job_create"
            try:
                current_dataset()
                return call(self._create_job, manifest)
            except Exception as error:
                self.failures.record(package.operation_id, stage, error)
                raise

        try:
            job = self._journal.reconcile_create(
                package.binding,
                self.cluster,
                lambda name: call(self._inspect_job, name),
                create,
                prepare=prepare
                if (self._prepare_output is not None or package.binding.dataset_input is not None)
                else None,
            )
            stage = "job_observation"
            observation_deadline = (
                self._operation_deadline(package, before_step())
                if before_step is not None
                else deadline
            )
            return self._record_status(package, job, observation_deadline)
        except Exception as error:
            self.failures.record(package.operation_id, stage, error)
            self._record_unknown(package)
            raise

    def failure_evidence(self, package):
        failure = self.failures.get(package.operation_id)
        collector = getattr(self._prepare_output, "__self__", None)
        if (
            failure is not None
            and failure["stage"] == "preparation"
            and hasattr(collector, "failures")
        ):
            detail = collector.failures.get(package.operation_id)
            if detail is not None and detail["occurred_at"] <= failure["occurred_at"]:
                return detail
        return failure

    def no_start_recovery_candidate(self, package):
        """A queue hint only; fresh original-journal attestation remains separate."""
        with self._connect() as db:
            row = db.execute(
                "SELECT j.binding_json,j.create_intent,j.cancellation_requested AS job_cancelled, "
                "j.job_uid,a.job_uid AS adapter_job_uid,"
                "a.cancellation_requested AS adapter_cancelled, "
                "a.pod_uids_json,a.observed_state FROM kubernetes_jobs j "
                "JOIN kubernetes_execution_bindings a "
                "ON a.operation_id=j.operation_id WHERE j.operation_id=?",
                (package.operation_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["binding_json"] == canonical_json(package.binding.journal_fields).decode()
            and row["create_intent"] == 0
            and row["job_cancelled"] == row["adapter_cancelled"] == 1
            and row["job_uid"] is None
            and row["adapter_job_uid"] is None
            and row["pod_uids_json"] == "[]"
            and row["observed_state"] == "unknown"
        )

    def collect_outputs(self, package):
        if self._collect_output is None:
            raise KubernetesExecutionError("Output collector is not configured.")
        handoff = self.artifact_collection_handoff(package)
        return self._collect_output(handoff, package)

    @property
    def output_collection_enabled(self):
        return self._collect_output is not None

    def release_collector(self, package):
        if self._release_output is not None:
            self._release_output(package)

    def reconcile(
        self, package: KubernetesExecutionPackage, *, timeout_seconds: float | None = None
    ) -> KubernetesExecutionStatus:
        """Observe existing Job/owned Pods only; reconciliation never creates work."""
        self._bind(package)
        deadline = self._operation_deadline(package, timeout_seconds)
        try:
            observed = self._call_before(deadline, self._inspect_job, package.binding.job_name)
        except Exception:
            # A provider read failure is an adverse observation, not an escape
            # from the supervisor that owns this already-submitted operation.
            return self._record_unknown(package)
        try:
            if observed is None:
                return self._record_disappeared_job(package, deadline)
            job = self._journal.reconcile_observation(package.binding, self.cluster, observed)
            return self._record_status(package, job, deadline)
        except Exception:
            self._record_unknown(package)
            raise

    def cancel(
        self, package: KubernetesExecutionPackage, *, timeout_seconds: float | None = None
    ) -> KubernetesCancellationObservation:
        """Persist cancellation before any Kubernetes callback or inspection."""
        self._bind(package)
        self._mark_cancellation_requested(package.operation_id)
        deadline = self._operation_deadline(package, timeout_seconds)
        try:
            observation = self._journal.request_cancel(
                package.binding,
                self.cluster,
                lambda name: self._call_before(deadline, self._inspect_job, name),
                lambda name, uid: self._call_before(deadline, self._delete_job, name, uid),
                lambda uid: self._call_before(deadline, self._observe_pods, uid),
            )
            self._record_cancellation(package, observation)
            return observation
        except Exception:
            self._record_cancellation(
                package, KubernetesCancellationObservation(True, False, False, False)
            )
            self._record_unknown(package)
            raise

    def cancellation_proof(self, package: KubernetesExecutionPackage) -> dict[str, object]:
        """Return the durable cancellation evidence without claiming a stop.

        The first attributed Job UID survives Job deletion and restart.  A missing
        UID or an unconfirmed fresh absence is explicitly reconciliation work.
        """
        self._bind(package)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid,cancellation_json FROM kubernetes_execution_bindings "
                "WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
        if row is None:
            raise KubernetesExecutionError("Kubernetes operation intent disappeared.")
        proof = json.loads(row["cancellation_json"]) if row["cancellation_json"] else {}
        if not isinstance(proof, dict):
            raise KubernetesExecutionError("Kubernetes cancellation evidence is malformed.")
        proof["job_uid"] = row["job_uid"]
        proof["reconciliation_required"] = not bool(proof.get("termination_confirmed"))
        if row["job_uid"] is not None:
            proof["provider_id"] = KubernetesProviderIdentity(
                self.cluster.cluster_uid, self.cluster.namespace_uid, row["job_uid"]
            ).provider_id
        return proof

    def associate_opencode_session(
        self, package: KubernetesExecutionPackage, binding: TesseraBinding
    ) -> None:
        """Associate an already-created adjacent OpenCode session without starting it."""
        self._bind(package)
        if (
            binding.session_id is None
            or binding.organization_id != _string(package.package, "org_id")
            or binding.project_id != _string(package.package, "project_id")
            or binding.task_id != _string(package.package, "task_id")
            or binding.operation_id != package.operation_id
            or binding.epoch != package.binding.epoch
            or binding.attempt_id != package.binding.attempt_id
            or binding.operation_authority_ref != _string(package.package, "grant_id")
            or binding.attempt_authority_ref != package.binding.attempt_id
        ):
            raise KubernetesExecutionError(
                "OpenCode session association conflicts with the admitted package."
            )
        values = (binding.harness_instance_id, binding.session_id, package.operation_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT harness_instance_id, opencode_session_id "
                "FROM kubernetes_execution_bindings "
                "WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
            if row is None:
                raise KubernetesExecutionError("Kubernetes operation intent disappeared.")
            if (row["harness_instance_id"], row["opencode_session_id"]) not in {
                (None, None),
                values[:2],
            }:
                raise KubernetesExecutionError(
                    "OpenCode session identity cannot change for an operation."
                )
            connection.execute(
                "UPDATE kubernetes_execution_bindings SET harness_instance_id=?, "
                "opencode_session_id=? "
                "WHERE operation_id=?",
                values,
            )

    def artifact_collection_handoff(
        self, package: KubernetesExecutionPackage
    ) -> ArtifactCollectionHandoff:
        """Emit a handoff only after an observed successful Job; no bytes move here."""
        self._bind(package)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid, pod_uids_json, observed_state, cancellation_requested, "
                "namespace, namespace_uid, pvc_name, pvc_uid, pvc_spec_sha256 "
                "FROM kubernetes_execution_bindings "
                "WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
            if (
                row is None
                or row["observed_state"] != "succeeded"
                or row["cancellation_requested"]
                or row["job_uid"] is None
            ):
                raise KubernetesExecutionError(
                    "Artifact collection requires an observed successful Kubernetes Job."
                )
            pod_uids = tuple(json.loads(row["pod_uids_json"]))
            if not pod_uids:
                raise KubernetesExecutionError(
                    "Artifact collection requires explicit successful owned-Pod evidence."
                )
            handoff = ArtifactCollectionHandoff(
                operation_id=package.operation_id,
                admitted_attempt_id=package.binding.attempt_id,
                target_digest=cluster_identity_digest(self.cluster),
                namespace=row["namespace"],
                namespace_uid=row["namespace_uid"],
                pvc_name=row["pvc_name"],
                pvc_uid=row["pvc_uid"],
                pvc_spec_sha256=row["pvc_spec_sha256"],
                mount_path=package.output.mount_path,
                output_paths=tuple(package.package["output_paths"]),
                job_uid=row["job_uid"],
                pod_uids=pod_uids,
            )
            existing = connection.execute(
                "SELECT handoff_json FROM kubernetes_execution_bindings WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()["handoff_json"]
            encoded = canonical_json(handoff.__dict__).decode("utf-8")
            if existing is not None and existing != encoded:
                raise KubernetesExecutionError(
                    "Artifact collection handoff conflicts with durable intent."
                )
            connection.execute(
                "UPDATE kubernetes_execution_bindings SET handoff_json=? WHERE operation_id=?",
                (encoded, package.operation_id),
            )
        return handoff

    def _record_status(
        self,
        package: KubernetesExecutionPackage,
        job: KubernetesJobObservation,
        deadline: float,
    ) -> KubernetesExecutionStatus:
        # The Job UID is recovery ownership and must survive a later Pod read
        # failure.  Persist it before invoking the injected Pod observer.
        self._record_job_identity(package, job.job_uid)
        try:
            observed_pods = self._call_before(deadline, self._observe_pods, job.job_uid)
        except Exception:
            return self._record_unknown(package)
        pods = tuple(
            parse_pod(pod, package.binding, self.cluster, job.job_uid) for pod in observed_pods
        )
        states = {pod.state for pod in pods}
        if (
            job.state == "unknown"
            or "unknown" in states
            or (job.state == "succeeded" and (not pods or states != {"succeeded"}))
            or (job.state == "failed" and (not pods or not states <= {"succeeded", "failed"}))
            or ("failed" in states and job.state not in {"succeeded", "failed"})
        ):
            state: Literal["pending", "running", "succeeded", "failed", "unknown"] = "unknown"
        elif job.state == "failed" or "failed" in states:
            state = "failed"
        elif job.state == "succeeded":
            state = "succeeded"
        elif job.state == "running" or "running" in states:
            state = "running"
        else:
            state = "pending"
        unconfirmed = canonical_json(
            KubernetesCancellationObservation(True, False, False, False).__dict__
        ).decode("utf-8")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid FROM kubernetes_execution_bindings WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
            if row is None:
                raise KubernetesExecutionError("Kubernetes operation intent disappeared.")
            if row["job_uid"] not in {None, job.job_uid}:
                raise KubernetesExecutionError(
                    "Kubernetes Job UID conflicts with the durable operation."
                )
            connection.execute(
                "UPDATE kubernetes_execution_bindings SET job_uid=?, pod_uids_json=?, "
                "observed_state=?, cancellation_json=CASE WHEN cancellation_requested=1 THEN ? "
                "ELSE cancellation_json END "
                "WHERE operation_id=?",
                (
                    job.job_uid,
                    json.dumps([pod.pod_uid for pod in pods]),
                    state,
                    unconfirmed,
                    package.operation_id,
                ),
            )
        return KubernetesExecutionStatus(
            package.operation_id,
            job,
            pods,
            state,
            KubernetesProviderIdentity(
                self.cluster.cluster_uid, self.cluster.namespace_uid, job.job_uid
            ).provider_id,
        )

    def _record_job_identity(self, package: KubernetesExecutionPackage, job_uid: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid FROM kubernetes_execution_bindings WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
            if row is None:
                raise KubernetesExecutionError("Kubernetes operation intent disappeared.")
            if row["job_uid"] not in {None, job_uid}:
                raise KubernetesExecutionError(
                    "Kubernetes Job UID conflicts with the durable operation."
                )
            connection.execute(
                "UPDATE kubernetes_execution_bindings SET job_uid=? WHERE operation_id=?",
                (job_uid, package.operation_id),
            )

    def _record_unknown(self, package: KubernetesExecutionPackage) -> KubernetesExecutionStatus:
        """Make a newer failed read authoritative without dropping recovery identity."""
        unconfirmed = canonical_json(
            KubernetesCancellationObservation(True, False, False, False).__dict__
        ).decode("utf-8")
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE kubernetes_execution_bindings SET observed_state='unknown', "
                    "cancellation_json=CASE WHEN cancellation_requested=1 THEN ? "
                    "ELSE cancellation_json END "
                    "WHERE operation_id=?",
                    (unconfirmed, package.operation_id),
                ).rowcount
                != 1
            ):
                raise KubernetesExecutionError("Kubernetes operation intent disappeared.")
            row = connection.execute(
                "SELECT job_uid FROM kubernetes_execution_bindings WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
        job_uid = row["job_uid"] if row is not None else None
        provider_id = (
            KubernetesProviderIdentity(
                self.cluster.cluster_uid, self.cluster.namespace_uid, job_uid
            ).provider_id
            if job_uid is not None
            else None
        )
        return KubernetesExecutionStatus(package.operation_id, None, (), "unknown", provider_id)

    def _record_disappeared_job(
        self, package: KubernetesExecutionPackage, deadline: float
    ) -> KubernetesExecutionStatus:
        """Keep reconciling Pods against the first recorded Job UID after Job deletion.

        A deterministic Job name is reusable, while its UID is the operation's
        durable recovery ownership.  Therefore a missing Job is never a terminal
        observation and never permits a replacement create.  Fresh owned-Pod
        reads remain required on every recovery pass, including after restart.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid FROM kubernetes_execution_bindings WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
        if row is None:
            raise KubernetesExecutionError("Kubernetes operation intent disappeared.")
        job_uid = row["job_uid"]
        if job_uid is None:
            return self._record_unknown(package)
        try:
            # Validate durable recovery identity before sending it to the injected
            # Pod observer.  It is not a name lookup and cannot attach a replacement.
            KubernetesProviderIdentity(
                self.cluster.cluster_uid, self.cluster.namespace_uid, job_uid
            )
            observed_pods = self._call_before(deadline, self._observe_pods, job_uid)
        except Exception:
            return self._record_unknown(package)
        try:
            pods = tuple(
                parse_pod(pod, package.binding, self.cluster, job_uid) for pod in observed_pods
            )
        except Exception:
            self._record_unknown(package)
            raise
        cancellation = canonical_json(
            KubernetesCancellationObservation(True, True, True, not pods).__dict__
        ).decode("utf-8")
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE kubernetes_execution_bindings SET pod_uids_json=?, "
                    "observed_state='unknown', cancellation_json=CASE "
                    "WHEN cancellation_requested=1 THEN ? ELSE cancellation_json END "
                    "WHERE operation_id=? AND job_uid=?",
                    (
                        json.dumps([pod.pod_uid for pod in pods]),
                        cancellation,
                        package.operation_id,
                        job_uid,
                    ),
                ).rowcount
                != 1
            ):
                raise KubernetesExecutionError(
                    "Kubernetes Job UID conflicts with the durable operation."
                )
        return KubernetesExecutionStatus(
            package.operation_id,
            None,
            pods,
            "unknown",
            KubernetesProviderIdentity(
                self.cluster.cluster_uid, self.cluster.namespace_uid, job_uid
            ).provider_id,
        )

    def _operation_deadline(
        self, package: KubernetesExecutionPackage, timeout_seconds: float | None
    ) -> float:
        if timeout_seconds is None:
            now = datetime.now(UTC)
            wall_deadline = min(
                package.hard_expires_at,
                package.lease_expires_at,
                package.lease_issued_at + timedelta(seconds=package.approved_duration_seconds),
            )
            remaining = (wall_deadline - now).total_seconds()
        elif type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds):
            raise ValueError("Kubernetes provider timeout must be finite.")
        else:
            remaining = float(timeout_seconds)
        return time.monotonic() + max(0.0, min(MAX_PROVIDER_CALL_SECONDS, remaining))

    def _run_callback(
        self, future: Future[Any], callback: Callable[..., Any], arguments: tuple[Any, ...]
    ) -> None:
        try:
            future.set_result(callback(*arguments))
        except BaseException as error:
            future.set_exception(error)

    def _call_before(self, deadline: float, callback: Callable[..., Any], *arguments: Any) -> Any:
        """Bound one injected provider call and never overlap a timed-out effect."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise KubernetesExecutionError(
                "Kubernetes provider call deadline exceeded.", diagnostic_code="deadline_exceeded"
            )
        with self._inflight_lock:
            if self._inflight is not None and not self._inflight.done():
                raise KubernetesExecutionError(
                    "Kubernetes provider call is still in progress.",
                    diagnostic_code="callback_in_progress",
                )
            future: Future[Any] = Future()
            self._inflight = future
            thread = threading.Thread(
                target=self._run_callback,
                args=(future, callback, arguments),
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError:
                self._inflight = None
                raise KubernetesExecutionError(
                    "Kubernetes provider callback is unavailable."
                ) from None
        try:
            return future.result(timeout=remaining)
        except TimeoutError:
            raise KubernetesExecutionError(
                "Kubernetes provider call deadline exceeded.", diagnostic_code="callback_timeout"
            ) from None

    def _mark_cancellation_requested(self, operation_id: str) -> None:
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE kubernetes_execution_bindings SET cancellation_requested=1 "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).rowcount
                != 1
            ):
                raise KubernetesExecutionError("Kubernetes operation intent disappeared.")

    def _record_cancellation(
        self, package: KubernetesExecutionPackage, observation: KubernetesCancellationObservation
    ) -> None:
        proof = canonical_json(observation.__dict__).decode("utf-8")
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE kubernetes_execution_bindings SET cancellation_json=? "
                    "WHERE operation_id=?",
                    (proof, package.operation_id),
                ).rowcount
                != 1
            ):
                raise KubernetesExecutionError("Kubernetes operation intent disappeared.")

    def _bind(self, package: KubernetesExecutionPackage) -> None:
        target_digest = cluster_identity_digest(self.cluster)
        encoded = canonical_json(
            {
                "binding": package.binding.journal_fields,
                "package_digest": package.package_digest,
                "target_digest": target_digest,
            }
        ).decode("utf-8")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT intent_json FROM kubernetes_execution_bindings WHERE operation_id=?",
                (package.operation_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO kubernetes_execution_bindings
                    (operation_id, intent_json, target_digest, namespace, namespace_uid, pvc_name,
                     pvc_uid, pvc_spec_sha256, job_name, pod_uids_json, observed_state,
                     cancellation_requested, cancellation_json, handoff_json,
                     harness_instance_id, opencode_session_id)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 'unknown', 0,
                     NULL, NULL, NULL, NULL)""",
                    (
                        package.operation_id,
                        encoded,
                        target_digest,
                        self.cluster.namespace,
                        self.cluster.namespace_uid,
                        package.output.pvc_name,
                        package.output.pvc_uid,
                        package.output.pvc_spec_sha256,
                        package.binding.job_name,
                    ),
                )
            elif row["intent_json"] != encoded:
                raise KubernetesExecutionError(
                    "Kubernetes target or package conflicts with durable intent."
                )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS kubernetes_execution_bindings (
                operation_id TEXT PRIMARY KEY, intent_json TEXT NOT NULL,
                target_digest TEXT NOT NULL,
                namespace TEXT NOT NULL, namespace_uid TEXT NOT NULL, pvc_name TEXT NOT NULL,
                pvc_uid TEXT NOT NULL, pvc_spec_sha256 TEXT NOT NULL, job_name TEXT NOT NULL,
                job_uid TEXT, pod_uids_json TEXT NOT NULL, observed_state TEXT NOT NULL,
                cancellation_requested INTEGER NOT NULL, cancellation_json TEXT, handoff_json TEXT,
                harness_instance_id TEXT, opencode_session_id TEXT)"""
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(kubernetes_execution_bindings)")
            }
            if "cancellation_json" not in columns:
                connection.execute(
                    "ALTER TABLE kubernetes_execution_bindings ADD COLUMN cancellation_json TEXT"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database)
        connection.row_factory = sqlite3.Row
        return connection


class KubernetesExecutionProtocolAdapter:
    """Translate the Kubernetes conformance adapter to neutral protocols.

    This is intentionally a compatibility layer rather than a replacement for
    :class:`KubernetesExecutionAdapter`: its calls preserve the existing local
    intent journal, one-create fence, UID fencing, and collection handoff.
    """

    _capabilities = ExecutionCapabilities(
        preflight=True,
        submit=True,
        observe=True,
        cancel=True,
        reconcile=True,
        collect=True,
        account=True,
        limitations=(
            "collection is a target-bound handoff, not byte transfer or result acceptance",
            "accounting exposure is unknown in the provider-disabled Kubernetes fixture",
            "unknown observations require reconciliation and never authorize resubmission",
        ),
    )

    def __init__(self, adapter: KubernetesExecutionAdapter) -> None:
        self.adapter = adapter
        self.target = self.target_binding(adapter.cluster)

    @staticmethod
    def target_binding(cluster: KubernetesClusterConfig) -> TargetBinding:
        return TargetBinding(
            adapter_kind="kubernetes-job",
            profile_version=cluster.profile,
            identity_digest=cluster_identity_digest(cluster),
        )

    def capabilities(self, target: TargetBinding) -> ExecutionCapabilities:
        self._require_target(target)
        return self._capabilities

    def preflight(self, request: PreflightRequest) -> PreflightResult:
        try:
            package = self._package(request.envelope)
            self._require_target(request.target)
            if request.envelope.deadline <= datetime.now(UTC):
                return PreflightResult(
                    request.target,
                    self._capabilities,
                    False,
                    "execution deadline elapsed",
                )
            # This validates the adapter-owned target capability and policy
            # without writing an intent or invoking an injected provider call.
            from .kubernetes_contracts import job_manifest

            job_manifest(package.binding, self.adapter.cluster)
        except (KubernetesExecutionError, KubernetesContractError, ValueError):
            return PreflightResult(
                request.target,
                self._capabilities,
                False,
                "target cannot admit the frozen execution envelope",
            )
        return PreflightResult(request.target, self._capabilities, True)

    def submit(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> Observation:
        package = self._package(envelope)
        return self._observation(
            envelope, self.adapter.submit(package, timeout_seconds=timeout_seconds)
        )

    def observe(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> Observation:
        """Observe using the existing no-create reconciliation path."""
        return self.reconcile(envelope, timeout_seconds=timeout_seconds)

    def cancel(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> NeutralCancellationObservation:
        package = self._package(envelope)
        cancellation = self.adapter.cancel(package, timeout_seconds=timeout_seconds)
        proof = self.adapter.cancellation_proof(package)
        return NeutralCancellationObservation(
            self._handle(envelope, proof.get("provider_id")),
            cancellation.requested,
            cancellation.termination_confirmed,
            not cancellation.termination_confirmed,
        )

    def reconcile(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> Observation:
        package = self._package(envelope)
        return self._observation(
            envelope, self.adapter.reconcile(package, timeout_seconds=timeout_seconds)
        )

    def collect(self, envelope: ExecutionEnvelope) -> CollectionHandoff:
        package = self._package(envelope)
        handoff = self.adapter.artifact_collection_handoff(package)
        provider_id = KubernetesProviderIdentity(
            self.adapter.cluster.cluster_uid,
            self.adapter.cluster.namespace_uid,
            handoff.job_uid,
        ).provider_id
        return CollectionHandoff(self._handle(envelope, provider_id), handoff.output_paths)

    def account(self, envelope: ExecutionEnvelope) -> AccountingObservation:
        self._package(envelope)
        # The existing provider-disabled path must not manufacture billable usage.
        return AccountingObservation(self._handle(envelope, None), "unknown")

    def _package(self, envelope: ExecutionEnvelope) -> KubernetesExecutionPackage:
        self._require_target(envelope.target)
        package = envelope.payload
        if not isinstance(package, KubernetesExecutionPackage):
            raise KubernetesExecutionError(
                "Kubernetes adapter received a foreign execution envelope."
            )
        if package.execution_envelope(self.target) != envelope:
            raise KubernetesExecutionError(
                "Kubernetes execution envelope conflicts with frozen intent."
            )
        return package

    def _require_target(self, target: TargetBinding) -> None:
        if target != self.target:
            raise KubernetesExecutionError(
                "Kubernetes execution target conflicts with the adapter binding."
            )

    @staticmethod
    def _handle(envelope: ExecutionEnvelope, provider_id: object) -> OperationHandle:
        return OperationHandle(
            envelope.target,
            envelope.operation_id,
            provider_id if isinstance(provider_id, str) else None,
        )

    def _observation(
        self, envelope: ExecutionEnvelope, status: KubernetesExecutionStatus
    ) -> Observation:
        return Observation(
            self._handle(envelope, status.provider_id),
            status.observed_state,
            datetime.now(UTC),
        )


def _string(value: Mapping[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str):
        raise KubernetesExecutionError(f"Kubernetes package {field} is malformed.")
    return result


def _positive_int(value: Mapping[str, object], field: str) -> int:
    result = value.get(field)
    if type(result) is not int or result < 1:
        raise KubernetesExecutionError(f"Kubernetes package {field} is malformed.")
    return result


def _utc_datetime(value: Mapping[str, object], field: str) -> datetime:
    result = value.get(field)
    if not isinstance(result, str):
        raise ValueError
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _network_mode(workload: Mapping[str, object]) -> Literal["none", "cluster"]:
    mode = workload.get("network_mode")
    if mode != "none":
        raise KubernetesExecutionError("Kubernetes package must explicitly request network-none.")
    return mode


__all__ = [
    "ArtifactCollectionHandoff",
    "KubernetesExecutionAdapter",
    "KubernetesExecutionError",
    "KubernetesExecutionPackage",
    "KubernetesExecutionProtocolAdapter",
    "KubernetesExecutionStatus",
    "KubernetesOutputBinding",
]
