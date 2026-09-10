"""Strict Phase 1 environment target and plan-request models.

They deliberately remain separate from local CPU execution profile and database
tables.  Target identity is supplied as a digest; no kubeconfig, Secret, current
context, namespace, or PVC appears in this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tessera_provisioner.contracts import (
    ProvisioningContractError,
    ProvisioningPlan,
    ResourceIntent,
)


@dataclass(frozen=True)
class EnvironmentActor:
    id: str
    tenant_id: str
    role: Literal["human", "agent"]


@dataclass(frozen=True)
class EnvironmentTarget:
    id: str
    tenant_id: str
    revision: int
    identity_digest: str
    adapter_version: str = "environment-adapter-v1"


@dataclass(frozen=True)
class EnvironmentPlanRequest:
    target_id: str
    expected_target_revision: int
    expires_at: str
    resource_intents: tuple[ResourceIntent, ...] = ()
    namespace: None = None
    persistent_volume_claim: None = None

    def __post_init__(self) -> None:
        if self.namespace is not None or self.persistent_volume_claim is not None:
            raise ProvisioningContractError(
                "Phase 1 requests must omit namespace and PVC selection."
            )


def resolve_environment_plan(
    target: EnvironmentTarget, request: EnvironmentPlanRequest
) -> ProvisioningPlan:
    """Purely resolve one explicit selected target into its canonical plan."""
    if target.id != request.target_id:
        raise ProvisioningContractError(
            "Environment plan target does not match the explicit selection."
        )
    if target.revision != request.expected_target_revision:
        raise ProvisioningContractError("Environment target revision is stale.")
    return ProvisioningPlan.create(
        tenant_id=target.tenant_id,
        target_id=target.id,
        target_revision=target.revision,
        target_identity_digest=target.identity_digest,
        expires_at=request.expires_at,
        resource_intents=request.resource_intents,
    )


__all__ = [
    "EnvironmentActor",
    "EnvironmentPlanRequest",
    "EnvironmentTarget",
    "resolve_environment_plan",
]
