from __future__ import annotations

from tessera_control.activation_gates import (
    DEFAULT_PROVIDER_ACTIVATION,
    CredentialLifecycle,
    credential_is_usable,
    provider_enabled_status,
)


def test_default_provider_contract_is_disabled_with_unknown_usage_and_billing() -> None:
    status = DEFAULT_PROVIDER_ACTIVATION

    assert status.provider_enabled is False
    assert status.provider_status == "disabled"
    assert status.usage_status == "unknown"
    assert status.billing_mode == "unknown"
    assert status.api_key_fallback is False


def test_unverified_fixture_claim_cannot_activate_a_provider() -> None:
    assert provider_enabled_status(live_fixture_recorded=False) is DEFAULT_PROVIDER_ACTIVATION
    assert provider_enabled_status(live_fixture_recorded=True) is DEFAULT_PROVIDER_ACTIVATION


def test_only_active_owner_bound_credential_is_usable() -> None:
    assert credential_is_usable(CredentialLifecycle.ACTIVE)
    for lifecycle in (
        CredentialLifecycle.ABSENT,
        CredentialLifecycle.EXPIRED,
        CredentialLifecycle.REVOKED,
        CredentialLifecycle.OWNER_MISMATCH,
    ):
        assert not credential_is_usable(lifecycle)
