"""Bounded remote append route, with no prompt or tool-content acceptance."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .agent_provenance_models import AppendAgentProvenance
from .external_agent_api import Actor, Key

PROVENANCE_BODY_LIMIT = 16 * 1024


class _BoundedProvenanceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            content = bytearray()
            async for chunk in request.stream():
                if len(chunk) > PROVENANCE_BODY_LIMIT - len(content):
                    return JSONResponse(
                        status_code=413, content={"detail": "Provenance body too large."}
                    )
                content.extend(chunk)
            request._body = bytes(content)
            return await handler(request)

        return bounded


router = APIRouter(route_class=_BoundedProvenanceRoute)


@router.post("/projects/{project_id}/agent-provenance")
def append(project_id: str, body: AppendAgentProvenance, request: Request, actor: Actor, key: Key):
    status, response = request.app.state.agent_provenance.append(
        actor, project_id, key, body.model_dump(mode="json")
    )
    return JSONResponse(status_code=status, content=response)
