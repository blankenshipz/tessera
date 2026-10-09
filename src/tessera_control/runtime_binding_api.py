"""Real authenticated runtime binding routes, no request-selected successful adapter."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import principal, worker_principal
from .external_agent_api import Key
from .runtime_binding_models import (
    AllocateRuntimeOperation,
    ClaimRuntimeOperation,
    IssueRuntimeEnrollment,
    IssueRuntimeFinancialAssertion,
    IssueRuntimePolicy,
    ResolveRuntimeBinding,
    RevokeRuntimeRecord,
    SettleRuntimeOperation,
)

router = APIRouter()
HumanActor = Annotated[dict, Depends(principal)]


def response(result):
    status, body = result
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/runtime-policies")
def issue_policy(
    project_id: str, body: IssueRuntimePolicy, request: Request, actor: HumanActor, key: Key
):
    return response(
        request.app.state.runtime_bindings.issue_policy(
            actor, project_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/projects/{project_id}/runtime-enrollments")
def issue_enrollment(
    project_id: str, body: IssueRuntimeEnrollment, request: Request, actor: HumanActor, key: Key
):
    return response(
        request.app.state.runtime_bindings.issue_enrollment(
            actor, project_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/projects/{project_id}/runtime-financial-assertions")
def issue_financial(
    project_id: str,
    body: IssueRuntimeFinancialAssertion,
    request: Request,
    actor: HumanActor,
    key: Key,
):
    return response(
        request.app.state.runtime_bindings.issue_financial(
            actor, project_id, key, body.model_dump(mode="json")
        )
    )


@router.post("/runtime-policies/{ref}/revoke")
def revoke_policy(
    ref: str, body: RevokeRuntimeRecord, request: Request, actor: HumanActor, key: Key
):
    return response(
        request.app.state.runtime_bindings.revoke(
            actor, "policy", ref, key, body.model_dump(mode="json")
        )
    )


@router.post("/runtime-enrollments/{ref}/revoke")
def revoke_enrollment(
    ref: str, body: RevokeRuntimeRecord, request: Request, actor: HumanActor, key: Key
):
    return response(
        request.app.state.runtime_bindings.revoke(
            actor, "enrollment", ref, key, body.model_dump(mode="json")
        )
    )


@router.post("/runtime-financial-assertions/{ref}/revoke")
def revoke_financial(
    ref: str, body: RevokeRuntimeRecord, request: Request, actor: HumanActor, key: Key
):
    return response(
        request.app.state.runtime_bindings.revoke(
            actor, "financial", ref, key, body.model_dump(mode="json")
        )
    )


def publisher_routes(prefix, dependency, audience):
    Actor = Annotated[dict, Depends(dependency)]

    @router.post(prefix + "/runtime-bindings/resolve")
    def resolve(body: ResolveRuntimeBinding, request: Request, actor: Actor, key: Key):
        return response(
            request.app.state.runtime_bindings.resolve_binding(
                actor, key, body.model_dump(mode="json"), audience
            )
        )

    @router.post(prefix + "/runtime-operations")
    def allocate(body: AllocateRuntimeOperation, request: Request, actor: Actor, key: Key):
        return response(
            request.app.state.runtime_bindings.allocate_operation(
                actor, key, body.model_dump(mode="json"), audience
            )
        )

    @router.post(prefix + "/runtime-operations/{ref}/claim")
    def claim(ref: str, body: ClaimRuntimeOperation, request: Request, actor: Actor, key: Key):
        return response(
            request.app.state.runtime_bindings.claim_operation(
                actor, ref, key, body.model_dump(mode="json"), audience
            )
        )

    @router.post(prefix + "/runtime-operations/{ref}/settle")
    def settle(ref: str, body: SettleRuntimeOperation, request: Request, actor: Actor, key: Key):
        return response(
            request.app.state.runtime_bindings.settle_operation(
                actor, ref, key, body.model_dump(mode="json"), audience
            )
        )

    @router.get(prefix + "/runtime-operations/{ref}")
    def operation(ref: str, request: Request, actor: Actor):
        return request.app.state.runtime_bindings.get_operation(actor, ref, audience)

    @router.get(prefix + "/runtime-financial-assertions/{ref}")
    def financial(ref: str, request: Request, actor: Actor):
        return request.app.state.runtime_bindings.resolve_financial(actor, ref, audience)


publisher_routes("", principal, "control")
publisher_routes("/worker", worker_principal, "worker")
