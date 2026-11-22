"""Worker-audience route recording an operation's egress journal summary."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, worker_principal
from .egress_journal import EgressJournalStore
from .egress_models import ReportEgressJournal
from .store import canonical_digest

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def journal_store(request: Request) -> EgressJournalStore:
    return request.app.state.egress_journal


Store = Annotated[EgressJournalStore, Depends(journal_store)]


@router.post("/worker/operations/{operation_id}/egress-journal")
def report_egress_journal(
    operation_id: str, body: ReportEgressJournal, worker: Worker, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.report_egress_journal(
        worker, operation_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(response, status_code=status)
