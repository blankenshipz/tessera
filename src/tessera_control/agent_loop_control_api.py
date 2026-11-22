"""HTTP routes for recorded agent-loop pause requests (ADR 0089)."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .agent_loop_control import PauseAgentLoop
from .external_agent_api import Actor, Key

router = APIRouter()


@router.post("/projects/{project_id}/agent-loop/pause")
def pause(project_id: str, body: PauseAgentLoop, request: Request, actor: Actor, key: Key):
    status, response = request.app.state.agent_loop_control.pause(
        actor, project_id, key, body.model_dump(mode="json")
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/projects/{project_id}/agent-loop/pauses")
def pauses(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return {
        "pauses": request.app.state.agent_loop_control.list_pauses(actor, project_id, offset, limit)
    }
