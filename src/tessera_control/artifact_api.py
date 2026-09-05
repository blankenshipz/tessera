"""HTTP routes for quarantined artifacts."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal, worker_principal
from .artifact_manifests import ArtifactManifestStore, CreateArtifactManifest
from .artifacts import (
    ArtifactStore,
    BeginArtifactUpload,
    EmptyUploadRequest,
    UploadArtifact,
    UploadChunk,
)
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


def manifests(request: Request) -> ArtifactManifestStore:
    return getattr(
        request.app.state,
        "artifact_manifests",
        ArtifactManifestStore(request.app.state.store.database_path.parent),
    )


ManifestStoreDependency = Annotated[ArtifactManifestStore, Depends(manifests)]


@router.post("/worker/leases/{lease_id}/artifacts")
def upload(lease_id: str, body: UploadArtifact, worker: Worker, key: Key, store: StoreDependency):
    payload = body.model_dump(mode="json")
    status, response = store.upload_artifact(
        worker, lease_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/worker/leases/{lease_id}/artifact-uploads")
def begin_upload(
    lease_id: str, body: BeginArtifactUpload, worker: Worker, key: Key, store: StoreDependency
):
    payload = body.model_dump(mode="json")
    status, response = store.begin_upload(worker, lease_id, key, canonical_digest(payload), payload)
    return JSONResponse(status_code=status, content=response)


@router.post("/worker/leases/{lease_id}/artifact-uploads/{upload_id}/chunks")
def append_chunk(
    lease_id: str,
    upload_id: str,
    body: UploadChunk,
    worker: Worker,
    key: Key,
    store: StoreDependency,
):
    payload = body.model_dump(mode="json")
    status, response = store.append_chunk(
        worker, lease_id, upload_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/worker/leases/{lease_id}/artifact-uploads/{upload_id}/complete")
def complete_upload(
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


@router.post("/worker/leases/{lease_id}/artifact-uploads/{upload_id}/status")
def upload_status(
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


@router.post("/worker/leases/{lease_id}/artifact-manifest")
def create_manifest(
    lease_id: str,
    body: CreateArtifactManifest,
    worker: Worker,
    key: Key,
    store: ManifestStoreDependency,
):
    payload = body.model_dump(mode="json")
    status, response = store.create_manifest(
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


@router.get("/projects/{project_id}/artifact-manifests")
def list_manifests(
    project_id: str,
    actor: Actor,
    store: ManifestStoreDependency,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
):
    return {"artifact_manifests": store.list_manifests(actor, project_id, offset, limit)}
