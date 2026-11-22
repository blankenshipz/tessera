"""Strict experiment-parent accounting commands, separate from billing metadata."""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Money = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Reference = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class AuthorizeExperimentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    ceiling_cents: Annotated[int, Field(strict=True, ge=1, le=5000)]
    expires_at: AwareDatetime
    evidence_digest: Digest
    source_reference: Reference
    prior_settled_cents: Money | None = None
    prior_pending_cents: Money | None = None
    ancillary_cents: Money | None = None
    shutdown_cents: Money | None = None


class ResolveExperimentBudgetBounds(BaseModel):
    """One-shot resolution of currently-unknown bounds; never edits a known bound."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    evidence_digest: Digest
    source_reference: Reference
    prior_settled_cents: Money | None = None
    prior_pending_cents: Money | None = None
    ancillary_cents: Money | None = None
    shutdown_cents: Money | None = None

    @model_validator(mode="after")
    def _at_least_one_bound(self):
        if all(
            getattr(self, name) is None
            for name in (
                "prior_settled_cents",
                "prior_pending_cents",
                "ancillary_cents",
                "shutdown_cents",
            )
        ):
            raise ValueError("At least one bound must be provided.")
        return self


class RecordExperimentCharge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    delivery_id: Reference
    evidence_digest: Digest
    source_reference: Reference
    category: Literal["compute", "prior", "ancillary", "shutdown"]
    admitted_attempt_id: Reference | None = None
    amount_cents: Money


class SaladBudgetBinding(BaseModel):
    """Authenticated control assertion, only a recovery constraint at the guardian."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: Reference
    admitted_attempt_id: Reference
    grant_id: Reference
    authorization_revision: Annotated[int, Field(strict=True, ge=1)]
    reservation_digest: Digest
    operation_id: Reference
    lease_id: Reference
    epoch: Annotated[int, Field(strict=True, ge=1)]
    package_digest: Digest
    max_exposure_cents: Money
    expires_at: AwareDatetime
