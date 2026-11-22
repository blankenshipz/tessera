"""Unlock requirement and gate decision request models (ADR 0045).

Kept free of other Tessera imports so campaign_models can embed them."""

from __future__ import annotations

import math
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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]

MAX_REQUIREMENTS = 4
Finite = Annotated[float, Field(allow_inf_nan=False)]


class BoundOutcomeRule(_Strict):
    """A pre-registered paired-delta gate: the human fixes the arms, the series, the
    statistic and the threshold before any result exists; the platform computes the rest."""

    arm_a_variant_id: Identifier
    arm_b_variant_id: Identifier
    metric: Identifier
    split_lock_id: Identifier
    partition: Identifier
    ci_method: Literal["t_interval_95", "percentile_bootstrap_95"] = "t_interval_95"
    statistic: Literal["mean", "ci_low", "ci_high"]
    comparison: Literal["ge", "gt", "le", "lt"]
    threshold: Finite
    minimum_pairs: Annotated[int, Field(strict=True, ge=2, le=1000)]
    minimum_samples: Annotated[int, Field(strict=True, ge=1, le=2**53)] = 1
    allowed_sources: Annotated[
        list[Literal["derived", "human_entered"]], Field(min_length=1, max_length=2)
    ] = ["derived"]  # noqa: RUF012
    allowed_outcomes: Annotated[list[Literal["pass", "fail"]], Field(min_length=1, max_length=2)]

    @field_validator("allowed_sources", "allowed_outcomes")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("values must be unique")
        return value

    @model_validator(mode="after")
    def distinct_arms(self) -> BoundOutcomeRule:
        if self.arm_a_variant_id == self.arm_b_variant_id:
            raise ValueError("the two arms must be different variants")
        if not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite")
        return self


class MeasurementCondition(_Strict):
    """A fail-closed predicate on one measurement of the named campaign's terminal receipt.

    It only ever narrows: it is checked in addition to the requirement's go or bound outcome,
    never instead of it, and every budget and authority check is unchanged. The named stage
    must have exactly one succeeded attempt that recorded a finite value under ``name``.
    """

    stage: Identifier
    name: Identifier
    op: Literal["lt", "le", "gt", "ge", "eq"]
    value: Finite


class UnlockRequirement(_Strict):
    campaign_id: Identifier
    accept_human_go: bool = False
    bound_outcome: BoundOutcomeRule | None = None
    # Absent keeps every existing requirement digest (and stored evaluation) unchanged.
    measurement: MeasurementCondition | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_measurement(self, handler):
        value = handler(self)
        if self.measurement is None:
            value.pop("measurement", None)
        return value

    @model_validator(mode="after")
    def accepts_something(self) -> UnlockRequirement:
        if not self.accept_human_go and self.bound_outcome is None:
            raise ValueError("an unlock requirement needs accept_human_go or a bound_outcome")
        return self


UnlockList = Annotated[list[UnlockRequirement], Field(min_length=1, max_length=MAX_REQUIREMENTS)]


class RecordGateDecision(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    decision: Literal["go", "no_go"]
    note: Annotated[str, Field(min_length=1, max_length=1000)]
