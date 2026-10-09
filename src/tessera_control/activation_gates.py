"""Pure declarations for capabilities that are deliberately not activated.

These constants are a policy contract for documentation and default tests.  They
do not discover credentials, contact a provider, or add organization roles to the
local control service.  In particular, changing a configuration value cannot turn
an unverified provider or collaboration fixture into a supported capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CollaborationRole(StrEnum):
    """Prospective organization roles; none is enrolled in local single-owner mode."""

    RESEARCHER = "researcher"
    APPROVER = "approver"
    REVIEWER = "reviewer"
    OPERATOR = "operator"


class CollaborationAction(StrEnum):
    DRAFT_PLAN = "draft_plan"
    APPROVE_EXECUTION = "approve_execution"
    REVIEW_EVIDENCE = "review_evidence"
    OPERATE_INFRASTRUCTURE = "operate_infrastructure"


ROLE_ACTIONS: dict[CollaborationRole, frozenset[CollaborationAction]] = {
    CollaborationRole.RESEARCHER: frozenset({CollaborationAction.DRAFT_PLAN}),
    CollaborationRole.APPROVER: frozenset({CollaborationAction.APPROVE_EXECUTION}),
    CollaborationRole.REVIEWER: frozenset({CollaborationAction.REVIEW_EVIDENCE}),
    CollaborationRole.OPERATOR: frozenset({CollaborationAction.OPERATE_INFRASTRUCTURE}),
}
"""Minimum separation of duties for a future organization implementation."""


class CredentialLifecycle(StrEnum):
    ABSENT = "absent"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    OWNER_MISMATCH = "owner_mismatch"


def credential_is_usable(lifecycle: CredentialLifecycle) -> bool:
    """Only an active, owner-bound credential may be used by a future adapter."""

    return lifecycle is CredentialLifecycle.ACTIVE


@dataclass(frozen=True)
class ProviderActivationStatus:
    """The only truthful default provider status for this repository."""

    provider_enabled: bool = False
    provider_status: str = "disabled"
    usage_status: str = "unknown"
    billing_mode: str = "unknown"
    api_key_fallback: bool = False


DEFAULT_PROVIDER_ACTIVATION = ProviderActivationStatus()
"""No selected adapter, credential custody, or live receipt exists by default."""


def provider_enabled_status(*, live_fixture_recorded: bool) -> ProviderActivationStatus:
    """Return the default status without treating a requested fixture as evidence.

    A real implementation must replace this contract only after it has persisted
    the selected fixture's pinned identity, credential-custody, bounded-spend,
    cancellation, and attributable-usage evidence.  This source-only declaration
    intentionally remains disabled even when a caller says a fixture was recorded.
    """

    del live_fixture_recorded
    return DEFAULT_PROVIDER_ACTIVATION
