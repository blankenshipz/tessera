"""Campaign submission, human approval, cancel and read routes (ADR 0038).

Submission grants nothing: approval is a human-only store check, and cancel is
reduce-only. Authorization lives in CampaignStore, not in these routes.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key
from .campaign_gate_models import RecordGateDecision
from .campaign_models import ApproveCampaign, CancelCampaign, SubmitCampaign, approve_payload
from .campaign_standing import CreateStandingApproval, RevokeStandingApproval
from .campaign_steering import (
    CancelCampaignStage,
    LimitCampaignConcurrency,
    PauseCampaign,
    ReduceCampaignBudget,
    ResumeCampaign,
    RevokeCampaignRelease,
    SteerCampaignPlacement,
)
from .campaign_store import CampaignStore
from .external_agent_api import Actor as external_actor_dep
from .store import canonical_digest

router = APIRouter()


Actor = external_actor_dep
Key = Annotated[str, Depends(idempotency_key)]


def campaigns(request: Request) -> CampaignStore:
    return request.app.state.campaigns


Campaigns = Annotated[CampaignStore, Depends(campaigns)]


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/campaigns")
def submit_campaign(
    project_id: str, body: SubmitCampaign, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.submit_campaign(actor, project_id, key, canonical_digest(payload), payload))


@router.get("/projects/{project_id}/campaigns")
def list_campaigns(
    project_id: str,
    actor: Actor,
    store: Campaigns,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return store.list_campaigns(actor, project_id, offset, limit)


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, actor: Actor, store: Campaigns) -> dict[str, Any]:
    return store.get_campaign(actor, campaign_id)


@router.post("/campaigns/{campaign_id}/approve")
def approve_campaign(
    campaign_id: str, body: ApproveCampaign, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    payload = approve_payload(body)
    return result(
        store.approve_campaign(actor, campaign_id, key, canonical_digest(payload), payload)
    )


@router.post("/campaigns/{campaign_id}/cancel")
def cancel_campaign(
    campaign_id: str, body: CancelCampaign, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.cancel_campaign(actor, campaign_id, key, canonical_digest(payload), payload)
    )


@router.post("/projects/{project_id}/campaign-specs/validate")
def validate_campaign_spec(
    project_id: str, body: SubmitCampaign, actor: Actor, store: Campaigns
) -> dict[str, Any]:
    """Dry-run of submit (ADR 0055): the same checks, nothing stored, nothing granted."""
    return store.validate_campaign_spec(actor, project_id, body.model_dump(mode="json"))


@router.post("/campaigns/{campaign_id}/budget-reduction")
def reduce_campaign_budget(
    campaign_id: str, body: ReduceCampaignBudget, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    """Reduce-only (ADR 0055): lowers an approved cap, never raises it."""
    payload = body.model_dump(mode="json")
    return result(
        store.reduce_campaign_budget(actor, campaign_id, key, canonical_digest(payload), payload)
    )


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(
    campaign_id: str, body: PauseCampaign, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    """ADR 0061: stops new stage reservations; running attempts are not touched."""
    payload = body.model_dump(mode="json")
    return result(store.pause_campaign(actor, campaign_id, key, canonical_digest(payload), payload))


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(
    campaign_id: str, body: ResumeCampaign, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.resume_campaign(actor, campaign_id, key, canonical_digest(payload), payload)
    )


@router.post("/campaigns/{campaign_id}/concurrency-limit")
def limit_campaign_concurrency(
    campaign_id: str, body: LimitCampaignConcurrency, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    """ADR 0061: lower-only cap on stages with an unfinished attempt."""
    payload = body.model_dump(mode="json")
    return result(
        store.limit_campaign_concurrency(
            actor, campaign_id, key, canonical_digest(payload), payload
        )
    )


@router.post("/campaigns/{campaign_id}/stages/{stage_key}/cancel")
def cancel_campaign_stage(
    campaign_id: str,
    stage_key: str,
    body: CancelCampaignStage,
    actor: Actor,
    key: Key,
    store: Campaigns,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.cancel_campaign_stage(
            actor, campaign_id, stage_key, key, canonical_digest(payload), payload
        )
    )


@router.post("/campaigns/{campaign_id}/placement-steering")
def steer_campaign_placement(
    campaign_id: str, body: SteerCampaignPlacement, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    """ADR 0060: pin or exclude a stage's approved provider candidates (latest call replaces)."""
    payload = body.model_dump(mode="json")
    return result(
        store.steer_campaign_placement(actor, campaign_id, key, canonical_digest(payload), payload)
    )


@router.post("/campaigns/{campaign_id}/releases/{release_id}/revoke")
def revoke_campaign_release(
    campaign_id: str,
    release_id: str,
    body: RevokeCampaignRelease,
    actor: Actor,
    key: Key,
    store: Campaigns,
) -> JSONResponse:
    """Reduce-only (ADR 0055): withdraws a data release a finished campaign used."""
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_campaign_release(
            actor, campaign_id, release_id, key, canonical_digest(payload), payload
        )
    )


@router.post("/campaigns/{campaign_id}/gate-decision")
def record_gate_decision(
    campaign_id: str, body: RecordGateDecision, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    """Human-only (ADR 0045): a gate go/no_go on a finished campaign."""
    payload = body.model_dump(mode="json")
    return result(
        store.record_gate_decision(actor, campaign_id, key, canonical_digest(payload), payload)
    )


@router.get("/campaigns/{campaign_id}/gate-decisions")
def list_gate_decisions(campaign_id: str, actor: Actor, store: Campaigns) -> dict[str, Any]:
    return store.list_gate_decisions(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/receipt")
def get_campaign_receipt(campaign_id: str, actor: Actor, store: Campaigns) -> dict[str, Any]:
    return store.get_campaign_receipt(actor, campaign_id)


# Standing approvals (ADR 0042): human-only routes.
@router.post("/projects/{project_id}/standing-approvals")
def create_standing_approval(
    project_id: str, body: CreateStandingApproval, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.create_standing_approval(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/standing-approvals")
def list_standing_approvals(
    project_id: str,
    actor: Actor,
    store: Campaigns,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return store.list_standing_approvals(actor, project_id, offset, limit)


@router.get("/projects/{project_id}/standing-approvals/{standing_id}")
def get_standing_approval(
    project_id: str, standing_id: str, actor: Actor, store: Campaigns
) -> dict[str, Any]:
    return store.get_standing_approval(actor, project_id, standing_id)


@router.post("/projects/{project_id}/standing-approvals/{standing_id}/revoke")
def revoke_standing_approval(
    project_id: str,
    standing_id: str,
    body: RevokeStandingApproval,
    actor: Actor,
    key: Key,
    store: Campaigns,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_standing_approval(
            actor, project_id, standing_id, key, canonical_digest(payload), payload
        )
    )
