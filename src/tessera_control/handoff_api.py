"""Metadata-only confirmed workspace handoff routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .handoff_models import ConfirmWorkspaceStop, HandBackWorkspace, RequestWorkspaceHandoff
from .handoffs import WorkspaceHandoffStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def handoffs(request: Request) -> WorkspaceHandoffStore:
    return request.app.state.handoffs


Records = Annotated[WorkspaceHandoffStore, Depends(handoffs)]


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/workspace-handoffs")
def request_handoff(
    project_id: str, body: RequestWorkspaceHandoff, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.request_handoff(actor, project_id, key, canonical_digest(payload), payload))


@router.get("/projects/{project_id}/workspace-handoffs")
def list_handoffs(
    project_id: str,
    actor: Actor,
    store: Records,
    offset: int = Query(0, ge=0, le=2**63 - 1),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    return {"workspace_handoffs": store.list_handoffs(actor, project_id, offset, limit)}


@router.get("/workspace-handoffs/{handoff_id}")
def get_handoff(handoff_id: str, actor: Actor, store: Records) -> dict[str, Any]:
    return store.get_handoff(actor, handoff_id)


@router.post("/workspace-handoffs/{handoff_id}/confirm-stop")
def confirm_stop(
    handoff_id: str, body: ConfirmWorkspaceStop, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.confirm_stop(
            actor, handoff_id, key, canonical_digest(payload), body.expected_revision
        )
    )


@router.post("/workspace-handoffs/{handoff_id}/handback")
def hand_back(
    handoff_id: str, body: HandBackWorkspace, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.hand_back(actor, handoff_id, key, canonical_digest(payload), payload))
