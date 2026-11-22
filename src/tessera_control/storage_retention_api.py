"""Storage retention routes: status, plan (dry run) and apply by digest.

A person, or an enrolled external agent holding ``retention:read`` (status and plan) or
``retention:apply`` (apply a listed plan), may call them. Workers never can. The caller never
supplies a path or a policy: what a plan lists and an apply deletes is what the operator's
policy file selects, and an apply needs the digest of a plan that is still current. Responses
carry only relative refs and fixed reason text, never an absolute path or raw OS error text.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from .external_agent_api import Actor
from .store import Conflict, Forbidden, _connect

router = APIRouter(prefix="/storage-retention")
_PATH = re.compile(r"(?<![\w.])/[\w.\-/]+")
_CANDIDATE_FIELDS = ("decision", "reasons", "kind", "ref", "path", "size_bytes", "sha256")


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def _trigger(request: Request, actor: dict[str, Any], action: str) -> str:
    """Authorize a person or an agent enrolled for ``action``; return the receipt trigger."""
    store = request.app.state.external_agents
    connection = _connect(store.database_path)
    try:
        live = store._actor(connection, actor)
        if live.get("kind") == "worker":
            raise Forbidden("Workers cannot use storage retention.")
        if live["role"] == "human":
            return "manual"
        store._scope(connection, live, action)
        return "agent"
    finally:
        connection.close()


def _scrub(text: object) -> str:
    return _PATH.sub("<path>", str(text))[:300]


def _state_dir(request: Request):
    return request.app.state.storage_retention_state_dir


@router.get("/status")
def status(request: Request, actor: Actor) -> dict[str, Any]:
    _trigger(request, actor, "retention:read")
    from .storage_retention_status import retention_status

    return retention_status(_state_dir(request))


@router.post("/plan")
def plan(request: Request, actor: Actor, body: PlanBody) -> dict[str, Any]:
    trigger = _trigger(request, actor, "retention:read")
    from . import storage_retention as retention

    try:
        receipt = retention.run(_state_dir(request), mode="dry_run", trigger=trigger)
    except retention.RetentionError as error:
        raise Conflict(_scrub(error)) from None
    listed = receipt["plan"]
    return {
        "run_id": receipt["run_id"],
        "plan_digest": receipt["plan_digest"],
        "candidates": [
            {field: candidate.get(field) for field in _CANDIDATE_FIELDS}
            for candidate in listed["candidates"]
        ],
        "skipped": len(listed.get("skipped", [])),
    }


@router.post("/apply")
def apply(request: Request, actor: Actor, body: ApplyBody):
    trigger = _trigger(request, actor, "retention:apply")
    from . import storage_retention as retention

    try:
        receipt = retention.run(
            _state_dir(request),
            mode="apply",
            trigger=trigger,
            expect_plan_digest=body.plan_digest,
        )
    except retention.RetentionError as error:
        # The refusal is also a receipt; its reason is fixed text the caller needs to re-plan.
        return JSONResponse(status_code=409, content={"status": "refused", "reason": _scrub(error)})
    return {
        "run_id": receipt["run_id"],
        "plan_digest": receipt["plan_digest"],
        "status": receipt["status"],
        "bytes_freed": receipt["bytes_freed"],
        "deleted": [
            {k: item.get(k) for k in ("kind", "ref", "size_bytes", "sha256")}
            for item in receipt["deleted"]
        ],
        "failed": [
            {
                "kind": item.get("kind"),
                "ref": item.get("ref"),
                "reason": _scrub(item.get("reason", item.get("error", ""))),
                **({"state_after": item["state_after"]} if "state_after" in item else {}),
            }
            for item in receipt["failed"]
        ],
    }
