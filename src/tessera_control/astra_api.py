"""HTTP routes for Astra's provider-disabled coordinator records."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .astra import AstraStore
from .astra_models import (
    CancelCoordinatorTurn,
    ClaimCoordinatorTurn,
    CompleteCoordinatorTurn,
    FailCoordinatorTurn,
    RegisterProposalDesign,
    RequestCoordinatorTurn,
)

router = APIRouter(prefix="/astra")
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]
Offset = Annotated[int, Query(ge=0, le=100_000)]
Limit = Annotated[int, Query(ge=1, le=100)]


def astra(request: Request) -> AstraStore:
    return request.app.state.astra


Records = Annotated[AstraStore, Depends(astra)]


def response(result: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = result
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/turns")
def request_turn(
    project_id: str, body: RequestCoordinatorTurn, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    return response(store.request_turn(actor, key, project_id, **body.model_dump(mode="json")))


@router.post("/turns/{turn_id}/claim")
def claim_turn(
    turn_id: str, body: ClaimCoordinatorTurn, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    return response(store.claim_turn(actor, key, turn_id, **body.model_dump(mode="json")))


@router.post("/turns/next-claim")
def claim_next_turn(actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.claim_next_turn(actor, key))


@router.post("/consumer/heartbeat")
def consumer_heartbeat(actor: Actor, key: Key, store: Records) -> JSONResponse:
    return response(store.consumer_heartbeat(actor, key))


@router.get("/projects/{project_id}/consumer-status")
def consumer_status(project_id: str, actor: Actor, store: Records) -> dict[str, Any]:
    return store.consumer_status(actor, project_id)


@router.post("/turns/{turn_id}/complete")
def complete_turn(
    turn_id: str, body: CompleteCoordinatorTurn, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    return response(store.complete_turn(actor, key, turn_id, **body.model_dump(mode="json")))


@router.post("/turns/{turn_id}/fail")
def fail_turn(
    turn_id: str, body: FailCoordinatorTurn, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    return response(store.fail_turn(actor, key, turn_id, **body.model_dump(mode="json")))


@router.post("/turns/{turn_id}/cancel")
def cancel_turn(
    turn_id: str, body: CancelCoordinatorTurn, actor: Actor, key: Key, store: Records
) -> JSONResponse:
    return response(store.cancel_turn(actor, key, turn_id, **body.model_dump(mode="json")))


@router.get("/projects/{project_id}/conversation")
def get_conversation(
    project_id: str, actor: Actor, store: Records, offset: Offset = 0, limit: Limit = 50
) -> dict[str, Any]:
    return store.get_conversation(actor, project_id, offset, limit)


@router.get("/turns/{turn_id}")
def get_turn(turn_id: str, actor: Actor, store: Records) -> dict[str, Any]:
    return store._turn_response(store.get_turn(actor, turn_id))


@router.get("/projects/{project_id}/proposals")
def list_proposals(
    project_id: str, actor: Actor, store: Records, offset: Offset = 0, limit: Limit = 50
) -> dict[str, Any]:
    return {"proposals": store.list_proposals(actor, project_id, offset, limit)}


@router.post("/proposals/{proposal_id}/register-design")
def register_design(
    proposal_id: str,
    body: RegisterProposalDesign,
    actor: Actor,
    key: Key,
    store: Records,
) -> JSONResponse:
    return response(
        store.register_proposal_design(actor, key, proposal_id, **body.model_dump(mode="json"))
    )
