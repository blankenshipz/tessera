"""Pure, fake-only provisioning contracts for the Phase 1 environment slice."""

from .contracts import CONTRACT_VERSION, ProvisioningContractError, ProvisioningPlan, ResourceIntent
from .fake_adapter import FakeProvisioningAdapter, FakeProvisioningOperator
from .outcome0_bootstrap import (
    BootstrapPlan,
    FakeOutcome0BootstrapAdapter,
    Outcome0BootstrapStore,
)

__all__ = [
    "CONTRACT_VERSION",
    "FakeProvisioningAdapter",
    "FakeProvisioningOperator",
    "FakeOutcome0BootstrapAdapter",
    "Outcome0BootstrapStore",
    "BootstrapPlan",
    "ProvisioningContractError",
    "ProvisioningPlan",
    "ResourceIntent",
]
