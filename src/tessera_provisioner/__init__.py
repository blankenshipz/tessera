"""Pure, fake-only provisioning contracts for the Phase 1 environment slice."""

from .contracts import CONTRACT_VERSION, ProvisioningContractError, ProvisioningPlan, ResourceIntent
from .fake_adapter import FakeProvisioningAdapter, FakeProvisioningOperator

__all__ = [
    "CONTRACT_VERSION",
    "FakeProvisioningAdapter",
    "FakeProvisioningOperator",
    "ProvisioningContractError",
    "ProvisioningPlan",
    "ResourceIntent",
]
