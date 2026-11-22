"""Routes for observed-provider run receipts (worker report, human read)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal, worker_principal
from .execution_run_receipts import ExecutionRunReceiptStore, ReportRunReceipt
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def run_receipts(request: Request) -> ExecutionRunReceiptStore:
    return ExecutionRunReceiptStore(request.app.state.execution.database_path.parent)


RunReceipts = Annotated[ExecutionRunReceiptStore, Depends(run_receipts)]


@router.post("/worker/external-operations/{operation_id}/run-receipt")
def report_run_receipt(
    operation_id: str, body: ReportRunReceipt, worker: Worker, key: Key, store: RunReceipts
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.report_run_receipt(
        worker, operation_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(response, status_code=status)


@router.get("/external-operations/{operation_id}/run-receipt")
def get_run_receipt(operation_id: str, actor: Actor, store: RunReceipts) -> dict[str, Any]:
    return store.get_run_receipt(actor, operation_id)
