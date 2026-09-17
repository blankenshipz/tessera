# ruff: noqa: E501
# fmt: off
"""HTTP routes for provider-disabled, mediated Explain status turns."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .astra_status import AstraStatusStore
from .astra_status_models import (
    BeginStatusToolCall,
    ClaimExplainStatusTurn,
    RecordStatusToolResult,
    RequestExplainStatusTurn,
    SettleExplainStatusTurn,
)

router = APIRouter(prefix="/astra")
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]

def status_store(request: Request) -> AstraStatusStore:
    return request.app.state.astra_status

Records = Annotated[AstraStatusStore, Depends(status_store)]

def _response(result: tuple[int, dict[str, Any]]) -> JSONResponse:
    return JSONResponse(status_code=result[0], content=result[1])

@router.post("/projects/{project_id}/explain-status-turns")
def request_turn(project_id: str, body: RequestExplainStatusTurn, actor: Actor, key: Key, store: Records): return _response(store.request_turn(actor, key, project_id, **body.model_dump(mode="json")))
@router.get("/projects/{project_id}/explain-status-turns")
def list_turns(project_id: str, actor: Actor, store: Records, offset: int = Query(0, ge=0, le=100_000), limit: int = Query(100, ge=1, le=100)): return {"turns": store.list_turns(actor, project_id, offset, limit)}
@router.get("/explain-status-turns/{turn_id}")
def get_turn(turn_id: str, actor: Actor, store: Records): return store.get_turn(actor, turn_id)
@router.post("/explain-status-turns/{turn_id}/claim")
def claim(turn_id: str, body: ClaimExplainStatusTurn, actor: Actor, key: Key, store: Records): return _response(store.claim_turn(actor, key, turn_id, **body.model_dump(mode="json")))
@router.post("/explain-status-turns/{turn_id}/tool-intent")
def intent(turn_id: str, body: BeginStatusToolCall, actor: Actor, key: Key, store: Records): return _response(store.begin_tool(actor, key, turn_id, **body.model_dump(mode="json")))
@router.post("/explain-status-turns/{turn_id}/tool-result")
def result(turn_id: str, body: RecordStatusToolResult, actor: Actor, key: Key, store: Records): return _response(store.record_result(actor, key, turn_id, **body.model_dump(mode="json")))
@router.post("/explain-status-turns/{turn_id}/answer")
def answer(turn_id: str, body: SettleExplainStatusTurn, actor: Actor, key: Key, store: Records): return _response(store.answer(actor, key, turn_id, **body.model_dump(mode="json")))
@router.post("/explain-status-turns/{turn_id}/uncertain")
def uncertain(turn_id: str, body: SettleExplainStatusTurn, actor: Actor, key: Key, store: Records): return _response(store.uncertain(actor, key, turn_id, **body.model_dump(mode="json")))
@router.post("/explain-status-turns/{turn_id}/cancel")
def cancel(turn_id: str, body: SettleExplainStatusTurn, actor: Actor, key: Key, store: Records): return _response(store.cancel(actor, key, turn_id, **body.model_dump(mode="json")))
