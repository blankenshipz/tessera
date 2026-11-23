"""Replacing a registered provider is replace-or-keep; a refused entry changes nothing."""

from __future__ import annotations

import dataclasses

import pytest

from tessera_control import campaign_provider_salad as salad
from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as providers
from tessera_control.campaign_providers import (
    OutputLimits,
    ProviderDescriptor,
    get_provider,
    register_provider,
    replace_provider,
    unregister_provider,
)

_BAD_LIMITS = OutputLimits(0, 1024, 1024)  # max_files must be positive


@pytest.fixture(autouse=True)
def _restore_registry():
    saved = dict(providers._PROVIDERS)
    worker_reg = set(worker._REGISTERED)
    salad_reg = list(salad._REGISTERED)
    yield
    providers._PROVIDERS.clear()
    providers._PROVIDERS.update(saved)
    worker._REGISTERED.clear()
    worker._REGISTERED.update(worker_reg)
    salad._REGISTERED[:] = salad_reg


def _config(*ids: str, slots: int = 1) -> dict:
    return {
        "local_docker": {"worker_principal_id": "local-principal", "worker_slots": slots},
        "self_hosted": [{"id": i, "worker_principal_id": f"p-{i}"} for i in ids],
    }


def _break(monkeypatch, bad_id: str) -> None:
    real = worker._descriptor

    def fake(provider_id, *args, **kwargs):
        d = real(provider_id, *args, **kwargs)
        if provider_id == bad_id:
            return dataclasses.replace(d, output_limits=_BAD_LIMITS)
        return d

    monkeypatch.setattr(worker, "_descriptor", fake)


def test_worker_refused_replacement_keeps_old_instance(monkeypatch):
    worker.register_from_config(_config("repl-a"))
    old = get_provider("repl-a")
    _break(monkeypatch, "repl-a")
    with pytest.raises(ValueError, match="output limit"):
        worker._replace("repl-a", worker.WorkerGrantProvider(worker._descriptor("repl-a", "x")))
    assert get_provider("repl-a") is old
    assert old.descriptor.worker_principal_id == "p-repl-a"


def test_bad_entry_in_multi_entry_config_changes_nothing(monkeypatch):
    worker.register_from_config(_config("repl-a", "repl-b"))
    old = {i: get_provider(i) for i in (worker.LOCAL_DOCKER_PROVIDER, "repl-a", "repl-b")}
    before = {i: p.descriptor for i, p in old.items()}
    _break(monkeypatch, "repl-b")
    with pytest.raises(ValueError, match="output limit"):
        worker.register_from_config(_config("repl-a", "repl-b", slots=4))
    for i, p in old.items():
        assert get_provider(i) is p
        assert p.descriptor == before[i]
    assert get_provider(worker.LOCAL_DOCKER_PROVIDER).descriptor.worker_slots == 1


def test_good_replacement_still_swaps():
    worker.register_from_config(_config("repl-a"))
    old = get_provider("repl-a")
    worker.register_from_config(_config("repl-a", slots=3))
    assert get_provider("repl-a") is not old
    assert get_provider(worker.LOCAL_DOCKER_PROVIDER).descriptor.worker_slots == 3


def test_register_provider_still_refuses_duplicates():
    worker.register_from_config(_config("repl-a"))
    old = get_provider("repl-a")
    with pytest.raises(ValueError, match="already registered"):
        register_provider(worker.WorkerGrantProvider(worker._descriptor("repl-a", "x")))
    assert get_provider("repl-a") is old


def test_foreign_owner_of_id_still_refused():
    foreign = worker.WorkerGrantProvider(worker._descriptor("repl-foreign", "x"))
    register_provider(foreign)
    try:
        with pytest.raises(ValueError, match="already registered"):
            worker.register_from_config(_config("repl-foreign"))
        assert get_provider("repl-foreign") is foreign
    finally:
        unregister_provider("repl-foreign")


def test_replace_provider_checks_before_swapping():
    worker.register_from_config(_config("repl-a"))
    old = get_provider("repl-a")
    bad = worker.WorkerGrantProvider(
        dataclasses.replace(worker._descriptor("repl-a", "x"), delegable_steps=frozenset({"nope"}))
    )
    with pytest.raises(ValueError, match="unknown steps"):
        replace_provider(bad)
    assert get_provider("repl-a") is old


def test_salad_refused_replacement_keeps_old_instance(monkeypatch):
    salad.register_provider_instance(None)
    old = get_provider(salad.PROVIDER_ID)
    bad = dataclasses.replace(old.descriptor, output_limits=_BAD_LIMITS)
    monkeypatch.setattr(salad.SaladRouteProvider, "descriptor", bad)
    with pytest.raises(ValueError, match="output limit"):
        salad.register_provider_instance(None)
    assert get_provider(salad.PROVIDER_ID) is old


def test_salad_good_replacement_swaps():
    salad.register_provider_instance(None)
    old = get_provider(salad.PROVIDER_ID)
    new = salad.register_provider_instance(None)
    assert new is not old
    assert get_provider(salad.PROVIDER_ID) is new
    assert isinstance(old.descriptor, ProviderDescriptor)


def test_mains_validation_is_unchanged_and_still_runs_before_any_swap():
    """The registration checks main has (provider object shape, worker slots, scratch bound) all
    run on a replacement before the registry entry changes; a refusal keeps the old provider."""
    from types import SimpleNamespace

    worker.register_from_config(_config("repl-a"))
    old = get_provider("repl-a")
    good = worker._descriptor("repl-a", "x")
    not_a_provider = SimpleNamespace(descriptor=good)  # no ComputeProvider methods
    with pytest.raises(ValueError, match="not a ComputeProvider"):
        replace_provider(not_a_provider)
    for bad_descriptor, match in (
        (dataclasses.replace(good, worker_slots=0), "worker_slots"),
        (dataclasses.replace(good, max_scratch_bytes=0), "max_scratch_bytes"),
    ):
        with pytest.raises(ValueError, match=match):
            replace_provider(worker.WorkerGrantProvider(bad_descriptor))
    assert get_provider("repl-a") is old
    with pytest.raises(ValueError, match="not a ComputeProvider"):
        register_provider(not_a_provider)  # a fresh id is refused by the same check
