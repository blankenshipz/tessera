"""Human capability decisions and separately authenticated worker intent/audit routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from .capabilities import CapabilityStore
from .capability_models import (
    AdmitCapability,
    ApproveCapabilities,
    CapabilityReceipt,
    RequestCapabilities,
    RevokeCapabilities,
)
from .execution_api import Actor, Key, Worker, result

router = APIRouter()


def capabilities(request: Request) -> CapabilityStore:
    return CapabilityStore(request.app.state.store.database_path.parent)


Capabilities = Annotated[CapabilityStore, Depends(capabilities)]


@router.post("/tasks/{task_id}/capabilities")
def request_capabilities(
    task_id: str, body: RequestCapabilities, actor: Actor, key: Key, store: Capabilities
):
    return result(store.request(actor, task_id, key, body.model_dump(mode="json")))


@router.get("/capabilities/{policy_id}")
def get_capabilities(policy_id: str, actor: Actor, store: Capabilities):
    return store.get(actor, policy_id)


@router.post("/capabilities/{policy_id}/approve")
def approve_capabilities(
    policy_id: str, body: ApproveCapabilities, actor: Actor, key: Key, store: Capabilities
):
    return result(store.approve(actor, policy_id, key, body.model_dump(mode="json")))


@router.post("/capabilities/{policy_id}/revoke")
def revoke_capabilities(
    policy_id: str, body: RevokeCapabilities, actor: Actor, key: Key, store: Capabilities
):
    return result(store.revoke(actor, policy_id, key, body.model_dump(mode="json")))


@router.post("/worker/capabilities/{policy_id}/operations")
def admit_capability(
    policy_id: str, body: AdmitCapability, actor: Worker, key: Key, store: Capabilities
):
    return result(store.admit(actor, policy_id, key, body.model_dump(mode="json")))


@router.post("/worker/capability-operations/{operation_id}/authorize")
def authorize_capability(operation_id: str, actor: Worker, store: Capabilities):
    return result(store.authorize(actor, operation_id))


@router.post("/worker/capability-operations/{operation_id}/receipt")
def report_capability(
    operation_id: str, body: CapabilityReceipt, actor: Worker, key: Key, store: Capabilities
):
    return result(store.receipt(actor, operation_id, key, body.model_dump(mode="json")))
