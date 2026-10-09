"""Control planning/review and worker-only synthetic operation bindings."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from .autonomy import AutonomyStore
from .autonomy_models import ActionUpdate, AutonomyPlan, SessionCommand, StartAction
from .execution_api import Actor, Key, Worker, result

router = APIRouter()


def autonomy(request: Request) -> AutonomyStore:
    return AutonomyStore(request.app.state.store.database_path.parent)


Sessions = Annotated[AutonomyStore, Depends(autonomy)]


@router.post("/tasks/{task_id}/autonomy-sessions")
def create_session(task_id: str, body: AutonomyPlan, actor: Actor, key: Key, store: Sessions):
    return result(store.create(actor, task_id, key, body.model_dump(mode="json")))


@router.get("/tasks/{task_id}/autonomy-sessions")
def list_sessions(task_id: str, actor: Actor, store: Sessions):
    return store.list(actor, task_id)


@router.get("/autonomy-sessions/{ident}")
def get_session(ident: str, actor: Actor, store: Sessions):
    return store.get(actor, ident)


@router.post("/autonomy-sessions/{ident}/commands")
def command_session(ident: str, body: SessionCommand, actor: Actor, key: Key, store: Sessions):
    return result(store.command(actor, ident, key, body.model_dump(mode="json")))


@router.post("/worker/autonomy-sessions/{ident}/actions")
def start_action(ident: str, body: StartAction, actor: Worker, key: Key, store: Sessions):
    return result(store.start(actor, ident, key, body.model_dump(mode="json")))


@router.post("/worker/autonomy-sessions/{ident}/heartbeat")
def heartbeat(ident: str, body: ActionUpdate, actor: Worker, key: Key, store: Sessions):
    return result(store.update_action(actor, ident, key, body.model_dump(mode="json")))


@router.post("/worker/autonomy-sessions/{ident}/receipt")
def receipt(ident: str, body: ActionUpdate, actor: Worker, key: Key, store: Sessions):
    return result(
        store.update_action(actor, ident, key, body.model_dump(mode="json"), receipt=True)
    )
