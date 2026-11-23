"""The written terms of an owner-signed delegation (ADR 0122).

A delegation lets the control plane, or an agent holding the matching scope, make a decision a
person would otherwise make (approve a campaign, record a gate go or no-go) *only when the written
rule is met*. The terms are fixed when a person signs the record and are never edited; the rules
are evaluated by the platform from recorded data, never from an agent's claim. Anything outside the
terms fails closed and is escalated to the owner.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from .campaign_gate_models import BoundOutcomeRule

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Cents = Annotated[int, Field(strict=True, ge=1, le=10_000_000)]

TERMS_VERSION = 1
MAX_PROVIDERS = 16
MAX_GATE_RULES = 8
MAX_DELEGATION_DAYS = 30
MAX_APPROVALS = 1000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GateRule(_Strict):
    """A written rule for one kind of gate decision: the platform computes the pre-registered
    paired-delta outcome for the campaign being gated (never an agent's number) and maps it to a
    decision. ``on_pass`` / ``on_fail`` name the decision a pass / fail records; an unset one is
    left to the owner (the request is escalated, nothing is recorded as a decision)."""

    name: Slug
    bound_outcome: BoundOutcomeRule
    on_pass: Literal["go", "no_go"] | None = "go"
    on_fail: Literal["go", "no_go"] | None = "no_go"

    @model_validator(mode="after")
    def decides_something(self) -> GateRule:
        if self.on_pass is None and self.on_fail is None:
            raise ValueError("a gate rule needs on_pass or on_fail")
        return self


ReviewKind = Literal["source_assessment", "plan", "source_use"]
ProviderClass = Literal["salad_gpu", "salad_cpu"]
MAX_REVIEW_INTENTS = 16
MAX_REVIEW_SECONDS = 86_400


class ReviewRule(_Strict):
    """A written rule for the per-run Salad reviews (ADR 0150): which review kinds the platform
    may resolve for a campaign approved under this delegation, and the bounds every reviewed
    stage run must sit inside. Optional; absent, a delegation covers no review (ADR 0122)."""

    # The review kinds covered; any other kind stays with a person.
    kinds: Annotated[list[ReviewKind], Field(min_length=1, max_length=3)]
    # The most one reviewed stage run may hold (its ``max_exposure_cents``); never above the
    # delegation's ``per_run_cap_cents``.
    max_attempt_cents: Cents
    # The longest one reviewed stage run may last (its ``max_duration_seconds``).
    max_duration_seconds: Annotated[int, Field(strict=True, ge=1, le=MAX_REVIEW_SECONDS)]
    # The compute a reviewed stage run may buy: a Salad GPU or a Salad CPU-only container.
    provider_classes: Annotated[list[ProviderClass], Field(min_length=1, max_length=2)]
    # The program outcomes a source may be used for (the source-use review's ``outcome``).
    source_use_intents: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
            ]
        ],
        Field(min_length=1, max_length=MAX_REVIEW_INTENTS),
    ]

    @field_validator("kinds", "provider_classes", "source_use_intents")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("review rule entries must be unique")
        return value


class DelegationTerms(_Strict):
    version: Literal[1] = TERMS_VERSION
    # A program the operator configured (program budgets); when set, the campaign's project must
    # be covered by that program, and the program's own cap still applies at admission.
    program_id: Identifier | None = None
    # The most this delegation may commit in total: the sum of the budget caps of the campaigns
    # approved under it that are not cancelled, counted under the write lock.
    budget_ceiling_cents: Cents
    # The most one approved campaign may hold (its spec's budget cap).
    per_run_cap_cents: Cents
    # Every provider a stage names (its provider, alternates and reroute targets) must be listed.
    allowed_providers: Annotated[list[Identifier], Field(min_length=1, max_length=MAX_PROVIDERS)]
    max_approvals: Annotated[int, Field(strict=True, ge=1, le=MAX_APPROVALS)] | None = None
    gate_rules: Annotated[list[GateRule], Field(max_length=MAX_GATE_RULES)] = []  # noqa: RUF012
    # ADR 0150: the per-run Salad reviews this delegation may resolve. Absent from the
    # canonical form (and so from the terms digest) when unset: existing digests are unchanged.
    review_rule: ReviewRule | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_review_rule(self, handler):
        value = handler(self)
        if self.review_rule is None:
            value.pop("review_rule", None)
        return value

    @field_validator("allowed_providers")
    @classmethod
    def unique_providers(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_providers must be unique")
        return value

    @field_validator("gate_rules")
    @classmethod
    def unique_rule_names(cls, value: list[GateRule]) -> list[GateRule]:
        names = [rule.name for rule in value]
        if len(set(names)) != len(names):
            raise ValueError("gate rule names must be unique")
        return value

    @model_validator(mode="after")
    def per_run_within_ceiling(self) -> DelegationTerms:
        if self.per_run_cap_cents > self.budget_ceiling_cents:
            raise ValueError("per_run_cap_cents cannot exceed budget_ceiling_cents")
        if self.review_rule is not None and (
            self.review_rule.max_attempt_cents > self.per_run_cap_cents
        ):
            # A review rule only narrows: it never lets one run hold more than the delegation.
            raise ValueError("review_rule.max_attempt_cents cannot exceed per_run_cap_cents")
        return self
