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


class BoundMetricGate(StrictResearchRequest):
    """Version-2 gate bound to a platform split lock and replicate aggregate.

    ``statistic`` selects which platform-computed statistic is compared with the
    threshold. A conservative "at least threshold" gate should use ``ci_low`` so
    the whole confidence interval must clear the bar; ``mean`` ignores spread.
    """

    id: NonBlank200
    category: MetricCategory
    variant_id: NonBlank200  # study variant, as in version 1
    experiment_variant_id: NonBlank200
    metric: NonBlank200
    unit: NonBlank200
    split_lock_id: NonBlank200
    split_partition: NonBlank200
    split_digest: Digest  # must equal the lock's membership digest
    training_examples: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None
    procedure: NonBlank4000
    comparison: Literal["ge", "gt", "le", "lt", "eq"]
    threshold: Finite
    statistic: Literal["mean", "ci_low", "ci_high"]
    minimum_replicates: Annotated[int, Field(ge=2, le=2**63 - 1)]
    minimum_samples: Annotated[int, Field(ge=1, le=2**63 - 1)]
    # Pre-registered so a narrower interval method cannot be chosen after the fact.
    ci_method: Literal["t_interval_95", "percentile_bootstrap_95"] = "t_interval_95"
    allowed_sources: Annotated[
        list[Literal["derived", "human_entered"]], Field(min_length=1, max_length=2)
    ] = ["derived"]  # noqa: RUF012 - pydantic copies defaults
    required_reference_lock_ids: Annotated[list[NonBlank200], Field(max_length=50)] = []  # noqa: RUF012

    @model_validator(mode="after")
    def curve_coordinate(self):
        if self.category == "few_shot_curve" and self.training_examples is None:
            raise ValueError("few-shot gates require a declared training_examples coordinate")
        if len(set(self.allowed_sources)) != len(self.allowed_sources):
            raise ValueError("allowed_sources must be unique")
        if len(set(self.required_reference_lock_ids)) != len(self.required_reference_lock_ids):
            raise ValueError("required_reference_lock_ids must be unique")
        return self


class EvaluationProtocol(StrictResearchRequest):
    version: Literal[1, 2]
    identity: EvaluationIdentity
    gates: Annotated[list[MetricGate | BoundMetricGate], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_gates(self):
        if len({g.id for g in self.gates}) != len(self.gates):
            raise ValueError("gate IDs must be unique")
        expected = MetricGate if self.version == 1 else BoundMetricGate
        if any(not isinstance(g, expected) for g in self.gates):
            raise ValueError(f"version {self.version} protocols require {expected.__name__} gates")
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


class BoundObservation(StrictResearchRequest):
    """Reference to a platform replicate group; carries no caller-supplied value."""

    gate_id: NonBlank200
    replicate_group_id: NonBlank200 | None = None
    missing_reason: NonBlank4000 | None = None

    @model_validator(mode="after")
    def group_or_missing(self):
        if (self.replicate_group_id is None) == (self.missing_reason is None):
            raise ValueError("provide exactly one of replicate_group_id or missing_reason")
        return self


class EvaluationRecord(StrictResearchRequest):
    version: Literal[1, 2]
    plan_decision_id: NonBlank200
    plan_scope_digest: Digest
    protocol_digest: Digest
    identity: EvaluationIdentity
    evidence_kind: Literal["caller_reported", "platform_replicate_aggregate"]
    observations: Annotated[list[MetricObservation | BoundObservation], Field(max_length=100)]

    @model_validator(mode="before")
    @classmethod
    def version_observations(cls, data):
        # A bare {gate_id, missing_reason} is valid in both shapes, so dispatch on
        # the declared version rather than letting the union guess.
        if isinstance(data, dict) and data.get("version") == 2:
            items = data.get("observations")
            if isinstance(items, list):
                data = {
                    **data,
                    "observations": [
                        BoundObservation.model_validate(o) if isinstance(o, dict) else o
                        for o in items
                    ],
                }
        return data

    @model_validator(mode="after")
    def unique_observations(self):
        if len({o.gate_id for o in self.observations}) != len(self.observations):
            raise ValueError("only one observation per declared gate is allowed")
        if self.version == 1:
            kind, observation = "caller_reported", MetricObservation
        else:
            kind, observation = "platform_replicate_aggregate", BoundObservation
        if self.evidence_kind != kind or any(
            not isinstance(o, observation) for o in self.observations
        ):
            raise ValueError(f"version {self.version} records require {kind} evidence")
        return self


def evaluate_record(protocol: EvaluationProtocol, record: EvaluationRecord) -> dict:
    """Version-1 exact comparisons; no interpolation, averaging or inferred values."""
    if protocol.version != 1 or record.version != 1:
        raise ValueError("version-1 evaluation requires a version-1 protocol and record")
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
        "evidence_binding": "unbound_caller_reported_legacy",
        "status": "fail"
        if "fail" in statuses
        else ("inconclusive" if "inconclusive" in statuses else "pass"),
        "gates": results,
        "execution_granted": False,
        "scientific_validity": "not_assessed",
    }
    return {**report, "report_digest": canonical_digest(report)}


_GROUP_FIELDS = (
    "id",
    "n",
    "mean",
    "std",
    "ci_low",
    "ci_high",
    "ci_method",
    "result_digest",
    "sources",
    "excluded_attempts",
    "audit",
)


def _bound_reason(gate: BoundMetricGate, facts: dict) -> str | None:
    if facts.get("variant_linked") is not True:
        return "variant_not_linked"
    if facts["variant_id"] != gate.experiment_variant_id:
        return "variant_mismatch"
    if facts["metric"] != gate.metric or facts["unit"] != gate.unit:
        return "metric_unit_mismatch"
    if (
        facts["lock_id"] != gate.split_lock_id
        or facts["membership_digest"] != gate.split_digest
        or facts["partition"] != gate.split_partition
    ):
        return "split_mismatch"
    if facts["ci_method"] != gate.ci_method:
        return "ci_method_mismatch"
    if facts.get("current") is not True:
        # A group is an immutable snapshot; once its series gains, loses or is
        # still running an attempt, an older subset cannot be cited.
        return "replicate_group_stale"
    audit = facts.get("audit") or {}
    if audit.get("within_lock") != "clean":
        return "split_leakage_not_clean"
    cross = audit.get("cross_set") or {}
    if any(cross.get(ref) != "clean" for ref in gate.required_reference_lock_ids):
        return "cross_set_leakage_not_clean"
    if any(source not in gate.allowed_sources for source in facts["sources"]):
        return "disallowed_measurement_source"
    if facts["n"] < gate.minimum_replicates:
        return "insufficient_replicates"
    if facts["min_sample_count"] < gate.minimum_samples:
        return "insufficient_samples"
    return None


def evaluate_bound_record(
    protocol: EvaluationProtocol, record: EvaluationRecord, groups: dict[str, dict]
) -> dict:
    """Version-2 evaluation of platform replicate aggregates; pure and deterministic.

    ``groups`` maps replicate_group_id to resolved facts: the replicate group row
    (JSON fields parsed), ``"audit"`` ({"within_lock": status, "cross_set":
    {reference_lock_id: status}}) and ``"variant_linked"`` (True only when the
    gate's experiment variant belongs to an experiment linked from the study
    variant; anything else makes the gate inconclusive) and ``"current"`` (True only
    when the group still equals the full current series). No value is accepted from
    the caller, averaged, imputed or interpolated.
    """
    if protocol.version != 2 or record.version != 2:
        raise ValueError("bound evaluation requires a version-2 protocol and record")
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
        facts, reason = None, None
        if observation is None:
            reason = "metric_not_recorded"
        elif observation.replicate_group_id is None:
            reason = f"metric_missing: {observation.missing_reason}"
        else:
            facts = groups.get(observation.replicate_group_id)
            reason = "replicate_group_not_found" if facts is None else _bound_reason(gate, facts)
        if reason:
            status = "inconclusive"
        else:
            met = getattr(operator, gate.comparison)(facts[gate.statistic], gate.threshold)
            status, reason = ("pass", None) if met else ("fail", "threshold_not_met")
        results.append(
            {
                "gate": gate.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json") if observation else None,
                "group": {k: facts[k] for k in _GROUP_FIELDS if k in facts} if facts else None,
                "compared_statistic": gate.statistic,
                "status": status,
                "reason": reason,
                "evidence_binding": "platform_replicate_aggregate",
            }
        )
    statuses = {r["status"] for r in results}
    report = {
        "version": 2,
        "protocol_digest": record.protocol_digest,
        "record_digest": canonical_digest(record.model_dump(mode="json")),
        "plan_decision_id": record.plan_decision_id,
        "plan_scope_digest": record.plan_scope_digest,
        "identity": record.identity.model_dump(mode="json"),
        "evidence_kind": record.evidence_kind,
        "evidence_binding": "platform_replicate_aggregate",
        "status": "fail"
        if "fail" in statuses
        else ("inconclusive" if "inconclusive" in statuses else "pass"),
        "gates": results,
        "execution_granted": False,
        "scientific_validity": "not_assessed",
    }
    return {**report, "report_digest": canonical_digest(report)}
