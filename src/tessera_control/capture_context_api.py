"""Enrolled-agent same-snapshot input/policy resolution, not approval issuance."""

from fastapi import APIRouter, Request, Response

from .capture_context_models import CaptureContextsReply, ResolveCaptureContexts
from .external_agent_api import Actor
from .public_capture_contexts import resolve_separated_capture_contexts
from .store import Forbidden, _connect

router = APIRouter()


@router.post(
    "/projects/{project_id}/agent-capture-contexts/resolve", response_model=CaptureContextsReply
)
def resolve(
    project_id: str,
    body: ResolveCaptureContexts,
    request: Request,
    response: Response,
    actor: Actor,
):
    binding = body.model_dump(mode="json")
    if project_id != binding["project_id"]:
        raise Forbidden("Capture resolution project does not match its binding.")
    connection = _connect(request.app.state.store.database_path)
    try:
        connection.execute("BEGIN")
        result = resolve_separated_capture_contexts(connection, actor, binding)
        response.headers["Cache-Control"] = "no-store"
        return {
            "verified_binding": binding,
            "resolution": result,
            "verification_scope": "current_issuer_context_policy_and_input_digest",
            "authority_effect": "none",
        }
    finally:
        connection.close()
