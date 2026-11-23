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
        return self
