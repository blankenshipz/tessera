"""Worker-audience routes recording an operation's stage watch reports and decision requests
(ADR 0087, ADR 0111)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, worker_principal
from .stage_watch import KEY_CONFLICT_DETAIL, StageWatchStore, WatchRefused
from .stage_watch_decisions import record_watch_decision
from .stage_watch_decisions_models import RecordWatchDecision
from .stage_watch_limits import REASON_STATUS
from .stage_watch_models import ReportStageWatch
from .store import Conflict, StoreError, canonical_digest

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def watch_store(request: Request) -> StageWatchStore:
    return request.app.state.stage_watch


Store = Annotated[StageWatchStore, Depends(watch_store)]


def _refusal(error: StoreError) -> JSONResponse:
    # Every definite refusal carries a closed ``reason`` (``stage_watch_limits``). The shared
    # idempotency check raises a plain Conflict; it is mapped here by its fixed message so no
    # other route changes. Anything else keeps the global handler's response with no reason,
    # which the worker treats as transient.
    if isinstance(error, WatchRefused):
        reason = error.reason
    elif isinstance(error, Conflict) and str(error) == KEY_CONFLICT_DETAIL:
        reason = "key_conflict"
    else:
        raise error
    return JSONResponse({"detail": str(error), "reason": reason}, status_code=REASON_STATUS[reason])


@router.post("/worker/operations/{operation_id}/watch-reports")
def report_stage_watch(
    operation_id: str, body: ReportStageWatch, worker: Worker, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    if payload.get("exit") is None:
        # The optional ``exit`` (ADR 0158) is left out when absent, so a report without it keeps
        # the exact body and idempotency digest it had before the field existed.
        payload.pop("exit", None)
    try:
        status, response = store.report_stage_watch(
            worker, operation_id, key, canonical_digest(payload), payload
        )
    except StoreError as error:
        return _refusal(error)
    return JSONResponse(response, status_code=status)


@router.post("/worker/operations/{operation_id}/watch-decisions")
def record_stage_watch_decision(
    operation_id: str, body: RecordWatchDecision, worker: Worker, key: Key, store: Store
) -> JSONResponse:
    """Record an advisory decision request; it is never admitted or acted on here."""
    payload = body.model_dump(mode="json")
    try:
        status, response = record_watch_decision(
            store, worker, operation_id, key, canonical_digest(payload), payload
        )
    except StoreError as error:
        return _refusal(error)
    return JSONResponse(response, status_code=status)
