"""Human-only, delegable composition of named sources into one input snapshot (ADR 0038)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .snapshot_composition import ComposeSnapshot
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict, Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


@router.post("/projects/{project_id}/composed-snapshots")
def compose(project_id: str, body: ComposeSnapshot, request: Request, actor: Actor, key: Key):
    payload = body.model_dump(mode="json")
    status, result = request.app.state.snapshot_composition.compose(
        actor, project_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=result)
