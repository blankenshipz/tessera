"""Approved campaign submission routes (ADR 0093).

Authorization lives in ``CampaignSubmissionStore``: creating is human-only, consuming needs
``campaign:submit_cleared`` for a non-human credential, cancelling is reduce-only
(``campaign:cancel``) and reads need ``campaign:read``. The operator switch is read from
``app.state.agent_submission`` on every call; when unset the feature is off.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key
from .campaign_submissions import (
    AgentSubmissionConfig,
    CampaignSubmissionStore,
    CancelSubmissionApproval,
    CreateSubmissionApproval,
    SubmissionRefused,
    SubmitApprovedSubmission,
)
from .external_agent_api import Actor
from .store import canonical_digest

router = APIRouter()

Key = Annotated[str, Depends(idempotency_key)]


def submissions(request: Request) -> CampaignSubmissionStore:
    return request.app.state.campaign_submissions


Submissions = Annotated[CampaignSubmissionStore, Depends(submissions)]


def submission_config(request: Request) -> AgentSubmissionConfig:
    config = getattr(request.app.state, "agent_submission", None)
    return config if isinstance(config, AgentSubmissionConfig) else AgentSubmissionConfig()


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/submission-approvals")
def create_submission_approval(
    project_id: str, body: CreateSubmissionApproval, actor: Actor, key: Key, store: Submissions
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.create_submission_approval(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/submission-approvals")
def list_submission_approvals(
    project_id: str,
    actor: Actor,
    store: Submissions,
    status: Literal["pending", "submitted", "cancelled", "expired", "refused_permanent"]
    | None = None,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return store.list_submission_approvals(actor, project_id, status, offset, limit)


@router.get("/submission-approvals/{approval_id}")
def get_submission_approval(approval_id: str, actor: Actor, store: Submissions) -> dict[str, Any]:
    return store.get_submission_approval(actor, approval_id)


@router.post("/submission-approvals/{approval_id}/submit")
def submit_approved(
    approval_id: str,
    body: SubmitApprovedSubmission,
    actor: Actor,
    key: Key,
    store: Submissions,
    request: Request,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    try:
        return result(
            store.submit_approved(
                actor,
                approval_id,
                key,
                canonical_digest(payload),
                payload,
                config=submission_config(request),
            )
        )
    except SubmissionRefused as refusal:
        return JSONResponse(status_code=409, content=refusal.body())


@router.post("/submission-approvals/{approval_id}/cancel")
def cancel_submission_approval(
    approval_id: str, body: CancelSubmissionApproval, actor: Actor, key: Key, store: Submissions
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.cancel_submission_approval(
            actor, approval_id, key, canonical_digest(payload), payload
        )
    )
