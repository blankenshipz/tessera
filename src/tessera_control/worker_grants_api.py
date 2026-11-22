"""Worker-audience route listing the caller's pending execution grants."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from .api import worker_principal
from .worker_grants import WorkerGrantStore

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]


def grants(request: Request) -> WorkerGrantStore:
    return WorkerGrantStore(request.app.state.execution.database_path.parent)


Grants = Annotated[WorkerGrantStore, Depends(grants)]


@router.get("/worker/execution-grants")
def list_pending_grants(worker: Worker, store: Grants) -> dict[str, Any]:
    return store.list_pending(worker)
