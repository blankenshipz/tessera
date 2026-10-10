"""FastAPI routes for the intentionally small local control API."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .deployment import DeploymentConfig, backend_status
from .models import (
    ApproveTask,
    CreateCredential,
    CreateProject,
    CreateTask,
    RenewWorkerCredential,
    ReviseTaskPlan,
)
from .request_limits import RequestLimitsMiddleware
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _token_hash, canonical_digest


class RejectBrowserOriginsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if "origin" in request.headers:
            return JSONResponse(
                status_code=403, content={"detail": "Browser origins are not accepted."}
            )
        return await call_next(request)


def principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> dict[str, str]:
    if authorization is None or not authorization.startswith("Bearer "):
        _unauthorized()
    token = authorization.removeprefix("Bearer ")
    if not token or token.strip() != token:
        _unauthorized()
    try:
        return request.app.state.store.authenticate(token)
    except Forbidden:
        _unauthorized()


def idempotency_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if value is None or not value.strip() or len(value) > 128:
        raise StoreError("A valid Idempotency-Key is required.")
    return value


def worker_principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    """Worker routes use a separate audience; worker tokens never admit control routes."""
    if authorization is None or not authorization.startswith("Bearer "):
        _unauthorized()
    token = authorization.removeprefix("Bearer ")
    if not token or token.strip() != token:
        _unauthorized()
    try:
        return request.app.state.store.authenticate_worker(token)
    except Forbidden:
        _unauthorized()


def create_app(
    state_dir: Path,
    deployment: DeploymentConfig | None = None,
    *,
    runtime_binding_adapter: Any = None,
    agent_sandbox_backend: Any = None,
) -> FastAPI:
    """Build a control API with an explicit serving profile around initialized state."""

    from .admin import AdminStore
    from .admin_api import router as admin_router
    from .agent_authority import AgentAuthorityStore
    from .agent_authority_api import router as agent_authority_router
    from .agent_provenance_api import router as agent_provenance_router
    from .agent_provenance_store import AgentProvenanceStore
    from .agent_research import AgentResearchStore
    from .artifact_api import router as artifact_router
    from .artifact_disposition_api import router as artifact_disposition_router
    from .astra import AstraStore
    from .astra_api import router as astra_router
    from .astra_status import AstraStatusStore
    from .astra_status_api import router as astra_status_router
    from .autonomy_api import router as autonomy_router
    from .capability_api import router as capability_router
    from .credential_lease_api import router as credential_lease_router
    from .credential_leases import _FakeSecretBackend
    from .dataset_ingestion import DatasetIngestionStore
    from .dataset_ingestion_api import router as dataset_ingestion_router
    from .execution import ExecutionStore
    from .execution_api import router as execution_router
    from .experiments import ExperimentStore
    from .experiments_api import router as experiments_router
    from .external_agent_api import router as external_agent_router
    from .external_agents import ExternalAgentStore
    from .handoff_api import router as handoff_router
    from .handoffs import WorkspaceHandoffStore
    from .interaction import InteractionStore
    from .interaction_api import router as interaction_router
    from .interaction_api import worker_router as interaction_worker_router
    from .observability import OperatorObservabilityStore
    from .remote_execution import RemoteExecutionStore
    from .remote_execution_api import router as remote_execution_router
    from .research_api import router as research_router
    from .skill_api import router as skill_router
    from .skills import SkillStore
    from .snapshot_releases import SnapshotReleaseStore
    from .snapshots import SnapshotStore

    deployment = deployment or DeploymentConfig.local()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestLimitsMiddleware)
    app.state.deployment = deployment

    @app.middleware("http")
    async def readiness_gate(request: Request, call_next: Any) -> Any:
        if request.url.path not in {"/health", "/health/live", "/health/ready"}:
            status = backend_status(state_dir)
            # A held recovery is intentionally served for authenticated forensics;
            # Store rejects every mutation there. Schema/state failures admit no API.
            if status["status"] != "ready" and status["reason"] != "recovery_hold":
                return JSONResponse(
                    status_code=503, content={"detail": "Control plane is not ready."}
                )
        return await call_next(request)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health")
    def legacy_health() -> dict[str, str]:
        """Keep the established local Compose probe response stable."""
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> JSONResponse:
        status = backend_status(state_dir)
        payload = {
            **status,
            "deployment_profile": deployment.profile,
            "public_origin": deployment.public_origin,
        }
        return JSONResponse(
            status_code=200 if status["status"] == "ready" else 503, content=payload
        )

    try:
        store = Store(state_dir)
    except ValueError as error:
        # An older/newer schema must keep a Pod alive long enough for its readiness
        # probe to state the migration problem, while all authority remains closed.
        if "schema version" not in str(error) and "requires an explicit" not in str(error):
            raise
        app.state.store = None
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(deployment.trusted_hosts))
        app.add_middleware(RejectBrowserOriginsMiddleware)
        return app
    app.state.store = store
    from .dataset_snapshot_api import router as dataset_snapshot_router

    app.include_router(dataset_snapshot_router)
    from .runtime_binding_api import router as runtime_binding_router
    from .runtime_bindings import NativeBridgeUnavailable, RuntimeBindingStore

    app.state.runtime_bindings = RuntimeBindingStore(state_dir, runtime_binding_adapter)
    app.include_router(runtime_binding_router)
    from tessera_worker.agent_sandbox import SandboxUnsafe

    from .agent_sandbox_api import router as agent_sandbox_router

    app.state.agent_sandbox_backend = agent_sandbox_backend
    app.include_router(agent_sandbox_router)

    @app.exception_handler(SandboxUnsafe)
    async def unresolved_sandbox(request: Request, error: SandboxUnsafe):
        return JSONResponse(status_code=409, content={"detail": "sandbox_evidence_unresolved"})

    @app.exception_handler(NativeBridgeUnavailable)
    async def unavailable_runtime_bridge(request: Request, error: NativeBridgeUnavailable):
        return JSONResponse(status_code=503, content={"detail": "native_bridge_unavailable"})

    from .generated_output_api import router as generated_output_router

    app.include_router(generated_output_router)
    from .ordinary_docker_api import router as ordinary_docker_router

    app.include_router(ordinary_docker_router)
    from .capture_context_api import router as capture_context_router

    app.include_router(capture_context_router)
    app.state.admin = AdminStore(state_dir)
    app.include_router(admin_router)
    app.state.external_agents = ExternalAgentStore(state_dir)
    app.include_router(external_agent_router)
    app.state.agent_authority = AgentAuthorityStore(state_dir)
    app.state.agent_research = AgentResearchStore(state_dir)
    app.include_router(agent_authority_router)
    app.state.agent_provenance = AgentProvenanceStore(state_dir)
    app.include_router(agent_provenance_router)
    app.state.observability = OperatorObservabilityStore(state_dir)
    # Private in-memory development/test seam.  It has no persisted values or API
    # for inserting/resolving secret values.
    app.state.fake_secret_backend = _FakeSecretBackend()
    app.state.execution = ExecutionStore(state_dir)
    app.state.remote_execution = RemoteExecutionStore(state_dir)
    app.state.snapshot_releases = SnapshotReleaseStore(state_dir)
    app.state.skills = SkillStore(state_dir)
    app.state.handoffs = WorkspaceHandoffStore(state_dir)
    app.include_router(execution_router)
    app.include_router(credential_lease_router)
    app.include_router(capability_router)
    app.include_router(autonomy_router)
    app.include_router(remote_execution_router)
    app.include_router(skill_router)
    app.include_router(artifact_router)
    app.include_router(artifact_disposition_router)
    app.state.research = DatasetIngestionStore(state_dir)
    app.state.experiments = ExperimentStore(state_dir)
    app.state.interactions = InteractionStore(store)
    app.state.astra = AstraStore(store)
    app.state.astra_status = AstraStatusStore(store)
    snapshots = SnapshotStore(state_dir)
    app.include_router(research_router)
    app.include_router(dataset_ingestion_router)
    app.include_router(experiments_router)
    app.include_router(interaction_router)
    app.include_router(interaction_worker_router)
    app.include_router(astra_router)
    app.include_router(astra_status_router)
    app.include_router(handoff_router)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(deployment.trusted_hosts),
    )
    app.add_middleware(RejectBrowserOriginsMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid request."})

    @app.exception_handler(StoreError)
    async def store_error(_: Request, error: StoreError) -> JSONResponse:
        if isinstance(error, NotFound):
            status = 404
        elif isinstance(error, Conflict):
            status = 409
        elif isinstance(error, Forbidden):
            status = 403
        else:
            status = 400
        return JSONResponse(status_code=status, content={"detail": str(error)})

    @app.get("/me")
    def me(actor: Annotated[dict[str, Any], Depends(principal)]) -> dict[str, Any]:
        return store.me(actor)

    @app.get("/recovery")
    def recovery(actor: Annotated[dict[str, Any], Depends(principal)]) -> dict[str, Any]:
        return store.recovery_status(actor)

    @app.get("/operator/health")
    def operator_health(actor: Annotated[dict[str, Any], Depends(principal)]) -> dict[str, Any]:
        return app.state.observability.health(actor)

    @app.get("/operator/audit")
    def operator_audit(
        actor: Annotated[dict[str, Any], Depends(principal)],
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, list[dict[str, Any]]]:
        return {"events": app.state.observability.audit_events(actor, after, limit)}

    @app.get("/credentials")
    def list_credentials(
        actor: Annotated[dict[str, Any], Depends(principal)],
        offset: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, list[dict[str, Any]]]:
        return {"credentials": store.list_credentials(actor, offset, limit)}

    @app.post("/credentials")
    def issue_credential(
        body: CreateCredential,
        actor: Annotated[dict[str, Any], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        token = body.token.get_secret_value()
        payload = body.model_dump(mode="json", exclude={"token"})
        # Preserve the pre-v50 command digest for ordinary credential retries.
        if payload.get("external_agent") is None:
            payload.pop("external_agent", None)
        payload["token_hash"] = _token_hash(token)
        status, response = store.issue_credential(
            actor, key, canonical_digest(payload), payload, token
        )
        return JSONResponse(status_code=status, content=response)

    @app.post("/credentials/{credential_id}/revoke")
    def revoke_credential(
        credential_id: str,
        actor: Annotated[dict[str, Any], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = {"credential_id": credential_id}
        status, response = store.revoke_credential(
            actor, credential_id, key, canonical_digest(payload)
        )
        return JSONResponse(status_code=status, content=response)

    @app.post("/credentials/{credential_id}/renew")
    def renew_worker(
        credential_id: str,
        body: RenewWorkerCredential,
        actor: Annotated[dict[str, Any], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")
        status, response = store.renew_worker_credential(
            actor, credential_id, key, canonical_digest(payload), payload
        )
        return JSONResponse(status_code=status, content=response)

    @app.get("/projects")
    def list_projects(
        actor: Annotated[dict[str, str], Depends(principal)],
        offset: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"projects": store.list_projects(actor, offset, limit)}

    @app.post("/projects")
    def create_project(
        body: CreateProject,
        actor: Annotated[dict[str, str], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")
        status, response = store.create_project(actor, key, canonical_digest(payload), payload)
        return JSONResponse(status_code=status, content=response)

    @app.get("/projects/{project_id}")
    def get_project(
        project_id: str, actor: Annotated[dict[str, str], Depends(principal)]
    ) -> dict[str, Any]:
        return store.get_project(actor, project_id)

    @app.get("/projects/{project_id}/tasks")
    def list_tasks(
        project_id: str,
        actor: Annotated[dict[str, str], Depends(principal)],
        offset: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"tasks": store.list_tasks(actor, project_id, offset, limit)}

    @app.post("/projects/{project_id}/tasks")
    def create_task(
        project_id: str,
        body: CreateTask,
        actor: Annotated[dict[str, str], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")
        status, response = store.create_task(
            actor, project_id, key, canonical_digest(payload), payload
        )
        return JSONResponse(status_code=status, content=response)

    @app.get("/tasks/{task_id}")
    def get_task(
        task_id: str, actor: Annotated[dict[str, str], Depends(principal)]
    ) -> dict[str, Any]:
        return store.get_task(actor, task_id)

    @app.get("/tasks/{task_id}/revisions")
    def list_task_revisions(
        task_id: str,
        actor: Annotated[dict[str, str], Depends(principal)],
        offset: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, list[dict[str, Any]]]:
        return {"revisions": store.list_task_revisions(actor, task_id, offset, limit)}

    @app.get("/tasks/{task_id}/plans/{plan_digest}")
    def get_current_task_plan(
        task_id: str,
        plan_digest: str,
        actor: Annotated[dict[str, str], Depends(principal)],
    ) -> dict[str, Any]:
        return store.get_current_task_plan(actor, task_id, plan_digest)

    @app.put("/tasks/{task_id}/plan")
    def revise_task_plan(
        task_id: str,
        body: ReviseTaskPlan,
        actor: Annotated[dict[str, str], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")
        status, response = store.revise_task_plan(
            actor, task_id, key, canonical_digest(payload), payload
        )
        return JSONResponse(status_code=status, content=response)

    @app.post("/tasks/{task_id}/approve")
    def approve_task(
        task_id: str,
        body: ApproveTask,
        actor: Annotated[dict[str, str], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = body.model_dump(mode="json", exclude_none=True)
        status, response = store.approve_task(
            actor,
            task_id,
            key,
            canonical_digest(payload),
            body.expected_revision,
            body.expected_plan_digest,
        )
        return JSONResponse(status_code=status, content=response)

    @app.get("/projects/{project_id}/labbook")
    def labbook(
        project_id: str,
        actor: Annotated[dict[str, str], Depends(principal)],
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, list[dict[str, Any]]]:
        return {"events": store.labbook(actor, project_id, after, limit)}

    @app.get("/projects/{project_id}/snapshots")
    def list_snapshots(
        project_id: str,
        actor: Annotated[dict[str, Any], Depends(principal)],
        offset: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"snapshots": snapshots.list_snapshots(actor, project_id, offset, limit)}

    @app.get("/snapshots/{snapshot_id}")
    def get_snapshot(
        snapshot_id: str, actor: Annotated[dict[str, Any], Depends(principal)]
    ) -> dict[str, Any]:
        return snapshots.get_snapshot(actor, snapshot_id)

    return app


def _unauthorized() -> NoReturn:
    raise HTTPException(status_code=401, detail="Unauthorized")
