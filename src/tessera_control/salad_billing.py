"""Billing declarations only. No ledger, provider hard cap or execution authority."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Cents = Annotated[int, Field(strict=True, ge=0, le=5000)]


class BillingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["credits_only", "aggregate_capped"] = "credits_only"
    ceiling_cents: Annotated[int, Field(strict=True, ge=1, le=5000)] | None = None
    prior_cost_cents: Cents | None = None
    reserved_cents: Cents | None = None
    ancillary_cents: Cents | None = None
    shutdown_cents: Cents | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def bounded(self):
        amounts = [
            self.prior_cost_cents,
            self.reserved_cents,
            self.ancillary_cents,
            self.shutdown_cents,
        ]
        if self.mode == "credits_only":
            if self.ceiling_cents is not None or any(v is not None for v in amounts):
                raise ValueError("Credits-only declarations cannot authorize cash exposure.")
        elif (
            self.ceiling_cents is None
            or sum(v for v in amounts if v is not None) > self.ceiling_cents
        ):
            raise ValueError("Aggregate declarations must fit the bounded ceiling.")
        return self


class BillingDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: BillingPolicy
    accounting: Literal["unknown", "declared_unverified"]
    lifetime: Literal["unbounded_unverified", "current", "expired"]
    ledger_verified: Literal[False] = False
    enforcement_verified: Literal[False] = False
    execution_enabled: Literal[False] = False


def billing_projection(policy: BillingPolicy | None, *, now=None):
    policy = policy or BillingPolicy()
    now = now or datetime.now(UTC)
    lifetime = (
        "unbounded_unverified"
        if policy.expires_at is None
        else "expired"
        if policy.expires_at <= now
        else "current"
    )
    known = all(
        v is not None
        for v in [
            policy.prior_cost_cents,
            policy.reserved_cents,
            policy.ancillary_cents,
            policy.shutdown_cents,
        ]
    )
    return {
        "billing": "no_cash_hard_cap_unverified"
        if policy.mode == "credits_only"
        else "aggregate_cap_expired"
        if lifetime == "expired"
        else "aggregate_cap_unverified",
        "billing_declaration": BillingDeclaration(
            policy=policy,
            accounting="declared_unverified" if known else "unknown",
            lifetime=lifetime,
        ).model_dump(mode="json"),
    }
