"""Attributed messages and intent-only human decision routes."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .interaction import InteractionStore
from .interaction_models import CreateDecision, CreateMessage, CreateThread, ResolveDecision

router = APIRouter(prefix="/interaction")
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]
Offset = Annotated[int, Query(ge=0, le=100000)]
Limit = Annotated[int, Query(ge=1, le=100)]


def interactions(request: Request) -> InteractionStore:
    return request.app.state.interactions


Records = Annotated[InteractionStore, Depends(interactions)]


def response(result: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = result
    return JSONResponse(status_code=status, content=body)


@router.post("/threads")
def create_thread(body: CreateThread, actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.create_thread(actor, key, **body.model_dump(mode="json")))


@router.post("/messages")
def send_message(body: CreateMessage, actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.send_message(actor, key, **body.model_dump(mode="json")))


@router.get("/projects/{project_id}/threads")
def list_threads(
    project_id: str, actor: Actor, store: Records, offset: Offset = 0, limit: Limit = 50
) -> dict[str, Any]:
    return {"threads": store.list_threads(actor, project_id, offset, limit)}


@router.get("/threads/{thread_id}/messages")
def list_messages(
    thread_id: str, actor: Actor, store: Records, offset: Offset = 0, limit: Limit = 50
) -> dict[str, Any]:
    return {"messages": store.list_messages(actor, thread_id, offset, limit)}


@router.post("/decisions")
def request_decision(body: CreateDecision, actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.request_decision(actor, key, **body.model_dump(mode="json")))


@router.post("/decisions/resolve")
def resolve_decision(body: ResolveDecision, actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.resolve_decision(actor, key, **body.model_dump(mode="json")))


@router.get("/projects/{project_id}/decisions")
def list_decisions(
    project_id: str,
    actor: Actor,
    store: Records,
    offset: Offset = 0,
    limit: Limit = 50,
    status: Literal["pending", "approved", "rejected", "expired"] = "pending",
) -> dict[str, Any]:
    return {"decisions": store.list_decisions(actor, project_id, offset, limit, status)}


@router.post("/projects/{project_id}/decisions/expire")
def expire_decisions(project_id: str, actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.expire_decisions(actor, key, project_id))
