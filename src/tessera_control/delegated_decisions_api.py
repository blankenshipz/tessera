"""Delegated decision routes (ADR 0122): approve a campaign or record a gate go/no-go under an
owner-signed delegation. Authorization and every rule live in ``DelegatedDecisionStore``."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key
from .delegated_decisions import (
    DelegatedApprove,
    DelegatedDecisionStore,
    DelegatedGateDecision,
    DelegationRefused,
)
from .external_agent_api import Actor
from .store import canonical_digest

router = APIRouter()
Key = Annotated[str, Depends(idempotency_key)]


def decisions(request: Request) -> DelegatedDecisionStore:
    return request.app.state.delegated_decisions


Decisions = Annotated[DelegatedDecisionStore, Depends(decisions)]


def _refused(error: DelegationRefused) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(error),
            "outside_delegation": True,
            "reasons": error.reasons,
            "escalated_to": "owner",
        },
    )


@router.post("/campaigns/{campaign_id}/delegated-approval")
def delegated_approval(
    campaign_id: str, body: DelegatedApprove, actor: Actor, key: Key, store: Decisions
) -> JSONResponse:
    """Approve a submitted campaign only if the named delegation's written rule is met."""
    payload = body.model_dump(mode="json")
    try:
        status, content = store.delegated_approve(
            actor, campaign_id, key, canonical_digest(payload), payload
        )
    except DelegationRefused as error:
        return _refused(error)
    return JSONResponse(status_code=status, content=store.agent_view(actor, content))


@router.post("/campaigns/{campaign_id}/delegated-gate-decision")
def delegated_gate_decision(
    campaign_id: str, body: DelegatedGateDecision, actor: Actor, key: Key, store: Decisions
) -> JSONResponse:
    """Record a gate go or no-go only if the delegation's written rule computes to one."""
    payload = body.model_dump(mode="json")
    try:
        status, content = store.delegated_gate_decision(
            actor, campaign_id, key, canonical_digest(payload), payload
        )
    except DelegationRefused as error:
        return _refused(error)
    return JSONResponse(status_code=status, content=content)


@router.get("/projects/{project_id}/delegation-escalations")
def delegation_escalations(
    project_id: str,
    actor: Actor,
    store: Decisions,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """What the owner has been asked to decide: requests outside a delegation."""
    return store.list_delegation_escalations(actor, project_id, offset, limit)
