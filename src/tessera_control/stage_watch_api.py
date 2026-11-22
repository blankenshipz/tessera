"""Worker-audience route recording an operation's stage watch report (ADR 0087)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, worker_principal
from .stage_watch import StageWatchStore, TooEarly
from .stage_watch_models import ReportStageWatch
from .store import canonical_digest

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def watch_store(request: Request) -> StageWatchStore:
    return request.app.state.stage_watch


Store = Annotated[StageWatchStore, Depends(watch_store)]


@router.post("/worker/operations/{operation_id}/watch-reports")
def report_stage_watch(
    operation_id: str, body: ReportStageWatch, worker: Worker, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    try:
        status, response = store.report_stage_watch(
            worker, operation_id, key, canonical_digest(payload), payload
        )
    except TooEarly as error:
        # 425 Too Early: retryable, unlike the 409 conflicts this route otherwise returns.
        return JSONResponse({"detail": str(error)}, status_code=425)
    return JSONResponse(response, status_code=status)
