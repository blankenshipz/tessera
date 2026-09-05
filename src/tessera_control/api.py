"""FastAPI routes for the intentionally small local control API."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import ApproveTask, CreateProject, CreateTask
from .store import Conflict, Forbidden, NotFound, Store, StoreError, canonical_digest


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


def create_app(state_dir: Path) -> FastAPI:
    """Build a loopback-only local control API around an existing initialized state."""

    store = Store(state_dir)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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

    @app.post("/tasks/{task_id}/approve")
    def approve_task(
        task_id: str,
        body: ApproveTask,
        actor: Annotated[dict[str, str], Depends(principal)],
        key: Annotated[str, Depends(idempotency_key)],
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")
        status, response = store.approve_task(
            actor, task_id, key, canonical_digest(payload), body.expected_revision
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

    return app


def _unauthorized() -> NoReturn:
    raise HTTPException(status_code=401, detail="Unauthorized")
