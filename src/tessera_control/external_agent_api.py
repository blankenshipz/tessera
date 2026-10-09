"""Thin HTTP command admission for the external-agent audience."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .external_agent_models import (
    CreateAgentArtifactContext,
    CreateAgentContext,
    CreateAgentProposal,
    PromoteAgentProposal,
)

router = APIRouter()


def external_actor(
    actor: Annotated[dict[str, Any], Depends(principal)],
    destination: Annotated[str | None, Header(alias="X-Agent-Destination")] = None,
):
    return {**actor, "agent_destination": destination}


Actor = Annotated[dict[str, Any], Depends(external_actor)]
Key = Annotated[str, Depends(idempotency_key)]


@router.get("/external-agent/status")
def status(request: Request, actor: Actor):
    return request.app.state.external_agents.status(actor)


@router.post("/projects/{project_id}/agent-proposals")
def create_proposal(
    project_id: str, body: CreateAgentProposal, request: Request, actor: Actor, key: Key
):
    status, response = request.app.state.external_agents.create_proposal(
        actor, project_id, key, body.model_dump(mode="json")
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/projects/{project_id}/agent-proposals")
def proposals(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return {
        "proposals": request.app.state.external_agents.list_records(
            actor, project_id, "proposals", offset, limit
        )
    }


@router.get("/agent-proposals/{proposal_id}")
def proposal(proposal_id: str, request: Request, actor: Actor):
    return request.app.state.external_agents.get_proposal(actor, proposal_id)


@router.post("/agent-proposals/{proposal_id}/promote")
def promote(proposal_id: str, body: PromoteAgentProposal, request: Request, actor: Actor, key: Key):
    status, response = request.app.state.external_agents.promote(
        actor, proposal_id, key, body.model_dump(mode="json")
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/projects/{project_id}/agent-contexts")
def create_context(
    project_id: str, body: CreateAgentContext, request: Request, actor: Actor, key: Key
):
    status, response = request.app.state.external_agents.create_context(
        actor, project_id, key, body.model_dump(mode="json")
    )
    return JSONResponse(status_code=status, content=response)


@router.post("/projects/{project_id}/agent-artifact-contexts")
def artifact_context(
    project_id: str, body: CreateAgentArtifactContext, request: Request, actor: Actor, key: Key
):
    status, response = request.app.state.external_agents.create_context(
        actor, project_id, key, body.model_dump(mode="json"), artifact=True
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/projects/{project_id}/agent-contexts")
def contexts(
    project_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return {
        "contexts": request.app.state.external_agents.list_records(
            actor, project_id, "contexts", offset, limit
        )
    }


@router.get("/agent-contexts/{context_id}")
def context(context_id: str, request: Request, actor: Actor):
    return request.app.state.external_agents.get_context(actor, context_id)
