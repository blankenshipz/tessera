"""Human enrollment and separately authenticated Docker worker endpoints."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .execution_api import Actor, Key, Worker
from .ordinary_docker import OrdinaryDockerStore
from .ordinary_docker_models import (
    DockerDispatchRequest,
    DockerOperationFence,
    DockerPackageRequest,
    DockerReadback,
    DockerReport,
    DockerRevoke,
    EnrollDockerTarget,
)
from .store import canonical_digest

router = APIRouter()


def store(request):
    return OrdinaryDockerStore(request.app.state.store.database_path.parent)


def response(value):
    code, body = value
    return JSONResponse(status_code=code, content=body)


@router.post("/projects/{project_id}/docker-targets")
def enroll(project_id: str, body: EnrollDockerTarget, request: Request, actor: Actor, key: Key):
    return response(store(request).enroll(actor, project_id, key, body.model_dump(mode="json")))


@router.post("/docker-targets/{target_id}/revoke")
def revoke(target_id: str, body: DockerRevoke, request: Request, actor: Actor, key: Key):
    return response(store(request).revoke(actor, target_id, key, body.model_dump(mode="json")))


@router.post("/worker/docker/leases/{lease_id}/package")
def package(lease_id: str, body: DockerPackageRequest, request: Request, actor: Worker, key: Key):
    return response(store(request).package(actor, lease_id, key, body.model_dump(mode="json")))


@router.post("/experiment-admitted-attempts/{attempt_id}/docker-dispatch")
def dispatch(
    attempt_id: str, body: DockerDispatchRequest, request: Request, actor: Actor, key: Key
):
    backend = store(request)
    payload = body.model_dump(mode="json")

    def command(connection, live):
        backend._require_human(live)
        return backend.dispatch_transaction(connection, live, attempt_id, payload)

    def guard(connection, live, cached):
        backend._require_human(live)
        row = connection.execute(
            "SELECT target_id,package_json FROM ordinary_docker_dispatches WHERE operation_id=?",
            (cached["operation_id"],),
        ).fetchone()
        if row is None:
            from .store import Conflict

            raise Conflict("Docker dispatch record is missing.")
        backend.target(connection, live, row[0])
        from tessera_worker.ordinary_docker_contract import DockerPackage

        package = DockerPackage.model_validate_json(row[1])
        worker = backend._live_principal(
            connection,
            {"id": package.worker_principal_id, "org_id": live["org_id"]},
            audience="worker",
        )
        backend.authority(
            connection,
            worker,
            {
                "operation_id": package.operation_id,
                "admitted_attempt_id": package.admitted_attempt_id,
                "lease_id": package.lease_id,
                "expected_epoch": package.epoch,
                "package_digest": package.package_digest,
                "target_digest": package.target_digest,
            },
        )

    return response(
        backend._mutate(
            actor,
            key,
            "POST",
            f"/experiment-admitted-attempts/{attempt_id}/docker-dispatch",
            canonical_digest(payload),
            command,
            replay_guard=guard,
        )
    )


@router.post("/worker/docker/current-authority")
def authority(body: DockerOperationFence, request: Request, actor: Worker):
    return store(request).current(actor, body.model_dump(mode="json"))


@router.get("/worker/docker/dispatches")
def pending(request: Request, actor: Worker, target_digest: str = Query(pattern=r"^[a-f0-9]{64}$")):
    return store(request).pending(actor, target_digest)


@router.post("/worker/docker/readback")
def readback(body: DockerReadback, request: Request, actor: Worker):
    return store(request).readback(actor, body.model_dump(mode="json"))


@router.post("/worker/docker/claim")
def claim(body: DockerOperationFence, request: Request, actor: Worker, key: Key):
    return response(store(request).claim(actor, key, body.model_dump(mode="json")))


@router.post("/worker/docker/report")
def report(body: DockerReport, request: Request, actor: Worker, key: Key):
    return response(store(request).report(actor, key, body.model_dump(mode="json")))
