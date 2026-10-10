"""Normal authenticated worker admission and separate human recovery routes."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .agent_sandbox import AgentSandboxStore
from .agent_sandbox_models import SandboxCleanup, SandboxCommand
from .execution_api import Actor, Key, Worker

router = APIRouter()


def store(request):
    return AgentSandboxStore(
        request.app.state.store.database_path.parent, request.app.state.agent_sandbox_backend
    )


def response(value):
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/worker/agent-sandbox/leases/{lease_id}")
def create(lease_id: str, body: SandboxCommand, request: Request, actor: Worker, key: Key):
    return response(store(request).create(actor, lease_id, key, body.model_dump(mode="json")))


@router.post("/worker/agent-sandboxes/{operation_id}/run")
def run(operation_id: str, body: SandboxCommand, request: Request, actor: Worker, key: Key):
    return response(
        store(request).action(actor, operation_id, key, "run", body.model_dump(mode="json"))
    )


@router.post("/worker/agent-sandboxes/{operation_id}/release")
def release(operation_id: str, body: SandboxCommand, request: Request, actor: Worker, key: Key):
    return response(
        store(request).action(actor, operation_id, key, "release", body.model_dump(mode="json"))
    )


@router.post("/worker/agent-sandboxes/{operation_id}/observe")
def worker_observe(
    operation_id: str, body: SandboxCommand, request: Request, actor: Worker, key: Key
):
    return response(
        store(request).action(actor, operation_id, key, "observe", body.model_dump(mode="json"))
    )


@router.post("/agent-sandboxes/{operation_id}/observe")
def observe(operation_id: str, body: SandboxCommand, request: Request, actor: Actor, key: Key):
    return response(
        store(request).action(actor, operation_id, key, "observe", body.model_dump(mode="json"))
    )


@router.get("/agent-sandboxes/{operation_id}")
def status(operation_id: str, request: Request, actor: Actor):
    return store(request).status(actor, operation_id)


@router.post("/agent-sandboxes/{operation_id}/stop")
def stop(operation_id: str, body: SandboxCommand, request: Request, actor: Actor, key: Key):
    return response(
        store(request).action(actor, operation_id, key, "stop", body.model_dump(mode="json"))
    )


@router.post("/agent-sandboxes/{operation_id}/cleanup")
def cleanup(operation_id: str, body: SandboxCleanup, request: Request, actor: Actor, key: Key):
    return response(store(request).cleanup(actor, operation_id, key, body.model_dump(mode="json")))


@router.post("/agent-sandboxes/{operation_id}/collection-handoff")
def collect(operation_id: str, body: SandboxCommand, request: Request, actor: Actor, key: Key):
    return response(store(request).collect(actor, operation_id, key, body.model_dump(mode="json")))


@router.post("/worker/agent-sandboxes/{operation_id}/collection-handoff")
def worker_collect(
    operation_id: str, body: SandboxCommand, request: Request, actor: Worker, key: Key
):
    return response(store(request).collect(actor, operation_id, key, body.model_dump(mode="json")))
