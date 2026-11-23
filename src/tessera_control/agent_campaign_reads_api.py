"""Scoped read routes for external agents driving campaigns; authorization is in the store."""

from fastapi import APIRouter, Query, Request

from .external_agent_api import Actor

router = APIRouter()


@router.get("/projects/{project_id}/agent-snapshots")
def snapshots(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
):
    return request.app.state.agent_campaign_reads.list_snapshots(actor, project_id, offset, limit)


@router.get("/projects/{project_id}/agent-snapshots/{snapshot_id}")
def snapshot(project_id: str, snapshot_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.get_snapshot(actor, project_id, snapshot_id)


@router.get("/campaigns/{campaign_id}/outputs")
def outputs(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_outputs(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/logs")
def logs(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_logs(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/stage-status")
def stage_status(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_stage_status(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/grants")
def grants(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_grants(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/retries")
def retries(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_retries(actor, campaign_id)


@router.get("/campaigns/{campaign_id}/holdout")
def holdout(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_holdout(actor, campaign_id)


@router.get("/projects/{project_id}/holdout-claims")
def holdout_claims(
    project_id: str,
    request: Request,
    actor: Actor,
    partition_digest: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
):
    return request.app.state.agent_campaign_reads.holdout_claims(
        actor, project_id, partition_digest
    )


@router.get("/campaigns/{campaign_id}/budget")
def budget(campaign_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.campaign_budget(actor, campaign_id)


@router.get("/projects/{project_id}/agent-standing-status")
def standing_status(project_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.standing_status(actor, project_id)


@router.get("/projects/{project_id}/agent-spend")
def project_spend(project_id: str, request: Request, actor: Actor):
    return request.app.state.agent_campaign_reads.project_spend(actor, project_id)
