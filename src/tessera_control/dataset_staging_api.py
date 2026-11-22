"""Human staging of released collected outputs as an input snapshot (ADR 0034 s4)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .dataset_staging import StageArtifactSnapshot

router = APIRouter()
Actor = Annotated[dict, Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


@router.post("/projects/{project_id}/artifact-snapshots")
def stage(project_id: str, body: StageArtifactSnapshot, request: Request, actor: Actor, key: Key):
    status, result = request.app.state.dataset_staging.stage(
        actor, project_id, key, body.model_dump(mode="json")
    )
    return JSONResponse(status_code=status, content=result)


@router.get("/snapshots/{snapshot_id}/staging")
def staging(snapshot_id: str, request: Request, actor: Actor):
    return request.app.state.dataset_staging.get_staging(actor, snapshot_id)
