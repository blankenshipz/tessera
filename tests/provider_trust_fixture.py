"""Test helpers for the provider trust setting (ADR 0040 section 4).

``conftest`` resets the process-wide setting to the fail-closed builtin default around
every test, so a test that needs a private class installs a policy that binds its worker
principal to the provider it exercises::

    def test_x(trust_policy):
        trust_policy({"local-docker": (ALL_CLASSES, [worker_id])})
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from tessera_control.provider_trust import TrustPolicy, install_policy, policy_from_document

ALL_CLASSES = ["public", "private_code_approved", "private_derived"]
DERIVED = ["public", "private_derived"]
CODE = ["public", "private_code_approved"]


def build_policy(providers: Mapping[str, tuple[Iterable[str], Iterable[str]]]) -> TrustPolicy:
    """``{provider_id: (data_classes, worker_principal_ids)}`` as a validated policy."""
    document = {
        "schema": "tessera.provider-trust.v1",
        "providers": {
            provider_id: {
                "data_classes": list(classes),
                "worker_principal_ids": list(principals),
            }
            for provider_id, (classes, principals) in providers.items()
        },
    }
    return policy_from_document(document, "file")


def install_trust(providers: Mapping[str, tuple[Iterable[str], Iterable[str]]]) -> TrustPolicy:
    policy = build_policy(providers)
    install_policy(policy)
    return policy


class _AnyWorkerPolicy(TrustPolicy):
    """Test-only: every worker principal counts as bound to one provider.

    For flows where the principal is created inside the code under test (a setup script
    that enrols its own executor), so its id cannot be listed beforehand.
    """

    bound_provider: str = ""
    bound_classes: tuple[str, ...] = ()

    def allowed(self, provider_id, worker_principal_id):
        if provider_id == self.bound_provider and isinstance(worker_principal_id, str):
            return frozenset({"public", *self.bound_classes})
        return frozenset({"public"})

    def provider_for_worker(self, worker_principal_id):
        return self.bound_provider


def install_trust_any_worker(provider_id: str, classes: Iterable[str]) -> TrustPolicy:
    base = build_policy({provider_id: (classes, [])})
    policy = _AnyWorkerPolicy(base.digest, base.document, base.source)
    object.__setattr__(policy, "bound_provider", provider_id)
    object.__setattr__(policy, "bound_classes", tuple(classes))
    install_policy(policy)
    return policy
