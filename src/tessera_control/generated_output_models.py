"""Human-admitted public research output, never execution approval."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from .models import NonBlank200, Sha256Digest, StrictRequest, StrictRevision


class OutputPins(StrictRequest):
    model_id: NonBlank200
    run_id: NonBlank200
    session_id: NonBlank200
    harness_instance_id: NonBlank200
    context_id: NonBlank200
    context_digest: Sha256Digest
    skill_digest: Sha256Digest
    config_digest: Sha256Digest
    task_id: NonBlank200 | None = None
    operation_id: NonBlank200 | None = None


class OutputAdmission(StrictRequest):
    action: Literal["generated-output:register"]
    output_kind: Literal["planner_proposal", "critic_review"]
    credential_id: NonBlank200
    destination: NonBlank200
    pins: OutputPins
    public_output_admitted: Literal[True]
    max_outputs: int = Field(strict=True, ge=1, le=16)
    max_total_bytes: int = Field(strict=True, ge=1, le=1048576)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def offset(cls, value):
        if value.tzinfo is None:
            raise ValueError("expiry requires an offset")
        return value


class RegisterGeneratedOutput(StrictRequest):
    grant_id: NonBlank200
    expected_revision: StrictRevision
    pins: OutputPins
    source_event_id: NonBlank200
    source_sequence: int = Field(strict=True, ge=1, le=2**53 - 1)
    output_kind: Literal["planner_proposal", "critic_review"]
    visibility: Literal["public"]
    sha256: Sha256Digest
    content_base64: str = Field(max_length=87384)


class OutputDisclosure(StrictRequest):
    credential_id: NonBlank200
    destination: NonBlank200
    disclosure_approved: Literal[True]
    expires_at: datetime

    _offset = field_validator("expires_at")(OutputAdmission.offset.__func__)


class OutputFence(StrictRequest):
    expected_revision: StrictRevision
