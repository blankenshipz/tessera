"""Human chunked snapshot upload routes (ADR 0037 s1)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .snapshot_chunks import (
    BeginChunkedSnapshot,
    ChunkedSnapshotStore,
    CompleteChunkedSnapshot,
    PutSnapshotChunk,
)

router = APIRouter()
Actor = Annotated[dict, Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def _store(request: Request) -> ChunkedSnapshotStore:
    return request.app.state.chunked_snapshots


def _reply(value) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/chunked-snapshots")
def begin_chunked_snapshot(
    project_id: str, body: BeginChunkedSnapshot, request: Request, actor: Actor, key: Key
) -> JSONResponse:
    return _reply(
        _store(request).begin_upload(actor, project_id, key, body.model_dump(mode="json"))
    )


@router.put("/projects/{project_id}/chunked-snapshots/{upload_id}/chunks/{seq}")
def put_snapshot_chunk(
    project_id: str,
    upload_id: str,
    seq: int,
    body: PutSnapshotChunk,
    request: Request,
    actor: Actor,
) -> JSONResponse:
    return _reply(
        _store(request).put_chunk(actor, project_id, upload_id, seq, body.model_dump(mode="json"))
    )


@router.post("/projects/{project_id}/chunked-snapshots/{upload_id}/complete")
def complete_chunked_snapshot(
    project_id: str,
    upload_id: str,
    request: Request,
    actor: Actor,
    key: Key,
    body: CompleteChunkedSnapshot | None = None,
) -> JSONResponse:
    payload = None if body is None else body.model_dump(mode="json")
    return _reply(_store(request).complete_upload(actor, project_id, upload_id, key, payload))


@router.post("/projects/{project_id}/chunked-snapshots/gc")
def collect_chunked_snapshot_garbage(
    project_id: str, request: Request, actor: Actor, key: Key
) -> JSONResponse:
    return _reply(_store(request).collect_garbage(actor, project_id, key))
