"""Reasoning admission routes (ADR 0163).

Authorization lives in ``ReasoningAdmissionCommands``: admitting needs a person's
``human_session`` credential, revoking any human-role credential, reading a project credential
(an enrolled external agent needs ``campaign:read``).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key
from .external_agent_api import Actor
from .reasoning_admissions import (
    CreateReasoningAdmission,
    ReasoningAdmissionCommands,
    RevokeReasoningAdmission,
)
from .store import canonical_digest

router = APIRouter()

Key = Annotated[str, Depends(idempotency_key)]


def admissions(request: Request) -> ReasoningAdmissionCommands:
    return request.app.state.reasoning_admissions


Admissions = Annotated[ReasoningAdmissionCommands, Depends(admissions)]


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/reasoning-admissions")
def create_reasoning_admission(
    project_id: str, body: CreateReasoningAdmission, actor: Actor, key: Key, store: Admissions
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.create_reasoning_admission(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/reasoning-admissions")
def list_reasoning_admissions(project_id: str, actor: Actor, store: Admissions) -> dict[str, Any]:
    return store.list_reasoning_admissions(actor, project_id)


@router.get("/projects/{project_id}/reasoning-admissions/{admission_id}")
def get_reasoning_admission(
    project_id: str, admission_id: str, actor: Actor, store: Admissions
) -> dict[str, Any]:
    return store.get_reasoning_admission(actor, project_id, admission_id)


@router.post("/projects/{project_id}/reasoning-admissions/{admission_id}/revoke")
def revoke_reasoning_admission(
    project_id: str,
    admission_id: str,
    body: RevokeReasoningAdmission,
    actor: Actor,
    key: Key,
    store: Admissions,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_reasoning_admission(
            actor, project_id, admission_id, key, canonical_digest(payload), payload
        )
    )
