"""Replacing a provider validates the replacement first; salad_route requires third_party."""

from __future__ import annotations

import pytest

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import (
    REQUIRED_PROVIDER_METHODS,
    OutputLimits,
    ProviderDescriptor,
    get_provider,
    provider_ids,
    register_provider,
    replace_provider,
    unregister_provider,
    validate_descriptor,
)


def _descriptor(provider_id: str = "prf-fake", **over) -> ProviderDescriptor:
    fields = {
        "provider_id": provider_id,
        "kind": "worker_grant",
        "runtimes": frozenset({"docker-cpu-v1"}),
        "target": None,
        "billable": False,
        "third_party": False,
        "delegable_steps": frozenset(worker.WORKER_STEPS),
        "output_limits": OutputLimits(8, 1024 * 1024, 1024 * 1024),
        "worker_principal_id": "",
    }
    fields.update(over)
    return ProviderDescriptor(**fields)


class _P:
    config_model = None

    def __init__(self, descriptor):
        self.descriptor = descriptor


for _name in REQUIRED_PROVIDER_METHODS:
    setattr(_P, _name, lambda self, *a, **k: None)


@pytest.fixture(autouse=True)
def _clean():
    before = provider_ids()
    yield
    for pid in provider_ids():
        if pid not in before:
            unregister_provider(pid)


def _salad(**over):
    fields = {
        "kind": "salad_route",
        "target": "salad_container",
        "billable": True,
        "third_party": True,
    }
    fields.update(over)
    return _descriptor(**fields)


def test_invalid_replacement_keeps_old_provider():
    old = _P(_descriptor(worker_slots=2))
    register_provider(old)
    bad = _P(_descriptor(worker_slots=0))
    with pytest.raises(ValueError, match="worker_slots"):
        replace_provider(bad)
    assert get_provider("prf-fake") is old
    assert get_provider("prf-fake").descriptor.worker_slots == 2


def test_replacement_missing_method_keeps_old_provider():
    old = _P(_descriptor())
    register_provider(old)
    bad = _P(_descriptor())
    bad.observe = 5
    with pytest.raises(ValueError, match="observe is not callable"):
        replace_provider(bad)
    assert get_provider("prf-fake") is old


def test_valid_replacement_swaps():
    old = _P(_descriptor(worker_slots=1))
    register_provider(old)
    new = _P(_descriptor(worker_slots=3))
    assert replace_provider(new) is new
    assert get_provider("prf-fake") is new
    assert provider_ids().count("prf-fake") == 1


def test_worker_replace_failure_keeps_registered_instance_and_set(monkeypatch):
    worker.register_from_config({"self_hosted": [{"id": "prf-host", "worker_principal_id": "p"}]})
    old = get_provider("prf-host")
    monkeypatch.setattr(worker, "_REGISTERED", set(worker._REGISTERED))
    bad = _P(_descriptor("prf-host", worker_slots=0))
    with pytest.raises(ValueError, match="worker_slots"):
        worker._replace("prf-host", bad)
    assert get_provider("prf-host") is old
    assert "prf-host" in worker._REGISTERED


def test_salad_route_third_party_true_accepted():
    validate_descriptor(_salad())


@pytest.mark.parametrize("value", [False, None, "yes", 1, 0])
def test_salad_route_without_third_party_true_refused(value):
    with pytest.raises(ValueError, match="third_party"):
        validate_descriptor(_salad(third_party=value))


def test_salad_route_with_third_party_absent_refused():
    d = _salad()
    object.__setattr__(d, "third_party", None)
    with pytest.raises(ValueError, match="third_party"):
        validate_descriptor(d)


def test_other_kinds_unaffected():
    validate_descriptor(_descriptor(third_party=False))
    validate_descriptor(_descriptor(third_party=True))
