"""HTTP routes for quarantined artifacts."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal, worker_principal
from .artifacts import ArtifactStore, UploadArtifact
from .store import canonical_digest

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def artifacts(request: Request) -> ArtifactStore:
    return getattr(
        request.app.state,
        "artifacts",
        ArtifactStore(request.app.state.store.database_path.parent),
    )


StoreDependency = Annotated[ArtifactStore, Depends(artifacts)]


@router.post("/worker/leases/{lease_id}/artifacts")
def upload(lease_id: str, body: UploadArtifact, worker: Worker, key: Key, store: StoreDependency):
    payload = body.model_dump(mode="json")
    status, response = store.upload_artifact(
        worker, lease_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/projects/{project_id}/artifacts")
def list_artifacts(
    project_id: str,
    actor: Actor,
    store: StoreDependency,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
):
    return {"artifacts": store.list_artifacts(actor, project_id, offset, limit)}
