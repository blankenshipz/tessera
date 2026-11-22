"""Control API for the model-spend budget and ledger (ADR 0090)."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .execution_api import Key, result
from .external_agent_api import Actor
from .model_spend import MAX_CENTS, ModelSpendStore, RunIdRequired

router = APIRouter(prefix="/model-spend")

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ProviderId = Annotated[str, StringConstraints(pattern=r"^(\*|[a-z][a-z0-9-]{0,63})$")]
Cents = Annotated[float, Field(ge=0, le=MAX_CENTS, allow_inf_nan=False)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReserveBody(_Body):
    project_id: Identifier
    run_id: Identifier
    seq: Annotated[int, Field(ge=1, le=2**31 - 1)]
    provider_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    worst_case_cents: Cents


class SettleBody(_Body):
    actual_cents: Cents
    receipt_sha256: Sha256


class ReleaseBody(_Body):
    reason: Annotated[str, StringConstraints(pattern=r"^not_sent$")]


class ResolveBody(_Body):
    outcome: Literal["not_sent", "spent"]
    actual_cents: Cents | None = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]

    @field_validator("reason")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        if re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("reason must not contain control characters")
        return value

    @model_validator(mode="after")
    def _amount_matches_outcome(self) -> ResolveBody:
        if (self.outcome == "spent") != (self.actual_cents is not None):
            raise ValueError("actual_cents is required for, and only for, outcome spent")
        return self


class LimitsBody(_Body):
    project_id: Identifier
    provider_id: ProviderId
    per_call_cents: Cents
    daily_cents: Cents


def model_spend_store(request: Request) -> ModelSpendStore:
    state = request.app.state
    store = getattr(state, "model_spend", None)
    if store is None:
        store = ModelSpendStore(state.admin.database_path.parent)
        state.model_spend = store
    return store


Spend = Annotated[ModelSpendStore, Depends(model_spend_store)]


@router.post("/reservations")
def reserve(body: ReserveBody, actor: Actor, key: Key, store: Spend):
    """Reserve a worst-case cost before a billed model call; fail closed (code in the body)."""
    return result(store.reserve(actor, key, body.model_dump()))


@router.post("/reservations/{call_id}/settlement")
def settle(call_id: Identifier, body: SettleBody, actor: Actor, key: Key, store: Spend):
    return result(store.settle(actor, call_id, key, body.model_dump()))


@router.post("/reservations/{call_id}/release")
def release(call_id: Identifier, body: ReleaseBody, actor: Actor, key: Key, store: Spend):
    return result(store.release(actor, call_id, key, body.model_dump()))


@router.post("/reservations/{call_id}/resolution")
def resolve(call_id: Identifier, body: ResolveBody, actor: Actor, key: Key, store: Spend):
    """Human only: close an open reservation the supervisor could not (an uncertain call) as
    ``not_sent`` (released) or ``spent`` (settled at ``actual_cents``), with a reason."""
    return result(store.resolve(actor, call_id, key, body.model_dump()))


@router.get("/reservations/{call_id}")
def reservation(
    call_id: Identifier,
    actor: Actor,
    store: Spend,
    run_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
):
    """One call's status and resolution (read only), for a supervisor refreshing its report.

    An agent reads only its own run's calls and must pass ``run_id``; a human may omit it."""
    try:
        return store.reservation(actor, call_id, run_id)
    except RunIdRequired as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.get("")
def read(
    actor: Actor,
    store: Spend,
    project_id: Identifier,
    days: int = Query(7, ge=1, le=90),
):
    """Limits, today's totals, per-day and per-provider history and open reservations."""
    return store.read(actor, project_id, days)


@router.put("/limits")
def set_limits(body: LimitsBody, actor: Actor, key: Key, store: Spend):
    """Human only: set the per-call and daily caps for a project and provider (``*`` = default)."""
    return result(store.set_limits(actor, key, body.model_dump()))
