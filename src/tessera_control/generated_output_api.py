"""Separately authenticated human admission and actual-agent registration."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .external_agent_api import Actor, Key
from .generated_output_models import (
    OutputAdmission,
    OutputDisclosure,
    OutputFence,
    RegisterGeneratedOutput,
)
from .generated_outputs import GeneratedOutputStore

router = APIRouter()


def store(request):
    return GeneratedOutputStore(request.app.state.store.database_path.parent)


def response(result):
    status, body = result
    return JSONResponse(status_code=status, content=body)


@router.post("/projects/{project_id}/generated-output-grants")
def issue(project_id: str, body: OutputAdmission, request: Request, actor: Actor, key: Key):
    return response(store(request).issue(actor, project_id, key, body.model_dump(mode="json")))


@router.post("/projects/{project_id}/generated-outputs")
def register(
    project_id: str, body: RegisterGeneratedOutput, request: Request, actor: Actor, key: Key
):
    return response(store(request).register(actor, project_id, key, body.model_dump(mode="json")))


@router.post("/generated-outputs/{output_id}/disclosures")
def disclose(output_id: str, body: OutputDisclosure, request: Request, actor: Actor, key: Key):
    return response(store(request).disclose(actor, output_id, key, body.model_dump(mode="json")))


@router.get("/generated-outputs/{output_id}")
def read(output_id: str, request: Request, actor: Actor, disclosure_id: str | None = None):
    return store(request).read(actor, output_id, disclosure_id)


@router.get("/generated-outputs/{output_id}/metadata")
def metadata(output_id: str, request: Request, actor: Actor, disclosure_id: str | None = None):
    value = store(request).read(actor, output_id, disclosure_id)
    value.pop("content_base64")
    return value


@router.post("/generated-output-grants/{grant_id}/revoke")
def revoke(grant_id: str, body: OutputFence, request: Request, actor: Actor, key: Key):
    return response(store(request).revoke(actor, grant_id, key, body.model_dump(mode="json")))


@router.post("/generated-output-disclosures/{disclosure_id}/revoke")
def withdraw(disclosure_id: str, body: OutputFence, request: Request, actor: Actor, key: Key):
    return response(
        store(request).revoke(
            actor, disclosure_id, key, body.model_dump(mode="json"), disclosure=True
        )
    )
