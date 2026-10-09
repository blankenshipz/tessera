"""Authenticated metadata-only MixFM dataset-ingestion routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .dataset_ingestion import DatasetIngestionStore
from .dataset_ingestion_models import (
    CreateIngestionProposal,
    RegisterDatasetSnapshot,
    ReviewIngestionProposal,
)
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def ingestion(request: Request) -> DatasetIngestionStore:
    return request.app.state.research


Ingestion = Annotated[DatasetIngestionStore, Depends(ingestion)]


@router.post("/studies/{study_id}/dataset-snapshots")
def register_dataset_snapshot(
    study_id: str, body: RegisterDatasetSnapshot, actor: Actor, key: Key, store: Ingestion
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.register_dataset_snapshot(
        actor, study_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/studies/{study_id}/dataset-snapshots")
def list_dataset_snapshots(
    study_id: str,
    actor: Actor,
    store: Ingestion,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"dataset_snapshots": store.list_dataset_snapshots(actor, study_id, offset, limit)}


@router.post("/studies/{study_id}/dataset-ingestion-proposals")
def create_ingestion_proposal(
    study_id: str, body: CreateIngestionProposal, actor: Actor, key: Key, store: Ingestion
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.create_ingestion_proposal(
        actor, study_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/dataset-ingestion-proposals/{proposal_id}/review")
def review_ingestion_proposal(
    proposal_id: str, body: ReviewIngestionProposal, actor: Actor, key: Key, store: Ingestion
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.review_ingestion_proposal(
        actor, proposal_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=response)
