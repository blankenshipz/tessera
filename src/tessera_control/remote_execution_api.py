"""Control-only proposal/review/stop/status; no provider-effect or policy write API."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from .execution_api import Actor, Key, result
from .remote_execution import RemoteExecutionStore
from .remote_execution_models import RemoteCommand, RemoteProposal

router = APIRouter()


def remote_execution(request: Request) -> RemoteExecutionStore:
    return request.app.state.remote_execution


Routes = Annotated[RemoteExecutionStore, Depends(remote_execution)]


@router.post("/tasks/{task_id}/remote-executions")
def propose(task_id: str, body: RemoteProposal, actor: Actor, key: Key, store: Routes):
    return result(store.propose(actor, task_id, key, body.model_dump(mode="json")))


@router.get("/tasks/{task_id}/remote-executions")
def routes(task_id: str, actor: Actor, store: Routes):
    return store.list(actor, task_id)


@router.get("/remote-executions/{ident}")
def inspect(ident: str, actor: Actor, store: Routes):
    return store.get(actor, ident)


@router.post("/remote-executions/{ident}/commands")
def command(ident: str, body: RemoteCommand, actor: Actor, key: Key, store: Routes):
    return result(store.command(actor, ident, key, body.model_dump(mode="json")))
