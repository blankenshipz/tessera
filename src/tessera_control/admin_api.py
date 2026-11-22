"""Authenticated metadata-only administration. No plaintext or provider transport."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from .admin import AdminStore, ProviderTrustStore
from .admin_models import (
    AdminCommand,
    AdminCreate,
    AdminRecord,
    AdminWorkspace,
    Identifier,
    SaladSetupView,
)
from .execution_api import Actor, Key, result
from .external_agent_api import Actor as ExternalActor
from .protected_secrets import ProtectedSecretStore
from .store import Forbidden

# The project routes and the deployment-wide provider trust routes share one router so the
# application includes a single object.
router = APIRouter()
project_router = APIRouter(prefix="/admin/projects/{project_id}")
trust_router = APIRouter(prefix="/admin/provider-trust")


def admin(request: Request) -> AdminStore:
    return request.app.state.admin


Administration = Annotated[AdminStore, Depends(admin)]


@project_router.get("/records/{ident}/enrollment", response_model=AdminRecord)
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


@project_router.get("/records/{ident}/salad-setup", response_model=SaladSetupView)
def salad_setup(project_id: Identifier, ident: Identifier, actor: Actor, store: Administration):
    return store.salad_setup(actor, project_id, ident)


@project_router.get("/records", response_model=AdminWorkspace)
def workspace(
    project_id: Identifier,
    actor: Actor,
    store: Administration,
    offset: int = Query(default=0, ge=0, le=200),
    limit: int = Query(default=50, ge=1, le=100),
):
    return store.workspace(actor, project_id, offset, limit)


@project_router.post("/records", response_model=AdminRecord)
def create(
    project_id: Identifier, body: AdminCreate, actor: Actor, key: Key, store: Administration
):
    return result(store.create(actor, project_id, key, body.model_dump(mode="json")))


@project_router.post("/records/{ident}/commands", response_model=AdminRecord)
def command(
    project_id: Identifier,
    ident: Identifier,
    body: AdminCommand,
    actor: Actor,
    key: Key,
    store: Administration,
):
    return result(store.command(actor, project_id, ident, key, body.model_dump(mode="json")))


@project_router.get("/audit")
def audit(
    project_id: Identifier,
    actor: Actor,
    store: Administration,
    after: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=100),
):
    return store.audit(actor, project_id, after, limit)


def provider_trust_store(request: Request) -> ProviderTrustStore:
    state = request.app.state
    store = getattr(state, "provider_trust", None)
    if store is None:
        store = ProviderTrustStore(
            state.admin.database_path.parent, path=getattr(state, "provider_trust_path", None)
        )
        state.provider_trust = store
    return store


TrustStore = Annotated[ProviderTrustStore, Depends(provider_trust_store)]


@trust_router.get("")
def read_provider_trust(actor: ExternalActor, store: TrustStore):
    """The setting, its digest and load time: a human, or an agent with provider_trust:read."""
    return store.read(actor)


@trust_router.post("/reload")
def reload_provider_trust(actor: Actor, key: Key, store: TrustStore):
    """Human only: re-read the configured file; there is no write route."""
    return result(store.reload(actor, key))


router.include_router(project_router)
router.include_router(trust_router)
