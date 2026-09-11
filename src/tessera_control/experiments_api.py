"""Authenticated goal-to-decision record routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .experiments import EXPORT_DEFAULT_BYTES, EXPORT_MAX_BYTES, ExperimentStore
from .experiments_models import (
    AdmitExperimentAttempt,
    AmendConclusion,
    CreateConclusion,
    CreateExperiment,
    LinkAttemptArtifacts,
    RecordAttempt,
    RecordMeasurement,
    ResolveAdmittedAttempt,
    ReviewConclusion,
    ReviseExperimentDesign,
    UpdateConclusion,
)
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def store(request: Request) -> ExperimentStore:
    return request.app.state.experiments


Experiments = Annotated[ExperimentStore, Depends(store)]


def response(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    return JSONResponse(status_code=value[0], content=value[1])


@router.get("/projects/{project_id}/experiments")
def list_experiments(
    project_id: str,
    actor: Actor,
    records: Experiments,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"experiments": records.list_experiments(actor, project_id, offset, limit)}


@router.post("/projects/{project_id}/experiments")
def create(
    project_id: str, body: CreateExperiment, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.create_experiment(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiments/{experiment_id}/designs")
def revise(
    experiment_id: str, body: ReviseExperimentDesign, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.revise_design(actor, experiment_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiment-variants/{variant_id}/attempts")
def attempt(
    variant_id: str, body: RecordAttempt, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.record_attempt(actor, variant_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiment-variants/{variant_id}/admitted-attempts")
def admit_attempt(
    variant_id: str, body: AdmitExperimentAttempt, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.admit_attempt(actor, variant_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiment-admitted-attempts/{attempt_id}/resolve")
def resolve_admitted_attempt(
    attempt_id: str, body: ResolveAdmittedAttempt, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.resolve_admitted_attempt(actor, attempt_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiment-attempts/{attempt_id}/artifacts")
def artifacts(
    attempt_id: str, body: LinkAttemptArtifacts, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.link_attempt_artifacts(actor, attempt_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiment-attempts/{attempt_id}/measurements")
def measurement(
    attempt_id: str, body: RecordMeasurement, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.record_measurement(actor, attempt_id, key, canonical_digest(payload), payload)
    )


@router.post("/experiments/{experiment_id}/conclusions")
def conclusion(
    experiment_id: str, body: CreateConclusion, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.create_conclusion(actor, experiment_id, key, canonical_digest(payload), payload)
    )


@router.put("/conclusions/{conclusion_id}")
def update(
    conclusion_id: str, body: UpdateConclusion, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.update_conclusion(actor, conclusion_id, key, canonical_digest(payload), payload)
    )


@router.post("/conclusions/{conclusion_id}/review")
def review(
    conclusion_id: str, body: ReviewConclusion, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.review_conclusion(actor, conclusion_id, key, canonical_digest(payload), payload)
    )


@router.post("/conclusions/{conclusion_id}/amendments")
def amend(
    conclusion_id: str, body: AmendConclusion, actor: Actor, key: Key, records: Experiments
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return response(
        records.amend_conclusion(actor, conclusion_id, key, canonical_digest(payload), payload)
    )


@router.get("/experiments/{experiment_id}")
@router.get("/experiments/{experiment_id}/export")
def export(
    experiment_id: str,
    actor: Actor,
    records: Experiments,
    max_bytes: int = Query(default=EXPORT_DEFAULT_BYTES, ge=256, le=EXPORT_MAX_BYTES),
) -> dict[str, Any]:
    return records.export_experiment(actor, experiment_id, {"max_bytes": max_bytes})
