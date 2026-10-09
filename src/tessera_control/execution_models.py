"""Strict command models for the authority-only local CPU execution pilot."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
PinnedImage = Annotated[str, StringConstraints(min_length=72, max_length=255)]
Revision = Annotated[int, Field(strict=True, ge=1)]
CreationRevision = Annotated[int, Field(strict=True, ge=0)]
Cents = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class ConfigureProjectExecution(ExecutionRequest):
    expected_revision: CreationRevision
    currency: Currency
    monetary_limit_cents: Cents
    max_concurrency: Annotated[int, Field(strict=True, ge=1, le=8)]


class IssueExecutionGrant(ExecutionRequest):
    task_id: Identifier
    expected_task_revision: Revision
    expected_plan_digest: Digest
    worker_principal_id: Identifier
    snapshot_id: Identifier
    pinned_image: PinnedImage
    expires_at: datetime
    max_exposure_cents: Cents
    currency: Currency
    acknowledges_unmeasured_infrastructure: Literal[True]

    @field_validator("acknowledges_unmeasured_infrastructure", mode="before")
    @classmethod
    def infrastructure_acknowledgement_is_literal_true(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("unmeasured infrastructure exposure must be explicitly acknowledged")
        return True

    @field_validator("pinned_image")
    @classmethod
    def image_is_pinned_docker_reference(cls, value: str) -> str:
        repository, marker, digest = value.partition("@sha256:")
        if (
            not marker
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not repository
            or any(character.isspace() or character == "\x00" for character in repository)
        ):
            raise ValueError("pinned_image must be a digest-pinned Docker repository reference")
        segments = repository.split("/")
        if any(
            segment in {"", ".", ".."}
            or segment.startswith("-")
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:"
                for character in segment
            )
            for segment in segments
        ):
            raise ValueError("pinned_image must be a safe Docker repository reference")
        return value

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_has_offset(cls, value: object) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("expires_at requires ISO-8601 time") from error
        if not isinstance(value, datetime):
            raise ValueError("expires_at requires ISO-8601 time")
        if value.tzinfo is None:
            raise ValueError("expires_at requires an offset")
        return value


class RevokeExecutionGrant(ExecutionRequest):
    expected_revision: Revision


class AcquireExecutionLease(ExecutionRequest):
    grant_id: Identifier


class RenewExecutionLease(ExecutionRequest):
    lease_id: Identifier
    expected_epoch: Revision


class PrepareExecutionPackage(ExecutionRequest):
    expected_epoch: Revision
    execution_adapter: Literal["local-cpu", "kubernetes-job"] | None = None
    admitted_attempt_id: Identifier | None = None
    # A fresh authority read is not evidence that a worker or operation restarted.
    restart_intent: Literal[True] | None = None
    persistent_output_collection: Literal[True] | None = None
    persistent_output_storage: dict[str, Any] | None = None


class ReportExecutionObservation(ExecutionRequest):
    lease_id: Identifier
    expected_epoch: Revision
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")] | None = None
    observed_state: Literal["running", "exited", "unknown"]
    observed_at: datetime
    provider_id: Identifier | None = None
    termination_confirmed: bool = False

    @field_validator("observed_at", mode="before")
    @classmethod
    def observed_at_has_offset(cls, value: object) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("observed_at requires ISO-8601 time") from error
        if not isinstance(value, datetime):
            raise ValueError("observed_at requires ISO-8601 time")
        if value.tzinfo is None:
            raise ValueError("observed_at requires an offset")
        return value

    @field_validator("termination_confirmed")
    @classmethod
    def confirmation_requires_exit(cls, value: bool, info) -> bool:
        if value and info.data.get("observed_state") != "exited":
            raise ValueError("termination confirmation requires an exited observation")
        return value


class ReportExternalOperationReceipt(ExecutionRequest):
    """Bounded evidence returned by the injected fake provider only."""

    lease_id: Identifier
    expected_epoch: Revision
    provider_kind: Literal["fake"]
    provider_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    provider_id: Identifier
    receipt_payload: dict[str, Any]
    receipt_digest: Digest
    observed_state: Literal["unknown", "running", "exited"]
    observed_at: datetime
    termination_confirmed: bool = False

    @field_validator("observed_at", mode="before")
    @classmethod
    def receipt_observed_at_has_offset(cls, value: object) -> datetime:
        return ReportExecutionObservation.observed_at_has_offset(value)

    @field_validator("termination_confirmed")
    @classmethod
    def receipt_confirmation_requires_exit(cls, value: bool, info) -> bool:
        if value and info.data.get("observed_state") != "exited":
            raise ValueError("termination confirmation requires an exited observation")
        return value


class ReconcileExecutionGrant(ExecutionRequest):
    expected_revision: Revision
    settlement_cents: Cents
    provider_terminated: Literal[True]


class ReconcileExpiredUnattemptedGrant(ExecutionRequest):
    """Release an expired reservation only when no attempt was admitted."""

    expected_revision: Revision


class AttestKubernetesNeverSubmitted(ExecutionRequest):
    """Original worker's fenced-journal and fresh provider absence attestation."""

    expected_revision: Revision
    proof_kind: Literal["bound_job_intent", "pre_manifest"]
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    lease_id: Identifier
    epoch: Revision
    package_digest: Digest
    target_digest: Digest
    job_name: Identifier
    namespace_uid: Identifier
    journal_binding_digest: Digest
    journal_manifest_digest: Digest | None
    manifest_not_generated: bool
    observed_at: datetime
    create_intent: Literal[False]
    cancellation_requested: Literal[True]
    job_uid: None
    namespace_uid_checked: Literal[True]
    deterministic_job_absent: Literal[True]
    operation_jobs_absent: Literal[True]
    operation_pods_absent: Literal[True]

    @field_validator("manifest_not_generated")
    @classmethod
    def manifest_kind_matches(cls, value: bool, info) -> bool:
        kind = info.data.get("proof_kind")
        digest = info.data.get("journal_manifest_digest")
        if (kind == "pre_manifest" and (not value or digest is not None)) or (
            kind == "bound_job_intent" and (value or digest is None)
        ):
            raise ValueError("Manifest evidence must match the distinct no-start proof kind.")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def observed_at_has_offset(cls, value: object) -> datetime:
        return ReportExecutionObservation.observed_at_has_offset(value)


class OwnerPreManifestNoStart(AttestKubernetesNeverSubmitted):
    """A human grant owner explicitly assumes the exclusive-journal evidence boundary.

    The control service cannot independently inspect that retained worker file or
    Kubernetes. This is neither the expired worker's voice nor provider termination.
    """

    proof_kind: Literal["pre_manifest"]
    journal_manifest_digest: None
    manifest_not_generated: Literal[True]
    owner_attests_original_exclusive_journal: Literal[True]


class ReconcileAttestedNeverSubmitted(ExecutionRequest):
    expected_revision: Revision
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    attestation_digest: Digest


class KubernetesNeverClaimedIdentity(ExecutionRequest):
    """Non-executable immutable recovery identity."""

    expected_revision: Revision
    grant_epoch: Revision
    dispatch_id: Identifier
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    lease_id: Identifier
    epoch: Revision
    package_digest: Digest
    target_digest: Digest
    actual_target_digest: Digest
    target_relation: Literal["exact", "declared_without_network_witness"]
    namespace_uid: Identifier
    job_name: Identifier


class AttestKubernetesNeverClaimed(KubernetesNeverClaimedIdentity):
    """Distinct original-worker evidence; never an executable package or termination."""

    journal_fence_digest: Digest
    observed_at: datetime
    proof_kind: Literal["never_claimed"]
    original_exclusive_journals: Literal[True]
    descriptor_absent: Literal[True]
    adapter_intent_absent: Literal[True]
    job_intent_absent: Literal[True]
    helper_intent_absent: Literal[True]
    namespace_uid_checked: Literal[True]
    deterministic_job_absent: Literal[True]
    operation_jobs_absent: Literal[True]
    operation_pods_absent: Literal[True]
    helper_pods_absent: Literal[True]

    @field_validator("observed_at", mode="before")
    @classmethod
    def observed_at_has_offset(cls, value: object) -> datetime:
        return ReportExecutionObservation.observed_at_has_offset(value)


class IssueNeverClaimedMaintenance(KubernetesNeverClaimedIdentity):
    original_worker_principal_id: Identifier
    original_worker_credential_id: Identifier
    namespace: Identifier
    cluster_uid: Identifier
    custody_reference: Identifier
    custody_digest: Digest
    provider_read_authorization_reference: Identifier
    human_attests_original_worker_stopped_exclusive_custody: Literal[True]
    human_attests_existing_provider_read_authorization: Literal[True]
    purpose: Literal["stop-only-never-claimed"]
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_has_offset(cls, value: object) -> datetime:
        return IssueExecutionGrant.expiry_has_offset(value)


class HumanMaintenanceNeverClaimedProof(AttestKubernetesNeverClaimed):
    maintenance_authorization_id: Identifier
    maintenance_authorization_digest: Digest
    maintenance_actor_principal_id: Identifier
    maintenance_actor_credential_id: Identifier
    human_issuer_principal_id: Identifier
    original_worker_principal_id: Identifier
    original_worker_credential_id: Identifier
    custody_digest: Digest
    provider_read_authorization_reference: Identifier
    human_attests_original_worker_stopped_exclusive_custody: Literal[True]


class ReconcileMaintenanceNeverClaimed(ExecutionRequest):
    proof: HumanMaintenanceNeverClaimedProof
    proof_digest: Digest


class AttestKubernetesPreparationNotSubmitted(ExecutionRequest):
    """Distinct historical preparation failure; never rewrites create_intent to false."""

    expected_revision: Revision
    proof_kind: Literal["pre_job_preparation"]
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    lease_id: Identifier
    epoch: Revision
    package_digest: Digest
    target_digest: Digest
    job_name: Identifier
    namespace_uid: Identifier
    journal_binding_digest: Digest
    journal_manifest_digest: Digest
    manifest_not_generated: Literal[False]
    observed_at: datetime
    create_intent: Literal[True]
    cancellation_requested: Literal[True]
    job_uid: None
    namespace_uid_checked: Literal[True]
    deterministic_job_absent: Literal[True]
    operation_jobs_absent: Literal[True]
    # This is the execution label selector, not all Pods associated with output.
    operation_pods_absent: Literal[True]
    preparation_manifest_digest: Digest
    preparation_pod_uid: Identifier
    preparation_journal_pod_uid: Identifier
    preparation_receipt_absent: Literal[True]
    preparation_gate: Literal["ordinary-output-prepare-before-job-v1"]
    job_request_not_reached: Literal[True]
    worker_attests_original_exclusive_stopped: Literal[True]

    @field_validator("preparation_journal_pod_uid")
    @classmethod
    def preparation_uid_is_original(cls, value: str, info) -> str:
        if value != info.data.get("preparation_pod_uid"):
            raise ValueError("Preparation readback UID must match its original journal UID.")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def preparation_observed_at_has_offset(cls, value: object) -> datetime:
        return ReportExecutionObservation.observed_at_has_offset(value)


class ReconcileAttestedPreparationNotSubmitted(ExecutionRequest):
    expected_revision: Revision
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    attestation_digest: Digest


class RecoveryReconciliation(ExecutionRequest):
    """An independently authenticated observation of one lost-worker operation."""

    expected_grant_revision: Revision
    expected_recovery_revision: CreationRevision
    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    request_cancellation: bool = False
    observed_state: Literal["unknown", "running", "exited"] | None = None
    observed_at: datetime | None = None
    provider_id: Identifier | None = None
    evidence_reference: Identifier | None = None
    evidence_digest: Digest | None = None
    termination_confirmed: bool = False

    @field_validator("observed_at", mode="before")
    @classmethod
    def recovery_observed_at_has_offset(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return ReportExecutionObservation.observed_at_has_offset(value)

    @field_validator("termination_confirmed")
    @classmethod
    def recovery_confirmation_requires_exit(cls, value: bool, info) -> bool:
        if value and info.data.get("observed_state") != "exited":
            raise ValueError("termination confirmation requires an exited observation")
        return value

    @field_validator("evidence_digest")
    @classmethod
    def recovery_evidence_is_complete(cls, value: str | None, info) -> str | None:
        fields = ("observed_state", "observed_at", "provider_id", "evidence_reference")
        supplied = [info.data.get(field) is not None for field in fields]
        if value is None:
            if any(supplied) or info.data.get("termination_confirmed"):
                raise ValueError("recovery evidence requires its digest")
            return value
        if not all(supplied):
            raise ValueError("recovery evidence must include state, time, provider, and reference")
        return value
