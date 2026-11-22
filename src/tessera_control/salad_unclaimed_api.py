"""Human route for the zero-settlement reconcile of a Salad grant with no provider claim."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, principal
from .salad_unclaimed import ReconcileUnclaimedSalad, SaladUnclaimedStore
from .store import canonical_digest

router = APIRouter()
Actor = Annotated[dict[str, Any], Depends(principal)]
Key = Annotated[str, Depends(idempotency_key)]


def unclaimed(request: Request) -> SaladUnclaimedStore:
    return request.app.state.salad_unclaimed


Unclaimed = Annotated[SaladUnclaimedStore, Depends(unclaimed)]


@router.post("/execution-grants/{grant_id}/reconcile-unclaimed-salad")
def reconcile_unclaimed_salad(
    grant_id: str, body: ReconcileUnclaimedSalad, actor: Actor, key: Key, store: Unclaimed
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, content = store.reconcile(actor, grant_id, key, canonical_digest(payload), payload)
    return JSONResponse(status_code=status, content=content)
