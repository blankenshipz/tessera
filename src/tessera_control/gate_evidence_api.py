"""Authenticated gate-evidence routes (split locks, leakage audits, attempt binding)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .gate_evidence import GateEvidenceStore
from .gate_evidence_models import BindAttemptSplit, CreateCrossSetLeakageAudit, CreateSplitLock
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def gate_store(request: Request) -> GateEvidenceStore:
    return request.app.state.gate_evidence


Evidence = Annotated[GateEvidenceStore, Depends(gate_store)]


def response(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    return JSONResponse(status_code=value[0], content=value[1])


@router.post("/projects/{project_id}/split-locks")
def create_split_lock(
    project_id: str, body: CreateSplitLock, actor: Actor, key: Key, store: Evidence
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        store.create_split_lock(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/split-locks/{lock_id}")
def get_split_lock(lock_id: str, actor: Actor, store: Evidence) -> dict[str, Any]:
    return store.get_split_lock(actor, lock_id)


@router.post("/split-locks/{lock_id}/leakage-audits")
def create_cross_set_audit(
    lock_id: str, body: CreateCrossSetLeakageAudit, actor: Actor, key: Key, store: Evidence
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        store.create_cross_set_audit(actor, lock_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiment-attempts/{attempt_id}/split-binding")
def bind_attempt_split(
    attempt_id: str, body: BindAttemptSplit, actor: Actor, key: Key, store: Evidence
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        store.bind_attempt_split(actor, attempt_id, key, canonical_digest(payload), payload)
    )


@router.get("/experiment-attempts/{attempt_id}/split-binding")
def get_attempt_split_binding(attempt_id: str, actor: Actor, store: Evidence) -> dict[str, Any]:
    return store.get_attempt_split_binding(actor, attempt_id)
