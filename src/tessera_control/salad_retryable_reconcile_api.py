"""Human route: reconcile an unknown Salad attempt as retryable infrastructure (ADR 0216)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .salad_retryable_reconcile import ReconcileRetryableSalad, SaladRetryableReconcileStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def retryable(request: Request) -> SaladRetryableReconcileStore:
    return request.app.state.salad_retryable_reconcile


Retryable = Annotated[SaladRetryableReconcileStore, Depends(retryable)]


@router.post("/execution-grants/{grant_id}/reconcile-retryable-salad")
def reconcile_retryable_salad(
    grant_id: str, body: ReconcileRetryableSalad, actor: Actor, key: Key, store: Retryable
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, content = store.reconcile_retryable(
        actor, grant_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(status_code=status, content=content)
