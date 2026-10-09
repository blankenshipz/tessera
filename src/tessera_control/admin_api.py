"""Authenticated metadata-only administration. No plaintext or provider transport."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from .admin import AdminStore
from .admin_models import AdminCommand, AdminCreate, AdminRecord, AdminWorkspace, Identifier
from .execution_api import Actor, Key, result
from .protected_secrets import ProtectedSecretStore
from .store import Forbidden

router = APIRouter(prefix="/admin/projects/{project_id}")


def admin(request: Request) -> AdminStore:
    return request.app.state.admin


Administration = Annotated[AdminStore, Depends(admin)]


@router.get("/records/{ident}/enrollment", response_model=AdminRecord)
def enrollment(
    project_id: Identifier, ident: Identifier, actor: Actor, store: Administration, request: Request
):
    """Metadata handoff only; consuming it requires the native helper's same database.

    An endpoint being loopback is NOT proof of co-location (SSH tunnels exist).
    No HTTP value import/read route exists, including on loopback.
    """
    if request.app.state.deployment.profile != "local-loopback-v1":
        raise Forbidden(
            "Remote enrollment requires a server-side broker; desktop custody is unavailable."
        )
    return ProtectedSecretStore(store.database_path.parent).inspect(actor, project_id, ident)


@router.get("/records", response_model=AdminWorkspace)
def workspace(
    project_id: Identifier,
    actor: Actor,
    store: Administration,
    offset: int = Query(default=0, ge=0, le=200),
    limit: int = Query(default=50, ge=1, le=100),
):
    return store.workspace(actor, project_id, offset, limit)


@router.post("/records", response_model=AdminRecord)
def create(
    project_id: Identifier, body: AdminCreate, actor: Actor, key: Key, store: Administration
):
    return result(store.create(actor, project_id, key, body.model_dump(mode="json")))


@router.post("/records/{ident}/commands", response_model=AdminRecord)
def command(
    project_id: Identifier,
    ident: Identifier,
    body: AdminCommand,
    actor: Actor,
    key: Key,
    store: Administration,
):
    return result(store.command(actor, project_id, ident, key, body.model_dump(mode="json")))


@router.get("/audit")
def audit(
    project_id: Identifier,
    actor: Actor,
    store: Administration,
    after: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=100),
):
    return store.audit(actor, project_id, after, limit)
