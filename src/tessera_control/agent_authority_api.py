"""Ordinary HTTP routes for bounded authority, never owner-token proxying."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .agent_authority_models import (
    ConsumeAgentAuthority,
    DelegateAgentAuthority,
    IssueAgentAuthority,
    RequestAgentAuthority,
    RevokeAgentAuthority,
)
from .agent_research_models import AgentObservation, AgentReviewRequest, ResearchProposal
from .external_agent_api import Actor, Key
from .external_agent_models import PromoteAgentProposal

router = APIRouter()


def _response(value):
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/agent-authority-requests")
def request_authority(
    project_id: str, body: RequestAgentAuthority, request: Request, actor: Actor, key: Key
):
    return _response(
        request.app.state.agent_authority.request_authority(
            actor, project_id, key, body.model_dump(mode="json")
        )
    )


@router.get("/projects/{project_id}/agent-authority-requests")
def requests(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return {
        "requests": request.app.state.agent_authority.list_requests(
            actor, project_id, offset, limit
        )
    }


@router.post("/projects/{project_id}/agent-authority-grants")
def issue(project_id: str, body: IssueAgentAuthority, request: Request, actor: Actor, key: Key):
    return _response(
        request.app.state.agent_authority.issue_grant(
            actor, project_id, key, body.model_dump(mode="json")
        )
    )


@router.get("/agent-authority-grants/{grant_id}")
def grant(grant_id: str, request: Request, actor: Actor):
    return request.app.state.agent_authority.get_grant(actor, grant_id)


@router.post("/agent-authority-grants/{grant_id}/revoke")
def revoke(grant_id: str, body: RevokeAgentAuthority, request: Request, actor: Actor, key: Key):
    return _response(
        request.app.state.agent_authority.revoke_grant(
            actor, grant_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/agent-authority-grants/{grant_id}/delegate")
def delegate(grant_id: str, body: DelegateAgentAuthority, request: Request, actor: Actor, key: Key):
    return _response(
        request.app.state.agent_authority.delegate_grant(
            actor, grant_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/agent-authority-grants/{grant_id}/consume")
def consume(grant_id: str, body: ConsumeAgentAuthority, request: Request, actor: Actor, key: Key):
    return _response(
        request.app.state.agent_authority.consume(
            actor, grant_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/projects/{project_id}/agent-workflow-proposals")
def research_proposal(
    project_id: str, body: ResearchProposal, request: Request, actor: Actor, key: Key
):
    return _response(
        request.app.state.agent_research.append(
            actor, project_id, "proposal", key, body.model_dump(mode="json")
        )
    )


@router.get("/projects/{project_id}/agent-workflow-proposals")
def research_proposals(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return {
        "proposals": request.app.state.agent_research.list_proposals(
            actor, project_id, offset, limit
        )
    }


@router.post("/agent-research-proposals/{proposal_id}/promote")
def promote_research(
    proposal_id: str, body: PromoteAgentProposal, request: Request, actor: Actor, key: Key
):
    return _response(
        request.app.state.agent_research.promote(
            actor, proposal_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/projects/{project_id}/agent-observations")
def observation(project_id: str, body: AgentObservation, request: Request, actor: Actor, key: Key):
    return _response(
        request.app.state.agent_research.append(
            actor, project_id, "observation", key, body.model_dump(mode="json")
        )
    )


@router.post("/projects/{project_id}/agent-review-requests")
def review_request(
    project_id: str, body: AgentReviewRequest, request: Request, actor: Actor, key: Key
):
    return _response(
        request.app.state.agent_research.append(
            actor, project_id, "review-request", key, body.model_dump(mode="json")
        )
    )


@router.get("/projects/{project_id}/agent-notebook")
@router.get("/projects/{project_id}/agent-notebook/export")
def agent_notebook(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    after_sequence: int = Query(0, ge=0),
    after_event_cursor: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    max_bytes: int = Query(2097152, ge=1024, le=2097152),
):
    # Offset is a provenance sequence cursor alias, not an unrestricted database offset.
    after_sequence = max(offset, after_sequence)
    return request.app.state.agent_research.notebook(
        actor,
        project_id,
        after_sequence=after_sequence,
        after_event_cursor=after_event_cursor,
        limit=limit,
        max_bytes=max_bytes,
    )
