"""Content-free, strict agent provenance envelopes, never reasoning transcripts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from .agent_provenance_guard_models import GuardPhaseObservation
from .agent_provenance_harness_models import HarnessDigest, HarnessID, PublicHarnessMetadata


class RegisteredResearchPins(BaseModel):
    """Closed content-free registry pin projection, also for uninstrumented events."""

    model_config = ConfigDict(extra="forbid", strict=True)
    model_id: HarnessID
    run_id: HarnessID
    session_id: HarnessID
    harness_instance_id: HarnessID
    context_id: HarnessID
    context_digest: HarnessDigest
    skill_digest: HarnessDigest
    config_digest: HarnessDigest
    task_id: HarnessID | None
    operation_id: HarnessID | None


class GeneratedResearchOutputReference(BaseModel):
    """Distinct registered research family; never a worker manifest/artifact proof."""

    model_config = ConfigDict(extra="forbid", strict=True)
    family: Literal["generated_public_research_output_v1"]
    output_id: HarnessID
    disclosure_id: HarnessID
    sha256: HarnessDigest
    output_kind: Literal["planner_proposal", "critic_review"]
    writer_actor_id: HarnessID
    writer_credential_id: HarnessID
    writer_org_id: HarnessID


OpaqueID = str


class ProvenanceReferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    study_id: OpaqueID | None = None
    experiment_id: OpaqueID | None = None
    variant_id: OpaqueID | None = None
    task_id: OpaqueID | None = None
    attempt_id: OpaqueID | None = None
    operation_id: OpaqueID | None = None
    grant_id: OpaqueID | None = None
    consumption_id: OpaqueID | None = None
    permission_id: OpaqueID | None = None
    message_id: OpaqueID | None = None
    delegation_id: OpaqueID | None = None
    peer_id: OpaqueID | None = None
    artifact_id: OpaqueID | None = None
    control_event_id: OpaqueID | None = None
    context_id: OpaqueID | None = None
    provenance_record_id: OpaqueID | None = None
    output_manifest_id: OpaqueID | None = None
    provider_job_uid: OpaqueID | None = None

    @model_serializer(mode="wrap")
    def omit_absent_additive_output_links(self, handler):
        # Preserve pre-output outbox wire digests and exact acknowledgement matching.
        value = handler(self)
        for field in ("output_manifest_id", "provider_job_uid"):
            if value.get(field) is None:
                value.pop(field, None)
        return value


class RemoteProvenanceIdentities(BaseModel):
    """Remote opaque identifiers are assertions, not central resource references."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str | None = None
    message_id: str | None = None
    peer_id: str | None = None
    destination_digest: str | None = None


class AgentProvenanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    record_id: str
    kind: Literal["agent", "model", "tool", "permission", "message", "delegation", "attempt"]
    outcome: Literal[
        "requested",
        "started",
        "succeeded",
        "failed",
        "denied",
        "unknown",
        "canceled",
        "cancelled",
        "late",
        "quarantined",
    ]
    correlation_id: str
    causation_id: str | None = None
    model_id: str | None = None
    tool_id: str | None = None
    references: ProvenanceReferences = Field(default_factory=ProvenanceReferences)
    remote: RemoteProvenanceIdentities = Field(default_factory=RemoteProvenanceIdentities)
    grant_revision: int | None = Field(default=None, ge=1)
    grant_epoch: int | None = Field(default=None, ge=1)
    content_digest: str | None = None
    harness: PublicHarnessMetadata | None = None
    generated_output: GeneratedResearchOutputReference | None = None
    guard_phase: GuardPhaseObservation | None = None
    error_code: (
        Literal[
            "permission_denied",
            "expired",
            "revoked",
            "budget_exceeded",
            "transport_failure",
            "provider_failure",
            "canceled",
            "outcome_unknown",
            "invalid_request",
        ]
        | None
    ) = None

    @model_serializer(mode="wrap")
    def omit_absent_harness_metadata(self, handler):
        value = handler(self)
        if value.get("harness") is None:
            value.pop("harness", None)
        if value.get("generated_output") is None:
            value.pop("generated_output", None)
        if value.get("guard_phase") is None:
            value.pop("guard_phase", None)
        return value


class ProvenanceGrantProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    grant_id: str
    expected_revision: int = Field(ge=1)
    expected_epoch: int = Field(ge=1)


class AppendAgentProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    record: AgentProvenanceRecord
    grant_proof: ProvenanceGrantProof | None = None
    output_proof: "OutputProvenanceProof | None" = None


class ProviderJobIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cluster_uid: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    namespace_uid: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    job_uid: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class OutputProvenanceProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    output_manifest_id: str
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_job: ProviderJobIdentity | None = None


AppendAgentProvenance.model_rebuild()
