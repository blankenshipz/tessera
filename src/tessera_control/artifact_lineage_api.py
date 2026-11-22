"""HTTP routes for tenant-scoped artifact lineage bookkeeping."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .artifact_lineage import (
    MAX_LINEAGE_DEPTH,
    ArtifactLineageStore,
    LineageEdgeRequest,
    LineageNodeRequest,
)

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def lineage_store(request: Request) -> ArtifactLineageStore:
    return ArtifactLineageStore(request.app.state.store.database_path.parent)


StoreDependency = Annotated[ArtifactLineageStore, Depends(lineage_store)]
_BASE = "/projects/{project_id}/artifact-lineage"


@router.post(_BASE + "/nodes")
def register_node(
    project_id: str, body: LineageNodeRequest, actor: Actor, key: Key, store: StoreDependency
) -> JSONResponse:
    status, response = store.register_node(actor, project_id, key, body.model_dump(mode="json"))
    return JSONResponse(status_code=status, content=response)


@router.get(_BASE + "/nodes")
def list_nodes(
    project_id: str,
    actor: Actor,
    store: StoreDependency,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=200),
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    return {"nodes": store.list_nodes(actor, project_id, offset, limit, kind)}


@router.get(_BASE + "/nodes/{node_id}")
def get_node(project_id: str, node_id: str, actor: Actor, store: StoreDependency) -> dict[str, Any]:
    return store.get_node(actor, project_id, node_id)


@router.post(_BASE + "/edges")
def record_edge(
    project_id: str, body: LineageEdgeRequest, actor: Actor, key: Key, store: StoreDependency
) -> JSONResponse:
    status, response = store.record_edge(actor, project_id, key, body.model_dump(mode="json"))
    return JSONResponse(status_code=status, content=response)


@router.get(_BASE + "/nodes/{node_id}/lineage")
def node_lineage(
    project_id: str,
    node_id: str,
    actor: Actor,
    store: StoreDependency,
    direction: Literal["ancestors", "descendants"] = Query(default="ancestors"),
    max_depth: int = Query(default=MAX_LINEAGE_DEPTH, ge=1, le=MAX_LINEAGE_DEPTH),
) -> dict[str, Any]:
    return store.lineage(actor, project_id, node_id, direction, max_depth)


@router.get(_BASE + "/export")
def export_lineage(project_id: str, actor: Actor, store: StoreDependency) -> dict[str, Any]:
    return store.export_lineage(actor, project_id)
