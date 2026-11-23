"""Campaign submission, human approval, cancel and read routes (ADR 0038).

Submission grants nothing: approval is a human-only store check, and cancel is
reduce-only. Authorization lives in CampaignStore, not in these routes.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import idempotency_key
from .campaign_gate_models import RecordGateDecision
from .campaign_models import ApproveCampaign, CancelCampaign, SubmitCampaign, approve_payload
from .campaign_rerun import RerunCampaign
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


@router.post("/campaigns/{campaign_id}/rerun")
def rerun_campaign(
    campaign_id: str, body: RerunCampaign, actor: Actor, key: Key, store: Campaigns
) -> JSONResponse:
    """ADR 0069: submit a new campaign that re-runs a finished one's failed stages (or its
    identical spec), reusing succeeded outputs by digest. It grants nothing; a person approves."""
    payload = body.model_dump(mode="json", exclude_none=True)
    return result(store.rerun_campaign(actor, campaign_id, key, canonical_digest(payload), payload))


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


@router.get("/campaigns/{campaign_id}/report")
def get_campaign_report(
    campaign_id: str, actor: Actor, store: Campaigns, detail: bool = False
) -> dict[str, Any]:
    """A plain-words report of the campaign (no internal ids unless detail=true)."""
    return store.get_campaign_report(actor, campaign_id, detail=detail)


@router.get("/projects/{project_id}/campaign-report")
def get_campaign_window_report(
    project_id: str,
    actor: Actor,
    store: Campaigns,
    since: str,
    until: str,
    detail: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """A plain-words report of the project's campaigns created in [since, until)."""
    return store.get_campaign_window_report(
        actor, project_id, since, until, detail=detail, limit=limit
    )


@router.get("/campaigns/{campaign_id}/receipt")
def get_campaign_receipt(campaign_id: str, actor: Actor, store: Campaigns) -> dict[str, Any]:
    return store.get_campaign_receipt(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/comparison")
def get_campaign_comparison(
    campaign_id: str,
    actor: Actor,
    store: Campaigns,
    baseline: Annotated[str, Query(min_length=1, max_length=200)],
    intervention: Annotated[list[str], Query(max_length=8)],
    metric: Annotated[list[str], Query(max_length=16)] = [],  # noqa: B006
    evaluation_input: Annotated[list[str], Query(max_length=8)] = [],  # noqa: B006
    validation_metric: Annotated[list[str], Query(max_length=16)] = [],  # noqa: B006
    test_metric: Annotated[list[str], Query(max_length=16)] = [],  # noqa: B006
    validation_input: Annotated[list[str], Query(max_length=8)] = [],  # noqa: B006
    test_input: Annotated[list[str], Query(max_length=8)] = [],  # noqa: B006
    select_by: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    select_direction: Annotated[Literal["max", "min"] | None, Query()] = None,
) -> dict[str, Any]:
    """Recorded-number deltas between a baseline stage and intervention stages (read-only).

    With select_by the intervention arm is chosen on validation metrics only; test metrics
    are reported for the baseline and the selected arm alone.
    """
    if select_by is None and not (metric and evaluation_input):
        missing = "metric" if not metric else "evaluation_input"
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", missing), "msg": "Field required", "input": None}]
        )
    return store.get_campaign_comparison(
        actor,
        campaign_id,
        baseline=baseline,
        interventions=intervention,
        metrics=metric,
        evaluation_inputs=evaluation_input,
        validation_metrics=validation_metric,
        test_metrics=test_metric,
        validation_inputs=validation_input,
        test_inputs=test_input,
        select_by=select_by,
        select_direction=select_direction,
    )


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
