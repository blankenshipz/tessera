"""Human byte adoption and scoped retained-profile read; releases stay separate."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .dataset_snapshot_models import UploadDatasetSnapshot
from .dataset_snapshots import DatasetSnapshotStore

router = APIRouter()
Actor = Annotated[dict, Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def datasets(request: Request):
    return DatasetSnapshotStore(request.app.state.store.database_path.parent)


@router.post("/projects/{project_id}/tabular-dataset-snapshots")
def adopt(project_id: str, body: UploadDatasetSnapshot, request: Request, actor: Actor, key: Key):
    status, result = datasets(request).adopt_dataset(actor, project_id, key, body)
    return JSONResponse(status_code=status, content=result)


@router.get("/tabular-dataset-snapshots/{snapshot_id}")
def read(snapshot_id: str, request: Request, actor: Actor):
    return datasets(request).get_dataset(actor, snapshot_id)
