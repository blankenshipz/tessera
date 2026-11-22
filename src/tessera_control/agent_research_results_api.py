"""Read-only result summaries for humans and scoped external agents."""

from fastapi import APIRouter, Request

from .external_agent_api import Actor

router = APIRouter()


@router.get("/projects/{project_id}/agent-results/experiments/{experiment_id}")
def experiment_results(project_id: str, experiment_id: str, request: Request, actor: Actor):
    return request.app.state.agent_results.experiment_summary(actor, project_id, experiment_id)


@router.get("/projects/{project_id}/agent-results/studies/{study_id}")
def study_results(project_id: str, study_id: str, request: Request, actor: Actor):
    return request.app.state.agent_results.program_summary(actor, project_id, study_id)
