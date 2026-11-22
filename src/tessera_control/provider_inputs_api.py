"""Provider input ticket routes (ADR 0037 s3).

Issue/revoke are worker routes.  The two pull routes have no principal: they authenticate
only by ``Authorization: Ticket <token>`` and answer every failure with one 401.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .api import worker_principal
from .provider_inputs import ProviderInputStore, TicketInvalid, TicketRequest

router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_INVALID = "Provider input ticket is not valid."


def _store(request: Request) -> ProviderInputStore:
    return request.app.state.provider_inputs


def _invalid() -> HTTPException:
    return HTTPException(status_code=401, detail=_INVALID)


def ticket_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Ticket "):
        raise _invalid()
    token = authorization.removeprefix("Ticket ")
    if not _TOKEN.fullmatch(token):
        raise _invalid()
    return token


Token = Annotated[str, Depends(ticket_token)]


@router.post("/worker/external-operations/{operation_id}/input-tickets")
def issue_input_ticket(
    operation_id: str, body: TicketRequest, request: Request, worker: Worker
) -> JSONResponse:
    # Deliberately no Idempotency-Key and no response persistence: the body holds a secret.
    issued = _store(request).issue_ticket(worker, operation_id, body.model_dump(mode="json"))
    return JSONResponse(status_code=201, content=issued, headers={"Cache-Control": "no-store"})


@router.post("/worker/external-operations/{operation_id}/input-tickets/revoke")
def revoke_input_tickets(
    operation_id: str, body: TicketRequest, request: Request, worker: Worker
) -> dict[str, Any]:
    return _store(request).revoke_tickets(worker, operation_id, body.model_dump(mode="json"))


@router.get("/provider-inputs/manifest")
def provider_manifest(request: Request, token: Token) -> JSONResponse:
    try:
        body = _store(request).read_manifest(token)
    except TicketInvalid:
        raise _invalid() from None
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})


@router.get("/provider-inputs/chunks/{seq}")
def provider_chunk(seq: int, request: Request, token: Token) -> JSONResponse:
    try:
        body = _store(request).read_chunk_for_ticket(token, seq)
    except TicketInvalid:
        raise _invalid() from None
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})
