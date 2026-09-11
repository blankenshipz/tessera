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
