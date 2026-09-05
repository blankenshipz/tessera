"""HTTP routes for append-only human artifact-manifest dispositions."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .artifact_dispositions import ArtifactDispositionRequest, ArtifactDispositionStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def dispositions(request: Request) -> ArtifactDispositionStore:
    return ArtifactDispositionStore(request.app.state.store.database_path.parent)


StoreDependency = Annotated[ArtifactDispositionStore, Depends(dispositions)]


@router.post("/artifact-manifests/{manifest_id}/dispositions")
def record_disposition(
    manifest_id: str,
    body: ArtifactDispositionRequest,
    actor: Actor,
    key: Key,
    store: StoreDependency,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.record_disposition(
        actor, manifest_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/artifact-manifests/{manifest_id}/dispositions")
def list_dispositions(
    manifest_id: str,
    actor: Actor,
    store: StoreDependency,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, list[dict[str, Any]]]:
    return {"dispositions": store.list_dispositions(actor, manifest_id, offset, limit)}
