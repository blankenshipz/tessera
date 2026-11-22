"""HTTP routes for large artifact uploads (worker audience)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal, worker_principal
from .artifacts import EmptyUploadRequest
from .large_artifacts import BeginLargeUpload, LargeArtifactStore, PutLargeChunk
from .store import canonical_digest

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]
Actor = Annotated[dict[str, Any], Depends(principal)]


def large_artifacts(request: Request) -> LargeArtifactStore:
    return getattr(
        request.app.state,
        "large_artifacts",
        LargeArtifactStore(request.app.state.store.database_path.parent),
    )


StoreDependency = Annotated[LargeArtifactStore, Depends(large_artifacts)]


@router.post("/worker/leases/{lease_id}/large-artifact-uploads")
def begin_large_upload(
    lease_id: str, body: BeginLargeUpload, worker: Worker, key: Key, store: StoreDependency
):
    payload = body.model_dump(mode="json")
    status, response = store.begin_upload(worker, lease_id, key, canonical_digest(payload), payload)
    return JSONResponse(status_code=status, content=response)


@router.post("/worker/leases/{lease_id}/large-artifact-uploads/{upload_id}/status")
def large_upload_status(
    lease_id: str,
    upload_id: str,
    body: EmptyUploadRequest,
    worker: Worker,
    key: Key,
    store: StoreDependency,
):
    payload = body.model_dump(mode="json")
    status, response = store.upload_status(
        worker, lease_id, upload_id, key, canonical_digest(payload)
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/worker/leases/{lease_id}/large-artifact-uploads/{upload_id}/chunks/{seq}")
def put_large_chunk(
    lease_id: str,
    upload_id: str,
    seq: int,
    body: PutLargeChunk,
    worker: Worker,
    store: StoreDependency,
):
    status, response = store.put_chunk(worker, lease_id, upload_id, seq, body.model_dump())
    return JSONResponse(status_code=status, content=response)


@router.post("/worker/leases/{lease_id}/large-artifact-uploads/{upload_id}/complete")
def complete_large_upload(
    lease_id: str,
    upload_id: str,
    body: EmptyUploadRequest,
    worker: Worker,
    key: Key,
    store: StoreDependency,
):
    payload = body.model_dump(mode="json")
    status, response = store.complete_upload(
        worker, lease_id, upload_id, key, canonical_digest(payload)
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/projects/{project_id}/large-artifacts/reclaim")
def reclaim_large_artifacts(
    project_id: str, body: EmptyUploadRequest, actor: Actor, key: Key, store: StoreDependency
):
    """A human frees reclaimable large artifacts (unmanifested with no live lease, or rejected).

    At most 64 per call; repeat for more. A replay with the same key returns the first answer.
    """
    payload = body.model_dump(mode="json")
    status, response = store.reclaim_for_human(actor, project_id, key, canonical_digest(payload))
    return JSONResponse(status_code=status, content=response)
