"""HTTP routes for client-requested research/review jobs (ADR 0084)."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from .external_agent_api import Actor, Key
from .review_jobs import ReviewForbidden, ReviewRefused
from .review_jobs_models import (
    ApproveReviewTier,
    ResolveReviewFinding,
    SubmitCodeReview,
    SubmitReviewJob,
)

router = APIRouter()


def _response(call):
    try:
        status, body = call()
    except ReviewForbidden as refusal:
        return JSONResponse(
            status_code=403,
            content={"detail": str(refusal), "reason": refusal.reason},
        )
    except ReviewRefused as refusal:
        return JSONResponse(
            status_code=refusal.status,
            content={"detail": refusal.reason, "reason": refusal.reason},
        )
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/review-jobs")
def submit(project_id: str, body: SubmitReviewJob, request: Request, actor: Actor, key: Key):
    return _response(
        lambda: request.app.state.review_jobs.submit(
            actor,
            project_id,
            key,
            body.model_dump(mode="json"),
            request.app.state.review_policy,
        )
    )


@router.post("/projects/{project_id}/code-reviews")
def submit_code_review(
    project_id: str, body: SubmitCodeReview, request: Request, actor: Actor, key: Key
):
    """Submit a code review of a platform git branch (ADR 0108); 201 with the job record."""
    return _response(
        lambda: request.app.state.review_jobs.submit_code_review(
            actor,
            project_id,
            key,
            body.model_dump(mode="json"),
            request.app.state.review_policy,
            getattr(request.app.state, "code_review_git", None),
        )
    )


@router.get("/projects/{project_id}/review-jobs")
def jobs(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    kind: Literal[
        "plan_review", "dataset_scout", "technique_scout", "pre_gate_review", "code_review"
    ]
    | None = None,
):
    return {"jobs": request.app.state.review_jobs.list_jobs(actor, project_id, offset, limit, kind)}


@router.get("/review-jobs/{job_id}")
def job(job_id: str, request: Request, actor: Actor):
    return request.app.state.review_jobs.get_job(actor, job_id)


@router.post("/review-jobs/{job_id}/approve-tier")
def approve_tier(job_id: str, body: ApproveReviewTier, request: Request, actor: Actor, key: Key):
    return _response(
        lambda: request.app.state.review_jobs.approve_tier(
            actor, job_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/review-jobs/{job_id}/findings")
def ingest_findings(
    job_id: str,
    request: Request,
    actor: Actor,
    key: Key,
    payload: Annotated[dict[str, Any], Body()],
):
    # Validated inside the store after authorization so an unauthorized caller sees 403, and an
    # invalid reviewer output is a stable 422 failed_output_invalid with nothing stored.
    return _response(
        lambda: request.app.state.review_jobs.record_findings(actor, job_id, key, payload)
    )


@router.get("/projects/{project_id}/review-findings")
def findings(
    project_id: str,
    request: Request,
    actor: Actor,
    job_id: str | None = Query(None, max_length=200),
    state: Literal["open", "accepted", "rejected"] | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return {
        "findings": request.app.state.review_jobs.list_findings(
            actor, project_id, job_id, state, offset, limit
        )
    }


@router.get("/review-findings/{finding_id}")
def finding(finding_id: str, request: Request, actor: Actor):
    return request.app.state.review_jobs.get_finding(actor, finding_id)


@router.post("/review-findings/{finding_id}/resolve")
def resolve(finding_id: str, body: ResolveReviewFinding, request: Request, actor: Actor, key: Key):
    return _response(
        lambda: request.app.state.review_jobs.resolve(
            actor, finding_id, key, body.model_dump(mode="json")
        )
    )
