"""Authenticated routes for complete, attributed research records."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .research import ResearchStore
from .research_models import (
    AcceptFinding,
    AssessSource,
    CloseStudy,
    CorrectSource,
    CreateFinding,
    CreateSource,
    CreateStudy,
    ReaffirmFinding,
    RecordVariantStatus,
    RegisterVariant,
    RetractFinding,
    ReviseFinding,
)
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def ledger(request: Request) -> ResearchStore:
    return request.app.state.research


Ledger = Annotated[ResearchStore, Depends(ledger)]


@router.post("/projects/{project_id}/studies")
def create_study(
    project_id: str, body: CreateStudy, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.create_study(
        actor, project_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/studies/{study_id}/variants")
def register_variant(
    study_id: str, body: RegisterVariant, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.register_variant(
        actor, study_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/variants/{variant_id}/record")
def record_variant_status(
    variant_id: str, body: RecordVariantStatus, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.record_variant_status(
        actor, variant_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/studies/{study_id}/sources")
def create_source(
    study_id: str, body: CreateSource, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.create_source(actor, study_id, key, canonical_digest(payload), payload)
    return JSONResponse(status_code=status, content=response)


@router.post("/sources/{source_id}/assess")
def assess_source(
    source_id: str, body: AssessSource, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.assess_source(
        actor, source_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/studies/{study_id}/findings")
def create_finding(
    study_id: str, body: CreateFinding, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.create_finding(
        actor, study_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/findings/{finding_id}/revise")
def revise_finding(
    finding_id: str, body: ReviseFinding, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.revise_finding(
        actor, finding_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/findings/{finding_id}/accept")
def accept_finding(
    finding_id: str, body: AcceptFinding, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.accept_finding(
        actor, finding_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/findings/{finding_id}/reaffirm")
def reaffirm_finding(
    finding_id: str, body: ReaffirmFinding, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.reaffirm_finding(
        actor, finding_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/findings/{finding_id}/retract")
def retract_finding(
    finding_id: str, body: RetractFinding, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.retract_finding(
        actor, finding_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/sources/{source_id}/correct")
def correct_source(
    source_id: str, body: CorrectSource, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.correct_source(
        actor, source_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/studies/{study_id}/close")
def close_study(
    study_id: str, body: CloseStudy, actor: Actor, key: Key, store: Ledger
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.close_study(actor, study_id, key, canonical_digest(payload), payload)
    return JSONResponse(status_code=status, content=response)


@router.get("/projects/{project_id}/studies")
def list_studies(
    project_id: str,
    actor: Actor,
    store: Ledger,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"studies": store.list_studies(actor, project_id, offset, limit)}


@router.get("/studies/{study_id}")
def get_study(study_id: str, actor: Actor, store: Ledger) -> dict[str, Any]:
    return store.get_study(actor, study_id)


@router.get("/studies/{study_id}/export")
def export_study(
    study_id: str,
    actor: Actor,
    store: Ledger,
    max_bytes: int = Query(default=1_000_000, ge=256, le=10_000_000),
) -> dict[str, Any]:
    return store.export_study(actor, study_id, {"max_bytes": max_bytes})
