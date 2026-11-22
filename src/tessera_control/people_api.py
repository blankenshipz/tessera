"""HTTP routes for owner-enrolled people (see people.py)."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .external_agent_api import Actor, Key
from .people import EnrolPerson, RenewPerson
from .store import _token_hash, canonical_digest

router = APIRouter()


@router.post("/people")
def enrol_person(body: EnrolPerson, request: Request, actor: Actor, key: Key):
    token = body.token.get_secret_value()
    payload = body.model_dump(mode="json", exclude={"token"})
    payload["token_hash"] = _token_hash(token)
    status, response = request.app.state.people.enrol(
        actor, key, canonical_digest(payload), payload, token
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/people")
def list_people(
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0, le=2**63 - 1),
    limit: int = Query(100, ge=1, le=200),
):
    return {"people": request.app.state.people.list_people(actor, offset, limit)}


@router.post("/people/{principal_id}/renew")
def renew_person(principal_id: str, body: RenewPerson, request: Request, actor: Actor, key: Key):
    payload = body.model_dump(mode="json")
    status, response = request.app.state.people.renew(
        actor,
        principal_id,
        key,
        canonical_digest({**payload, "principal_id": principal_id}),
        payload,
    )
    return JSONResponse(status_code=status, content=response)
