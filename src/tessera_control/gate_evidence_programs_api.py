"""Authenticated gate-evidence programs routes."""

from typing import Any

from fastapi import APIRouter, Query

from .gate_evidence_api import Actor, Evidence, Key, response
from .gate_evidence_models import AttachProgramOutcome, CreateResearchProgram
from .store import canonical_digest

router = APIRouter()


@router.post("/projects/{project_id}/research-programs")
def create_research_program(
    project_id: str, body: CreateResearchProgram, actor: Actor, key: Key, store: Evidence
):
    payload = body.model_dump(mode="json")
    return response(
        store.create_research_program(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.post("/research-programs/{program_id}/outcomes")
def attach_program_outcome(
    program_id: str, body: AttachProgramOutcome, actor: Actor, key: Key, store: Evidence
):
    payload = body.model_dump(mode="json")
    return response(
        store.attach_program_outcome(actor, program_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/research-programs")
def list_research_programs(
    project_id: str,
    actor: Actor,
    store: Evidence,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"research_programs": store.list_research_programs(actor, project_id, offset, limit)}


@router.get("/research-programs/{program_id}")
def get_research_program(program_id: str, actor: Actor, store: Evidence) -> dict[str, Any]:
    return store.get_research_program(actor, program_id)
