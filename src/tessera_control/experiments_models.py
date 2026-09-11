"""Strict request models for the bounded goal-to-decision ledger."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .research_models import _bounded_json


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


NonBlank200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NonBlank4000 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
OperationId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class DesignVariant(ExperimentRequest):
    kind: Literal["baseline", "intervention"]
    name: NonBlank200
    method_digest: Digest
    input_digest: Digest
    metadata: dict[str, Any]
    execution_task_id: Identifier | None = None
    execution_plan_digest: Digest | None = None

    _metadata_is_bounded = field_validator("metadata")(_bounded_json)

    @model_validator(mode="after")
    def execution_plan_is_complete(self) -> DesignVariant:
        if (self.execution_task_id is None) != (self.execution_plan_digest is None):
            raise ValueError("execution task and plan digest must be supplied together")
        return self


class CreateExperiment(ExperimentRequest):
    goal: NonBlank4000
    success_criterion: NonBlank4000
    protocol: NonBlank4000
    variants: Annotated[list[DesignVariant], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def requires_one_baseline_and_one_intervention(self) -> CreateExperiment:
        if {item.kind for item in self.variants} != {"baseline", "intervention"}:
            raise ValueError("a design requires one baseline and one intervention")
        return self


class ReviseExperimentDesign(CreateExperiment):
    expected_revision: Revision


class RecordAttempt(ExperimentRequest):
    expected_variant_revision: Revision
    provenance_kind: Literal["admitted_operation", "human_reported_observation"]
    operation_id: OperationId | None = None
    human_observation: NonBlank4000 | None = None
    outcome: Literal["succeeded", "failed", "cancelled", "inconclusive"]
    reason: NonBlank4000 | None = None

    @model_validator(mode="after")
    def non_success_outcomes_need_a_reason(self) -> RecordAttempt:
        if self.outcome != "succeeded" and self.reason is None:
            raise ValueError("failed, cancelled, and inconclusive outcomes require a reason")
        if self.provenance_kind == "admitted_operation":
            if self.operation_id is None or self.human_observation is not None:
                raise ValueError("an admitted operation requires only its operation id")
        elif self.operation_id is not None or self.human_observation is None:
            raise ValueError(
                "a human-reported observation requires its observation and no operation id"
            )
        return self


class LinkAttemptArtifacts(ExperimentRequest):
    expected_revision: Revision
    artifact_ids: Annotated[list[Identifier], Field(min_length=1, max_length=128)]

    @field_validator("artifact_ids")
    @classmethod
    def artifact_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("artifact ids must be unique")
        return value


class RecordMeasurement(ExperimentRequest):
    expected_revision: Revision
    name: NonBlank200
    value: dict[str, Any]
    artifact_id: Identifier | None = None

    _value_is_bounded = field_validator("value")(_bounded_json)


class EvidenceCitation(ExperimentRequest):
    attempt_id: Identifier
    artifact_ids: Annotated[list[Identifier], Field(max_length=128)] = []
    measurement_ids: Annotated[list[Identifier], Field(max_length=128)] = []

    @model_validator(mode="after")
    def needs_linked_evidence(self) -> EvidenceCitation:
        if not self.artifact_ids and not self.measurement_ids:
            raise ValueError("each citation requires an artifact or measurement")
        if len(set(self.artifact_ids)) != len(self.artifact_ids) or len(
            set(self.measurement_ids)
        ) != len(self.measurement_ids):
            raise ValueError("citation evidence ids must be unique")
        return self


class CreateConclusion(ExperimentRequest):
    statement: NonBlank4000
    evidence: Annotated[list[EvidenceCitation], Field(min_length=1, max_length=128)]
    # Older clients can omit this while the store still freezes the current design.
    # New clients should name the design context they reviewed explicitly.
    design_revision: Revision | None = None


class UpdateConclusion(CreateConclusion):
    expected_revision: Revision


class ReviewConclusion(ExperimentRequest):
    expected_revision: Revision
    design_revision: Revision | None = None


class AmendConclusion(ExperimentRequest):
    """Append one terminal human correction relationship to a reviewed conclusion."""

    expected_revision: Revision
    expected_design_revision: Revision
    relation: Literal["supersedes", "retracts"]
    reason: NonBlank4000


class ExportExperiment(ExperimentRequest):
    """The complete snapshot must fit the control/desktop response boundary."""

    max_bytes: Annotated[int, Field(strict=True, ge=256, le=4 * 1024 * 1024)] = 1_000_000
