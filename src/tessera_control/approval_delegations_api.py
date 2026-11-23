"""Approval delegation routes (ADR 0122).

Authorization lives in ``ApprovalDelegationCommands``: creating and revoking are human-only,
reads need ``campaign:read`` for a scoped agent credential.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key
from .approval_delegations import (
    ApprovalDelegationCommands,
    CreateApprovalDelegation,
    RevokeApprovalDelegation,
)
from .external_agent_api import Actor
from .store import canonical_digest

router = APIRouter()

Key = Annotated[str, Depends(idempotency_key)]


def delegations(request: Request) -> ApprovalDelegationCommands:
    return request.app.state.approval_delegations


Delegations = Annotated[ApprovalDelegationCommands, Depends(delegations)]


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/approval-delegations")
def create_approval_delegation(
    project_id: str, body: CreateApprovalDelegation, actor: Actor, key: Key, store: Delegations
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.create_approval_delegation(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/approval-delegations")
def list_approval_delegations(
    project_id: str,
    actor: Actor,
    store: Delegations,
    status: Literal["active", "revoked"] | None = None,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return store.list_approval_delegations(actor, project_id, status, offset, limit)


@router.get("/approval-delegations/{delegation_id}")
def get_approval_delegation(delegation_id: str, actor: Actor, store: Delegations) -> dict[str, Any]:
    return store.get_approval_delegation(actor, delegation_id)


@router.post("/approval-delegations/{delegation_id}/revoke")
def revoke_approval_delegation(
    delegation_id: str,
    body: RevokeApprovalDelegation,
    actor: Actor,
    key: Key,
    store: Delegations,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_approval_delegation(
            actor, delegation_id, key, canonical_digest(payload), payload
        )
    )


@router.get("/approval-delegations/{delegation_id}/decisions")
def list_delegated_decisions(
    delegation_id: str,
    actor: Actor,
    store: Delegations,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return store.list_delegated_decisions(actor, delegation_id, offset, limit)
