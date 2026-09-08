"""Typed control admission and separately authenticated worker authority routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal, worker_principal
from .execution import ExecutionStore
from .execution_models import (
    AcquireExecutionLease,
    ConfigureProjectExecution,
    IssueExecutionGrant,
    PrepareExecutionPackage,
    ReconcileExecutionGrant,
    RenewExecutionLease,
    ReportExecutionObservation,
    RevokeExecutionGrant,
)
from .input_delivery import DeliverInputs, InputDeliveryStore
from .runtime_packages import RuntimePackageStore
from .snapshot_releases import ReleaseSnapshot, RevokeSnapshotRelease, SnapshotReleaseStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def execution(request: Request) -> ExecutionStore:
    return request.app.state.execution


def input_delivery(request: Request) -> InputDeliveryStore:
    return InputDeliveryStore(request.app.state.execution.database_path.parent)


def releases(request: Request) -> SnapshotReleaseStore:
    return request.app.state.snapshot_releases


Authority = Annotated[ExecutionStore, Depends(execution)]
Delivery = Annotated[InputDeliveryStore, Depends(input_delivery)]
Releases = Annotated[SnapshotReleaseStore, Depends(releases)]


def result(value) -> JSONResponse:
    status, body = value
    return JSONResponse(status_code=status, content=body)


@router.put("/projects/{project_id}/execution-limits")
def configure_limits(
    project_id: str, body: ConfigureProjectExecution, actor: Actor, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.configure_project_execution(
            actor, project_id, key, canonical_digest(payload), payload
        )
    )


@router.get("/projects/{project_id}/execution-limits")
def get_limits(project_id: str, actor: Actor, store: Authority) -> dict[str, Any]:
    return store.get_project_execution(actor, project_id)


@router.post("/projects/{project_id}/execution-grants")
def issue_grant(
    project_id: str, body: IssueExecutionGrant, actor: Actor, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.issue_execution_grant(actor, project_id, key, canonical_digest(payload), payload)
    )


@router.get("/projects/{project_id}/execution-grants")
def list_grants(
    project_id: str,
    actor: Actor,
    store: Authority,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"grants": store.list_execution_grants(actor, project_id, offset, limit)}


@router.get("/execution-grants/{grant_id}")
def get_grant(grant_id: str, actor: Actor, store: Authority) -> dict[str, Any]:
    return store.get_execution_grant(actor, grant_id)


@router.post("/execution-grants/{grant_id}/revoke")
def revoke_grant(
    grant_id: str, body: RevokeExecutionGrant, actor: Actor, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_execution_grant(
            actor, grant_id, key, canonical_digest(payload), body.expected_revision
        )
    )


@router.post("/execution-grants/{grant_id}/reconcile")
def reconcile_grant(
    grant_id: str, body: ReconcileExecutionGrant, actor: Actor, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.reconcile_execution_grant(actor, grant_id, key, canonical_digest(payload), payload)
    )


@router.post("/worker/leases")
def acquire_lease(
    body: AcquireExecutionLease, worker: Worker, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.acquire_execution_lease(worker, body.grant_id, key, canonical_digest(payload))
    )


@router.post("/worker/leases/renew")
def renew_lease(
    body: RenewExecutionLease, worker: Worker, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.renew_execution_lease(
            worker, body.lease_id, key, canonical_digest(payload), body.expected_epoch
        )
    )


@router.post("/worker/observations")
def report_observation(
    body: ReportExecutionObservation, worker: Worker, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.report_execution_observation(
            worker, body.lease_id, key, canonical_digest(payload), payload
        )
    )


@router.post("/worker/leases/{lease_id}/inputs")
def deliver_inputs(
    lease_id: str, body: DeliverInputs, worker: Worker, key: Key, store: Delivery
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.deliver_inputs(worker, lease_id, key, canonical_digest(payload), body.expected_epoch)
    )


@router.post("/worker/leases/{lease_id}/package")
def execution_package(
    lease_id: str, body: PrepareExecutionPackage, worker: Worker, key: Key, store: Authority
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        RuntimePackageStore(store.database_path.parent).execution_package(
            worker,
            lease_id,
            key,
            canonical_digest(payload),
            body.expected_epoch,
            body.persistent_output_collection is True,
            body.persistent_output_storage,
        )
    )


@router.post("/snapshots/{snapshot_id}/releases")
def release_snapshot(
    snapshot_id: str, body: ReleaseSnapshot, actor: Actor, key: Key, store: Releases
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.release_snapshot(actor, snapshot_id, key, canonical_digest(payload), payload)
    )


@router.get("/snapshots/{snapshot_id}/releases")
def list_releases(
    snapshot_id: str,
    actor: Actor,
    store: Releases,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"releases": store.list_snapshot_releases(actor, snapshot_id, offset, limit)}


@router.post("/snapshot-releases/{release_id}/revoke")
def revoke_release(
    release_id: str, body: RevokeSnapshotRelease, actor: Actor, key: Key, store: Releases
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.revoke_snapshot_release(
            actor, release_id, key, canonical_digest(payload), body.expected_revision
        )
    )
