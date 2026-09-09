"""Human credential-access lease records and worker handle route."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal, worker_principal
from .credential_lease_models import (
    CreateCredentialAccessLease,
    RenewCredentialAccessLease,
    RevokeCredentialAccessLease,
)
from .credential_leases import CredentialAccessLeaseStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def leases(request: Request) -> CredentialAccessLeaseStore:
    return CredentialAccessLeaseStore(request.app.state.store.database_path.parent)


Store = Annotated[CredentialAccessLeaseStore, Depends(leases)]


def result(value: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.post("/execution-grants/{grant_id}/credential-access-leases")
def create_lease(
    grant_id: str, body: CreateCredentialAccessLease, actor: Actor, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.create(actor, grant_id, key, canonical_digest(payload), payload))


@router.get("/projects/{project_id}/credential-access-leases")
def list_leases(
    project_id: str,
    actor: Actor,
    store: Store,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"credential_access_leases": store.list(actor, project_id, offset, limit)}


@router.post("/credential-access-leases/{lease_id}/renew")
def renew_lease(
    lease_id: str, body: RenewCredentialAccessLease, actor: Actor, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.renew(actor, lease_id, key, canonical_digest(payload), payload))


@router.post("/credential-access-leases/{lease_id}/revoke")
def revoke_lease(
    lease_id: str,
    body: RevokeCredentialAccessLease,
    actor: Actor,
    key: Key,
    store: Store,
    request: Request,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    response = store.revoke(actor, lease_id, key, canonical_digest(payload), payload)
    request.app.state.fake_secret_backend.revoke(lease_id)
    return result(response)


@router.post("/worker/credential-access-leases/{lease_id}/access")
def access_lease(lease_id: str, worker: Worker, store: Store, request: Request) -> dict[str, str]:
    return store.issue_handle(worker, lease_id, request.app.state.fake_secret_backend)
