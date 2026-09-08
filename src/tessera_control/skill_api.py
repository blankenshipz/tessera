"""Metadata-only skill snapshot and human release routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .skills import CreateSkillSnapshot, ReleaseSkill, RevokeSkillRelease, SkillStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def skills(request: Request) -> SkillStore:
    return request.app.state.skills


Skills = Annotated[SkillStore, Depends(skills)]


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/skills")
def create_skill(
    project_id: str, body: CreateSkillSnapshot, actor: Actor, key: Key, store: Skills
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.create_skill_snapshot(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/skills")
def list_skills(
    project_id: str,
    actor: Actor,
    store: Skills,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"skills": store.list_skill_snapshots(actor, project_id, offset, limit)}


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, actor: Actor, store: Skills) -> dict[str, Any]:
    return store.get_skill_snapshot(actor, skill_id)


@router.post("/skills/{skill_id}/releases")
def release_skill(
    skill_id: str, body: ReleaseSkill, actor: Actor, key: Key, store: Skills
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.release_skill(actor, skill_id, key, canonical_digest(payload), payload))


@router.get("/skills/{skill_id}/releases")
def list_skill_releases(
    skill_id: str,
    actor: Actor,
    store: Skills,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"releases": store.list_skill_releases(actor, skill_id, offset, limit)}


@router.post("/skill-releases/{release_id}/revoke")
def revoke_skill_release(
    release_id: str, body: RevokeSkillRelease, actor: Actor, key: Key, store: Skills
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_skill_release(
            actor, release_id, key, canonical_digest(payload), body.expected_revision
        )
    )
