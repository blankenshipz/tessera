"""Explicit control-plane Docker enrollment and native worker reports."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from tessera_worker.ordinary_docker_contract import DockerLease, DockerTarget

from .models import DockerResources, NonBlank200, Sha256Digest, StrictRequest, StrictRevision


class EnrollDockerTarget(StrictRequest):
    worker_principal_id: NonBlank200
    target: DockerTarget
    resources: DockerResources
    max_output_bytes: int = Field(strict=True, ge=1, le=16777216)
    network: Literal["none"]
    placement: Literal["independent_execution_host"]
    isolation_approved: Literal[True]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def offset(cls, value):
        if value.tzinfo is None:
            raise ValueError("expiry requires an offset")
        return value


class DockerPackageRequest(StrictRequest):
    expected_epoch: StrictRevision
    admitted_attempt_id: NonBlank200


class DockerDispatchRequest(StrictRequest):
    expected_revision: StrictRevision
    target_digest: Sha256Digest
    profile_version: Literal["ordinary-docker-v1"]


class DockerOperationFence(StrictRequest):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    admitted_attempt_id: NonBlank200
    lease_id: NonBlank200
    expected_epoch: StrictRevision
    package_digest: Sha256Digest
    target_digest: Sha256Digest


class DockerReport(DockerOperationFence):
    container_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    state: Literal["running", "succeeded", "failed", "cancelled", "unknown"]
    termination_confirmed: bool
    stop_requested: bool
    exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)


class DockerReadback(DockerOperationFence):
    expected_container_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    delivery_key: NonBlank200 | None = None


class DockerReportAck(DockerReport):
    scientific_validity: Literal["not_assessed"]
    human_resolution_required: Literal[True]


class DockerDeliveryReceipt(StrictRequest):
    idempotency_key: NonBlank200
    payload_digest: Sha256Digest
    response_status: Literal[200]
    response: DockerReportAck


class DockerReadbackReply(StrictRequest):
    fence: DockerOperationFence
    task_id: NonBlank200
    grant_id: NonBlank200
    worker_principal_id: NonBlank200
    claimed_by_principal_id: NonBlank200 | None
    container_id: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    desired_state: Literal["admitted", "cancel_requested"]
    dispatch_state: Literal[
        "queued", "claimed", "running", "succeeded", "failed", "cancelled", "unknown"
    ]
    dispatch_revision: StrictRevision
    can_execute: bool
    authorization: Literal["execute", "stop_only"]
    effective_action: Literal["run", "stop"]
    lease: DockerLease
    latest_report: DockerReport | None
    latest_delivery: DockerDeliveryReceipt | None
    delivery_key: NonBlank200 | None
    delivery: DockerDeliveryReceipt | None


class DockerRevoke(StrictRequest):
    expected_revision: StrictRevision
