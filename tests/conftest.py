"""Shared test configuration.

Approval-trust policy (see ``tessera_control.approval_trust``) refuses the root
``local_owner`` credential for human decisions unless the caller is labelled
owner-operated automation. Most suites drive decisions with the owner credential
as a convenience, so by default every owner authentication in the test process is
labelled ``pytest``. Tests that exercise the policy itself opt out with
``@pytest.mark.real_owner_trust`` and then see the production behaviour.
"""

from __future__ import annotations

import pytest

from tessera_control.approval_trust import AUTOMATION_KEY
from tessera_control.store import Store


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_owner_trust: do not auto-label owner credentials as test automation"
    )


@pytest.fixture(autouse=True)
def _label_owner_as_test_automation(request, monkeypatch):
    if request.node.get_closest_marker("real_owner_trust"):
        return
    original = Store.authenticate

    def authenticate(self, token):
        actor = original(self, token)
        if actor.get("kind") == "local_owner" and not actor.get(AUTOMATION_KEY):
            actor[AUTOMATION_KEY] = "pytest"
        return actor

    monkeypatch.setattr(Store, "authenticate", authenticate)


@pytest.fixture(autouse=True)
def _provider_trust_default():
    """Every test starts and ends on the fail-closed builtin provider trust setting."""
    from tessera_control.provider_trust import reset_policy

    reset_policy()
    yield
    reset_policy()


@pytest.fixture
def trust_policy():
    """Install a trust setting: ``trust_policy({provider: (classes, [worker ids])})``."""
    from provider_trust_fixture import install_trust

    return install_trust


@pytest.fixture
def trust_any_worker():
    """Install a trust setting that binds every worker principal to one provider."""
    from provider_trust_fixture import install_trust_any_worker

    return install_trust_any_worker


@pytest.fixture(autouse=True)
def _campaign_providers_default():
    """Provider registrations are process-wide: restore the defaults after every test."""
    yield
    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_provider_salad import register_provider_instance
    from tessera_control.campaign_providers import unregister_provider

    register_provider_instance(None)
    for provider_id in sorted(worker._REGISTERED - {worker.LOCAL_DOCKER_PROVIDER}):
        unregister_provider(provider_id)
    worker._REGISTERED.intersection_update({worker.LOCAL_DOCKER_PROVIDER})
    worker.register_from_config(None)
