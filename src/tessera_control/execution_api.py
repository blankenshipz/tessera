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
    ReportExternalOperationReceipt,
    RevokeExecutionGrant,
)
from .input_delivery import DeliverInputs, InputDeliveryStore
from .result_acceptances import ResultAcceptanceRequest, ResultAcceptanceStore
from .result_checkpoints import CommitExecutionResultCheckpoint, ResultCheckpointStore
from .runtime_packages import RuntimePackageStore
from .snapshot_releases import ReleaseSnapshot, RevokeSnapshotRelease, SnapshotReleaseStore
from .store import canonical_digest
from .worker_models import RegisterWorker, WorkerHeartbeat, WorkerLifecycle
from .workers import WorkerRegistryStore

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def execution(request: Request) -> ExecutionStore:
    return request.app.state.execution


def workers(request: Request) -> WorkerRegistryStore:
    return WorkerRegistryStore(request.app.state.execution.database_path.parent)


def input_delivery(request: Request) -> InputDeliveryStore:
    return InputDeliveryStore(request.app.state.execution.database_path.parent)


def releases(request: Request) -> SnapshotReleaseStore:
    return request.app.state.snapshot_releases


Authority = Annotated[ExecutionStore, Depends(execution)]
Workers = Annotated[WorkerRegistryStore, Depends(workers)]
Delivery = Annotated[InputDeliveryStore, Depends(input_delivery)]
Releases = Annotated[SnapshotReleaseStore, Depends(releases)]


def result_checkpoints(request: Request) -> ResultCheckpointStore:
    return ResultCheckpointStore(request.app.state.execution.database_path.parent)


Checkpoints = Annotated[ResultCheckpointStore, Depends(result_checkpoints)]


def result_acceptances(request: Request) -> ResultAcceptanceStore:
    return ResultAcceptanceStore(request.app.state.execution.database_path.parent)


Acceptances = Annotated[ResultAcceptanceStore, Depends(result_acceptances)]


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


@router.post("/execution-grants/{grant_id}/result-checkpoint")
def commit_result_checkpoint(
    grant_id: str,
    body: CommitExecutionResultCheckpoint,
    actor: Actor,
    key: Key,
    store: Checkpoints,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.commit(actor, grant_id, key, canonical_digest(payload), payload))


@router.get("/projects/{project_id}/result-checkpoints")
def list_result_checkpoints(
    project_id: str,
    actor: Actor,
    store: Checkpoints,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"result_checkpoints": store.list_checkpoints(actor, project_id, offset, limit)}


@router.post("/result-checkpoints/{checkpoint_id}/acceptances")
def record_result_acceptance(
    checkpoint_id: str,
    body: ResultAcceptanceRequest,
    actor: Actor,
    key: Key,
    store: Acceptances,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.record(actor, checkpoint_id, key, canonical_digest(payload), payload))


@router.get("/result-checkpoints/{checkpoint_id}/acceptances")
def list_result_acceptances(
    checkpoint_id: str,
    actor: Actor,
    store: Acceptances,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return {"result_acceptances": store.list_records(actor, checkpoint_id, offset, limit)}


@router.post("/projects/{project_id}/workers")
def register_worker(
    project_id: str, body: RegisterWorker, actor: Actor, key: Key, store: Workers
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.register_worker(actor, project_id, key, canonical_digest(payload), payload))


@router.post("/workers/{worker_id}/drain")
@router.post("/workers/{worker_id}/quarantine")
@router.post("/workers/{worker_id}/retire")
def lifecycle_worker(
    worker_id: str, body: WorkerLifecycle, actor: Actor, key: Key, store: Workers, request: Request
) -> JSONResponse:
    action = request.url.path.rsplit("/", 1)[1]
    payload = body.model_dump(mode="json")
    return result(
        store.lifecycle_worker(actor, worker_id, action, key, canonical_digest(payload), payload)
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


@router.post("/worker/external-operations/{operation_id}/receipt")
def report_provider_receipt(
    operation_id: str,
    body: ReportExternalOperationReceipt,
    worker: Worker,
    key: Key,
    store: Authority,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(
        store.report_external_operation_receipt(
            worker, operation_id, key, canonical_digest(payload), payload
        )
    )


@router.post("/worker/heartbeat")
def heartbeat(body: WorkerHeartbeat, worker: Worker, key: Key, store: Workers) -> JSONResponse:
    payload = body.model_dump(mode="json")
    return result(store.heartbeat(worker, key, canonical_digest(payload), payload))


@router.get("/worker/status")
def worker_status(worker: Worker, store: Workers) -> dict[str, Any]:
    return store.worker_status(worker)


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
            body.admitted_attempt_id,
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
