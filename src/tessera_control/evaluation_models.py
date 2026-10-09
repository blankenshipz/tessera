"""Caller-reported measurements and deterministic gates, never computed science."""

import operator
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .research_models import Digest, NonBlank200, NonBlank4000, StrictResearchRequest
from .store import canonical_digest

MetricCategory = Literal[
    "teacher_imitation",
    "experimental_ground_truth",
    "scaffold_ood",
    "counterfactual_direction",
    "calibration",
    "capability_retention",
    "few_shot_curve",
    "ingredient_holdout",
    "family_holdout",
    "domain_holdout",
    "leakage_check",
]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class EvaluationIdentity(StrictResearchRequest):
    """Hashes of caller-declared manifests; not a claim of byte verification."""

    data_digest: Digest
    code_digest: Digest
    environment_digest: Digest


class MetricGate(StrictResearchRequest):
    id: NonBlank200
    category: MetricCategory
    variant_id: NonBlank200
    metric: NonBlank200
    unit: NonBlank200
    split_digest: Digest
    training_examples: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None
    procedure: NonBlank4000
    comparison: Literal["ge", "gt", "le", "lt", "eq"]
    threshold: Finite
    minimum_samples: Annotated[int, Field(ge=1, le=2**63 - 1)]

    @model_validator(mode="after")
    def curve_coordinate(self):
        if self.category == "few_shot_curve" and self.training_examples is None:
            raise ValueError("few-shot gates require a declared training_examples coordinate")
        return self


class EvaluationProtocol(StrictResearchRequest):
    version: Literal[1]
    identity: EvaluationIdentity
    gates: Annotated[list[MetricGate], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_gates(self):
        if len({g.id for g in self.gates}) != len(self.gates):
            raise ValueError("gate IDs must be unique")
        return self


class MetricObservation(StrictResearchRequest):
    gate_id: NonBlank200
    value: Finite | None = None
    sample_count: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None
    evidence_digest: Digest | None = None
    provenance: NonBlank4000 | None = None
    missing_reason: NonBlank4000 | None = None

    @model_validator(mode="after")
    def measured_or_missing(self):
        if self.value is None:
            if self.missing_reason is None or any(
                v is not None for v in (self.sample_count, self.evidence_digest, self.provenance)
            ):
                raise ValueError("missing metrics require a reason and no measurement evidence")
        elif self.missing_reason is not None or any(
            v is None for v in (self.sample_count, self.evidence_digest, self.provenance)
        ):
            raise ValueError("measured metrics require samples, evidence and explicit provenance")
        return self


class EvaluationRecord(StrictResearchRequest):
    version: Literal[1]
    plan_decision_id: NonBlank200
    plan_scope_digest: Digest
    protocol_digest: Digest
    identity: EvaluationIdentity
    evidence_kind: Literal["caller_reported"]
    observations: Annotated[list[MetricObservation], Field(max_length=100)]

    @model_validator(mode="after")
    def unique_observations(self):
        if len({o.gate_id for o in self.observations}) != len(self.observations):
            raise ValueError("only one observation per declared gate is allowed")
        return self


def evaluate_record(protocol: EvaluationProtocol, record: EvaluationRecord) -> dict:
    """Version-1 exact comparisons; no interpolation, averaging or inferred values."""
    if record.protocol_digest != canonical_digest(protocol.model_dump(mode="json")):
        raise ValueError("evaluation protocol digest does not match the declared protocol")
    if record.identity != protocol.identity:
        raise ValueError("evaluation data/code/environment identity does not match the plan")
    observations = {o.gate_id: o for o in record.observations}
    if observations.keys() - {g.id for g in protocol.gates}:
        raise ValueError("evaluation contains undeclared metric gate IDs")
    results = []
    for gate in protocol.gates:
        observation = observations.get(gate.id)
        reason = None
        if observation is None:
            reason = "metric_not_recorded"
        elif observation.value is None:
            reason = f"metric_missing: {observation.missing_reason}"
        elif observation.sample_count < gate.minimum_samples:
            reason = "insufficient_samples"
        status = (
            "inconclusive"
            if reason
            else (
                "pass"
                if getattr(operator, gate.comparison)(observation.value, gate.threshold)
                else "fail"
            )
        )
        results.append(
            {
                "gate": gate.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json") if observation else None,
                "status": status,
                "reason": reason or ("threshold_not_met" if status == "fail" else None),
            }
        )
    statuses = {r["status"] for r in results}
    report = {
        "version": 1,
        "protocol_digest": record.protocol_digest,
        "record_digest": canonical_digest(record.model_dump(mode="json")),
        "plan_decision_id": record.plan_decision_id,
        "plan_scope_digest": record.plan_scope_digest,
        "identity": record.identity.model_dump(mode="json"),
        "evidence_kind": record.evidence_kind,
        "status": "fail"
        if "fail" in statuses
        else ("inconclusive" if "inconclusive" in statuses else "pass"),
        "gates": results,
        "execution_granted": False,
        "scientific_validity": "not_assessed",
    }
    return {**report, "report_digest": canonical_digest(report)}
