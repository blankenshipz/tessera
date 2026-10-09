"""Versioned research review scopes, not execution permissions or model outputs."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .evaluation_models import EvaluationProtocol, EvaluationRecord
from .research_models import Digest, NonBlank200, NonBlank4000, StrictResearchRequest

OUTCOME_CONDITIONS = {
    "outcome_0": (
        "direct_property_language",
        "uncertainty_distribution",
        "pairwise_ranking",
        "counterfactual_intervention",
        "attribution_assisted",
        "representation_ablations",
        "observable_scratchpad",
        "scaffold_ood",
        "capability_retention",
    ),
    "outcome_1": (
        "unified_mixture_schema",
        "ingredient_concentration_context",
        "permutation_aware_model",
        "baseline_vs_pretrained",
        "few_shot_curves",
        "ingredient_holdout",
        "family_holdout",
        "domain_holdout",
        "calibration",
        "leakage_controls",
    ),
}
Ids = Annotated[list[NonBlank200], Field(max_length=100)]
Notes = Annotated[list[NonBlank4000], Field(max_length=20)]


class ComputeIntent(StrictResearchRequest):
    budget_cents: Annotated[int, Field(ge=0, le=2**63 - 1)]
    max_wall_seconds: Annotated[int, Field(ge=1, le=31_536_000)]
    max_gpu_hours: Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]
    target: Literal[
        "local_cpu",
        "configured_remote",
        "salad_gpu_future",  # Historical reviews, not routing.
        "kubernetes",
        "salad_container",
        "salad_inference",
    ]
    resources: NonBlank4000


class ProgramBrief(StrictResearchRequest):
    objective: NonBlank4000
    hypothesis: NonBlank4000
    success_gate: NonBlank4000
    failure_gate: NonBlank4000
    stop_gate: NonBlank4000
    target_domain: NonBlank4000
    provenance_policy: NonBlank4000
    data_source_ids: Annotated[list[NonBlank200], Field(min_length=1, max_length=100)]
    compute: ComputeIntent
    dataset_recommendation: NonBlank4000
    representation_recommendation: NonBlank4000
    split_recommendation: NonBlank4000
    metric_recommendation: NonBlank4000
    uncertainty: NonBlank4000
    missing_prerequisites: Notes


class ConditionReview(StrictResearchRequest):
    disposition: Literal["selected", "deferred"]
    rationale: NonBlank4000
    variant_ids: Ids

    @model_validator(mode="after")
    def selected_has_registered_work(self):
        if (self.disposition == "selected") != bool(self.variant_ids):
            raise ValueError("selected conditions need variants; deferred conditions need none")
        return self


class ProgramReview(StrictResearchRequest):
    type: Literal["research_program"]
    version: Literal[1]
    study_id: NonBlank200
    outcome: Literal["outcome_0", "outcome_1"]
    action: Literal["plan", "source_use", "evaluation", "go_no_go"]
    basis_digest: Digest
    supersedes_id: NonBlank200 | None = None
    reason: NonBlank4000
    brief: ProgramBrief | None = None
    conditions: dict[str, ConditionReview] = Field(default_factory=dict, max_length=20)
    source_ids: Ids = []
    intended_use: NonBlank4000 | None = None
    finding_ids: Ids = []
    recommendation: Literal["go", "no_go"] | None = None
    evaluation_protocol: EvaluationProtocol | None = None
    evaluation: EvaluationRecord | None = None
    evaluation_id: NonBlank200 | None = None

    @model_validator(mode="after")
    def complete_review(self):
        if self.evaluation_protocol is not None and self.action != "plan":
            raise ValueError("only plans declare evaluation protocols")
        if (self.action == "evaluation") != (self.evaluation is not None):
            raise ValueError("evaluation reviews require an evaluation record, and only those do")
        if self.evaluation_id is not None and self.action != "go_no_go":
            raise ValueError("only go/no-go reviews reference evaluation decisions")
        for values in (self.source_ids, self.finding_ids):
            if len(set(values)) != len(values):
                raise ValueError("reference IDs must be unique")
        if self.action == "plan":
            if self.brief is None or set(self.conditions) != set(OUTCOME_CONDITIONS[self.outcome]):
                raise ValueError(
                    "plan needs a brief and complete selected/deferred condition accounting"
                )
            if not any(c.disposition == "selected" for c in self.conditions.values()):
                raise ValueError("plan needs at least one selected condition")
        elif self.brief is not None or self.conditions:
            raise ValueError("only plan reviews carry a brief and conditions")
        if self.action == "source_use":
            if not self.source_ids or self.intended_use is None:
                raise ValueError("source-use review needs sources and intended use")
        elif self.source_ids or self.intended_use is not None:
            raise ValueError("only source-use reviews carry source use")
        if self.action == "go_no_go":
            if self.recommendation is None:
                raise ValueError("direction review needs an explicit go/no-go recommendation")
            if self.recommendation == "go" and not self.finding_ids:
                raise ValueError("go needs human-reviewed finding evidence")
        elif self.recommendation is not None or self.finding_ids:
            raise ValueError("only go/no-go reviews carry findings and a recommendation")
        return self
