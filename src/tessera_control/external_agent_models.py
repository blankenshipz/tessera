"""Closed schemas for non-authoritative external-agent collaboration."""

from typing import Literal

from pydantic import Field, field_validator

from .models import (
    ExecutionSpec,
    NonBlank200,
    NonBlank4000,
    Sha256Digest,
    StrictDuration,
    StrictNonNegativeInt,
    StrictRequest,
    StrictRevision,
    within_operator_limit,
)


class AgentPlan(StrictRequest):
    title: NonBlank200
    purpose: NonBlank4000
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration
    execution_spec: ExecutionSpec

    @field_validator("execution_spec")
    @classmethod
    def _within_operator_limit(cls, spec: ExecutionSpec) -> ExecutionSpec:
        return within_operator_limit(spec)


class AgentProvenance(StrictRequest):
    client: NonBlank200
    model: NonBlank200
    session_id: NonBlank200


class CreateAgentProposal(StrictRequest):
    proposal_type: Literal["task_plan"]
    plan: AgentPlan
    provenance: AgentProvenance


class PromoteAgentProposal(StrictRequest):
    expected_revision: StrictRevision
    expected_digest: Sha256Digest


class CreateAgentContext(StrictRequest):
    credential_id: NonBlank200
    destination: NonBlank200
    title: NonBlank200
    body: NonBlank4000
    classification: Literal["public", "project"]
    disclosure_approved: Literal[True] = Field(...)


class CreateAgentArtifactContext(StrictRequest):
    credential_id: NonBlank200
    destination: NonBlank200
    title: NonBlank200
    classification: Literal["public", "project"]
    disclosure_approved: Literal[True] = Field(...)
    manifest_id: NonBlank200
    artifact_id: NonBlank200
    expected_manifest_digest: Sha256Digest
    max_bytes: int = Field(strict=True, ge=1, le=65536)
