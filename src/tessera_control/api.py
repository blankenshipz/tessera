"""FastAPI routes for the intentionally small local control API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .approval_trust import (
    AUTOMATION_HEADER,
    AUTOMATION_KEY,
    DELEGATION_HEADER,
    DELEGATION_KEY,
    valid_automation_label,
)
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
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    automation: Annotated[str | None, Header(alias=AUTOMATION_HEADER)] = None,
    delegation: Annotated[str | None, Header(alias=DELEGATION_HEADER)] = None,
) -> dict[str, str]:
    if authorization is None or not authorization.startswith("Bearer "):
        _unauthorized()
    token = authorization.removeprefix("Bearer ")
    if not token or token.strip() != token:
        _unauthorized()
    try:
        actor = request.app.state.store.authenticate(token)
    except Forbidden:
        _unauthorized()
    request.app.state.store.check_credential_route(actor, request.method, request.url.path)
    if automation is not None:
        # Only a label: it never grants authority, it marks decisions as automation.
        if not valid_automation_label(automation):
            raise StoreError(
                "X-Tessera-Automation must be a non-empty label of at most 128 characters."
            )
        actor[AUTOMATION_KEY] = automation
    if delegation is not None:
        # A reference to the owner delegation the helper acts under; a label only, and never
        # accepted without the automation label it qualifies.
        if automation is None:
            raise StoreError("X-Tessera-Delegation requires an X-Tessera-Automation label.")
        if not valid_automation_label(delegation):
            raise StoreError(
                "X-Tessera-Delegation must be a non-empty label of at most 128 characters."
            )
        actor[DELEGATION_KEY] = delegation
    return actor


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
    campaign_runner_interval: float | None = None,
    campaign_scheduler: bool = True,
    campaign_stuck_scan_seconds: float = 300.0,
    review_single_owner: bool = False,
    storage_retention: bool = False,
    code_review_git_remotes: Path | str | None = None,
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
    from .artifact_lineage_api import router as artifact_lineage_router
    from .astra import AstraStore
    from .astra_api import router as astra_router
    from .astra_status import AstraStatusStore
    from .astra_status_api import router as astra_status_router
    from .autonomy_api import router as autonomy_router
    from .capability_api import router as capability_router
    from .credential_health_api import router as credential_health_router
    from .credential_lease_api import router as credential_lease_router
    from .credential_leases import _FakeSecretBackend
    from .dataset_ingestion import DatasetIngestionStore
    from .dataset_ingestion_api import router as dataset_ingestion_router
    from .deploy_check_api import router as deploy_check_router
    from .execution import ExecutionStore
    from .execution_api import router as execution_router
    from .execution_run_receipt_api import router as execution_run_receipt_router
    from .experiments import ExperimentStore
    from .experiments_api import router as experiments_router
    from .external_agent_api import router as external_agent_router
    from .external_agents import ExternalAgentStore
    from .handoff_api import router as handoff_router
    from .handoffs import WorkspaceHandoffStore
    from .interaction import InteractionStore
    from .interaction_api import router as interaction_router
    from .interaction_api import worker_router as interaction_worker_router
    from .large_artifact_api import router as large_artifact_router
    from .observability import OperatorObservabilityStore
    from .provider_inputs import ProviderInputStore
    from .provider_inputs_api import router as provider_inputs_router
    from .provider_registration_api import router as provider_registration_router
    from .remote_execution import RemoteExecutionStore
    from .remote_execution_api import router as remote_execution_router
    from .research_api import router as research_router
    from .skill_api import router as skill_router
    from .skills import SkillStore
    from .snapshot_releases import SnapshotReleaseStore
    from .snapshots import SnapshotStore
    from .storage_retention_api import router as storage_retention_router

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
    app.include_router(provider_registration_router)
    from .license_labels_api import router as license_labels_router

    app.include_router(license_labels_router)
    from .model_spend_api import router as model_spend_router

    app.include_router(model_spend_router)
    app.state.storage_retention_state_dir = state_dir
    app.include_router(storage_retention_router)
    app.include_router(credential_health_router)
    app.include_router(deploy_check_router)
    app.state.external_agents = ExternalAgentStore(state_dir)
    app.include_router(external_agent_router)
    app.state.agent_authority = AgentAuthorityStore(state_dir)
    app.state.agent_research = AgentResearchStore(state_dir)
    app.include_router(agent_authority_router)
    from .review_jobs import NoDestinationsPolicy, ReviewJobStore
    from .review_jobs_api import router as review_jobs_router

    app.state.review_single_owner = review_single_owner
    app.state.review_jobs = ReviewJobStore(state_dir)
    app.state.review_jobs.single_owner = review_single_owner
    app.state.review_policy = NoDestinationsPolicy()
    # Operator git remotes for code review submit (ADR 0108); none: submit is refused. A
    # malformed file raises ValueError/OSError here, so startup fails closed.
    app.state.code_review_git = None
    if code_review_git_remotes is not None:
        from .code_review_git import load_git_remotes

        app.state.code_review_git = load_git_remotes(code_review_git_remotes)
    app.include_router(review_jobs_router)
    from .agent_loop_control import AgentLoopControlStore
    from .agent_loop_control_api import router as agent_loop_control_router

    app.state.agent_loop_control = AgentLoopControlStore(state_dir)
    app.include_router(agent_loop_control_router)
    from .people import PeopleStore
    from .people_api import router as people_router

    app.state.people = PeopleStore(state_dir)
    app.include_router(people_router)
    from .agent_research_results import AgentResultStore
    from .agent_research_results_api import router as agent_results_router

    app.state.agent_results = AgentResultStore(state_dir)
    app.include_router(agent_results_router)
    app.state.agent_provenance = AgentProvenanceStore(state_dir)
    app.include_router(agent_provenance_router)
    app.state.observability = OperatorObservabilityStore(state_dir)
    # Private in-memory development/test seam.  It has no persisted values or API
    # for inserting/resolving secret values.
    app.state.fake_secret_backend = _FakeSecretBackend()
    app.state.execution = ExecutionStore(state_dir)
    from .dataset_staging import DatasetStagingStore
    from .dataset_staging_api import router as dataset_staging_router
    from .egress_journal import EgressJournalStore
    from .egress_journal_api import router as egress_journal_router
    from .image_build_report import ImageBuildStore
    from .image_build_report import router as image_build_router
    from .public_fetch_report import PublicFetchStore
    from .public_fetch_report import router as public_fetch_router
    from .snapshot_chunks import ChunkedSnapshotStore
    from .snapshot_chunks_api import router as snapshot_chunks_router
    from .stage_watch import StageWatchStore
    from .stage_watch_api import router as stage_watch_router

    app.state.egress_journal = EgressJournalStore(state_dir)
    app.state.stage_watch = StageWatchStore(state_dir)
    app.state.public_fetch = PublicFetchStore(state_dir)
    app.state.image_build = ImageBuildStore(state_dir)
    app.state.dataset_staging = DatasetStagingStore(state_dir)
    app.state.chunked_snapshots = ChunkedSnapshotStore(state_dir)
    app.state.remote_execution = RemoteExecutionStore(state_dir)
    app.state.snapshot_releases = SnapshotReleaseStore(state_dir)
    app.state.skills = SkillStore(state_dir)
    app.state.handoffs = WorkspaceHandoffStore(state_dir)
    app.state.provider_inputs = ProviderInputStore(state_dir)
    app.include_router(execution_router)
    app.include_router(execution_run_receipt_router)
    app.include_router(provider_inputs_router)
    app.include_router(egress_journal_router)
    app.include_router(stage_watch_router)
    app.include_router(public_fetch_router)
    app.include_router(image_build_router)
    app.include_router(dataset_staging_router)
    app.include_router(snapshot_chunks_router)
    app.include_router(credential_lease_router)
    app.include_router(capability_router)
    app.include_router(autonomy_router)
    app.include_router(remote_execution_router)
    app.include_router(skill_router)
    from .campaign_api import router as campaign_router
    from .campaign_store import CampaignStore
    from .snapshot_composition import SnapshotCompositionStore
    from .snapshot_composition_api import router as snapshot_composition_router
    from .worker_grants_api import router as worker_grants_router

    app.state.campaigns = CampaignStore(state_dir)
    app.state.snapshot_composition = SnapshotCompositionStore(state_dir)
    app.include_router(campaign_router)
    from .campaign_stuck_api import router as campaign_stuck_router

    app.include_router(campaign_stuck_router)
    from .agent_campaign_reads import AgentCampaignReads
    from .agent_campaign_reads_api import router as agent_campaign_reads_router

    app.state.agent_campaign_reads = AgentCampaignReads(state_dir)
    app.include_router(agent_campaign_reads_router)
    from .stage_watch_read import StageWatchRead
    from .stage_watch_read import router as stage_watch_read_router

    app.state.stage_watch_read = StageWatchRead(state_dir)
    app.include_router(stage_watch_read_router)
    from .campaign_submissions import AgentSubmissionConfig, CampaignSubmissionStore
    from .campaign_submissions_api import router as campaign_submissions_router

    app.state.campaign_submissions = CampaignSubmissionStore(state_dir)
    # Off by default (ADR 0093): serve, or the embedding operator, replaces it.
    app.state.agent_submission = AgentSubmissionConfig()
    app.include_router(campaign_submissions_router)
    app.include_router(snapshot_composition_router)
    app.include_router(worker_grants_router)
    from .salad_unclaimed import SaladUnclaimedStore
    from .salad_unclaimed_api import router as salad_unclaimed_router

    app.state.salad_unclaimed = SaladUnclaimedStore(state_dir)
    app.include_router(salad_unclaimed_router)
    app.include_router(artifact_router)
    app.include_router(large_artifact_router)
    app.include_router(artifact_disposition_router)
    app.include_router(artifact_lineage_router)
    app.state.research = DatasetIngestionStore(state_dir)
    app.state.experiments = ExperimentStore(state_dir)
    from .gate_evidence import GateEvidenceStore
    from .gate_evidence_api import router as gate_evidence_router
    from .gate_evidence_metrics_api import router as gate_evidence_metrics_router
    from .gate_evidence_programs_api import router as gate_evidence_programs_router

    app.state.gate_evidence = GateEvidenceStore(state_dir)
    app.state.interactions = InteractionStore(store)
    app.state.astra = AstraStore(store)
    app.state.astra_status = AstraStatusStore(store)
    snapshots = SnapshotStore(state_dir)
    app.include_router(research_router)
    app.include_router(dataset_ingestion_router)
    app.include_router(experiments_router)
    app.include_router(gate_evidence_router)
    app.include_router(gate_evidence_metrics_router)
    app.include_router(gate_evidence_programs_router)
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

    if campaign_runner_interval is not None:
        # In-process campaign runner: started and stopped with the ASGI lifespan, composed
        # with (never replacing) the existing lifespan.
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def runner_lifespan(application: FastAPI):
            from . import campaign_runner

            handle = campaign_runner.start_background(
                state_dir, campaign_runner_interval, scheduler=campaign_scheduler
            )
            try:
                async with previous_lifespan(application) as state:
                    yield state
            finally:
                handle.stop()

        app.router.lifespan_context = runner_lifespan

    # The stuck-campaign scanner reads findings only and is independent of the runner: it runs
    # whenever the service does (interval 0 disables it), including with --no-campaign-runner.
    scanner_previous = app.router.lifespan_context

    @asynccontextmanager
    async def scanner_lifespan(application: FastAPI):
        from . import campaign_stuck

        scanner = campaign_stuck.start_background(
            state_dir / "tessera.sqlite3", campaign_stuck_scan_seconds
        )
        try:
            async with scanner_previous(application) as state:
                yield state
        finally:
            scanner.stop()

    app.router.lifespan_context = scanner_lifespan

    if storage_retention:
        # Retention timer: reports (dry run) or, only when the operator's policy sets
        # auto_apply, applies; composed with the existing lifespan like the runner.
        retention_previous = app.router.lifespan_context

        @asynccontextmanager
        async def retention_lifespan(application: FastAPI):
            from . import storage_retention as retention

            handle = retention.start_background(state_dir)
            try:
                async with retention_previous(application) as state:
                    yield state
            finally:
                handle.stop()

        app.router.lifespan_context = retention_lifespan

    return app


def _unauthorized() -> NoReturn:
    raise HTTPException(status_code=401, detail="Unauthorized")
