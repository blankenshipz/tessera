"""Authenticated gate-evidence metrics routes."""

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .gate_evidence_api import Actor, Evidence, Key, response
from .gate_evidence_models import CreatePairedReplicateGroup, CreateReplicateGroup, RecordMetric
from .store import canonical_digest

router = APIRouter()


@router.post("/experiment-attempts/{attempt_id}/metric-records")
def record_metric(
    attempt_id: str, body: RecordMetric, actor: Actor, key: Key, store: Evidence
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(store.record_metric(actor, attempt_id, key, canonical_digest(payload), payload))


@router.get("/experiment-attempts/{attempt_id}/metric-records")
def list_metric_records(attempt_id: str, actor: Actor, store: Evidence) -> dict[str, Any]:
    return {"metric_records": store.list_metric_records(actor, attempt_id)}


@router.post("/projects/{project_id}/replicate-groups")
def create_replicate_group(
    project_id: str, body: CreateReplicateGroup, actor: Actor, key: Key, store: Evidence
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        store.create_replicate_group(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/replicate-groups/{group_id}")
def get_replicate_group(group_id: str, actor: Actor, store: Evidence) -> dict[str, Any]:
    return store.get_replicate_group(actor, group_id)


@router.post("/projects/{project_id}/paired-replicate-groups")
def create_paired_group(
    project_id: str, body: CreatePairedReplicateGroup, actor: Actor, key: Key, store: Evidence
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        store.create_paired_group(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/paired-replicate-groups/{group_id}")
def get_paired_group(group_id: str, actor: Actor, store: Evidence) -> dict[str, Any]:
    return store.get_paired_group(actor, group_id)
