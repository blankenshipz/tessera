"""Registration refuses a malformed provider descriptor or provider object, atomically."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel

from tessera_control import campaign_provider_salad as salad
from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import (
    MAX_SCRATCH_BYTES,
    MAX_WORKER_SLOTS,
    OPTIONAL_PROVIDER_HOOKS,
    REQUIRED_PROVIDER_METHODS,
    ComputeProvider,
    OutputLimits,
    ProviderDescriptor,
    ProviderHooks,
    provider_ids,
    register_provider,
    unregister_provider,
)


def _descriptor(provider_id: str = "pdv-fake", **over) -> ProviderDescriptor:
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


class _Good:
    config_model = None

    def __init__(self, descriptor):
        self.descriptor = descriptor


for _name in REQUIRED_PROVIDER_METHODS:
    setattr(_Good, _name, lambda self, *a, **k: None)


def _good(**over):
    return _Good(_descriptor(**over))


@pytest.fixture(autouse=True)
def _clean():
    before = provider_ids()
    yield
    for pid in provider_ids():
        if pid not in before:
            unregister_provider(pid)


def _refused(provider, match):
    before = provider_ids()
    with pytest.raises(ValueError, match=match):
        register_provider(provider)
    assert provider_ids() == before


def test_good_provider_registers():
    p = _good()
    assert register_provider(p) is p
    assert "pdv-fake" in provider_ids()


def test_descriptor_subclass_refused():
    @dataclasses.dataclass(frozen=True)
    class Sub(ProviderDescriptor):
        extra: int = 1

    base = _descriptor()
    sub = Sub(**dataclasses.asdict(base))
    _refused(_Good(sub), "exactly ProviderDescriptor")


@pytest.mark.parametrize("slots", [0, 9, True, "2", 1.0, -1])
def test_worker_slots_refused(slots):
    _refused(_good(worker_slots=slots), "worker_slots")


@pytest.mark.parametrize("slots", [1, MAX_WORKER_SLOTS])
def test_worker_slots_bounds_accepted(slots):
    register_provider(_good(worker_slots=slots))


@pytest.mark.parametrize("scratch", [0, -1, True, MAX_SCRATCH_BYTES + 1, "64", 1.5])
def test_max_scratch_bytes_refused(scratch):
    _refused(_good(max_scratch_bytes=scratch), "max_scratch_bytes")


def test_max_scratch_bytes_ceiling_accepted():
    register_provider(_good(max_scratch_bytes=MAX_SCRATCH_BYTES))


def test_worker_operator_file_shares_ceilings():
    with pytest.raises(ValueError):
        worker.LocalDockerConfig(worker_slots=MAX_WORKER_SLOTS + 1)


def test_missing_method_refused():
    class Bare:
        config_model = None

    q = Bare()
    q.descriptor = _descriptor()
    for n in REQUIRED_PROVIDER_METHODS:
        if n != "settle":
            setattr(q, n, lambda *a, **k: None)
    _refused(q, "missing method settle")


def test_non_callable_method_refused():
    p = _good()
    p.observe = 5
    _refused(p, "observe is not callable")


@pytest.mark.parametrize("model", [int, "x", object(), dict])
def test_bad_config_model_refused(model):
    p = _good()
    p.config_model = model
    _refused(p, "config_model")


def test_missing_config_model_refused():
    p = _good()
    del_cls = type("NoModel", (), {})
    q = del_cls()
    q.descriptor = p.descriptor
    for n in REQUIRED_PROVIDER_METHODS:
        setattr(q, n, lambda *a, **k: None)
    _refused(q, "missing config_model")


def test_pydantic_config_model_accepted():
    class M(BaseModel):
        pass

    p = _good()
    p.config_model = M
    register_provider(p)


@pytest.mark.parametrize("hook", OPTIONAL_PROVIDER_HOOKS)
def test_non_callable_optional_hook_refused(hook):
    p = _good()
    setattr(p, hook, "nope")
    _refused(p, f"optional hook {hook} is not callable")


def test_callable_optional_hooks_accepted():
    p = _good()
    for hook in OPTIONAL_PROVIDER_HOOKS:
        setattr(p, hook, lambda *a, **k: None)
    register_provider(p)


def test_all_problems_listed_together():
    p = _good()
    p.observe = None
    p.config_model = 3
    p.sweep = 1
    with pytest.raises(ValueError) as err:
        register_provider(p)
    text = str(err.value)
    assert "observe is not callable" in text
    assert "config_model" in text
    assert "optional hook sweep" in text


def test_registry_unchanged_after_refusal_then_good_registers():
    before = provider_ids()
    bad = _good()
    bad.cancel = 0
    with pytest.raises(ValueError):
        register_provider(bad)
    assert provider_ids() == before
    register_provider(_good())  # same id is free again


def test_builtin_providers_register():
    worker.register_from_config(
        {"self_hosted": [{"id": "pdv-self-hosted", "worker_principal_id": "worker-a"}]}
    )
    salad.register_from_config(None)
    try:
        for pid in ("local-docker", "pdv-self-hosted", "salad"):
            assert pid in provider_ids()
    finally:
        worker.register_from_config(None)
        unregister_provider("pdv-self-hosted")


def test_required_methods_match_protocol():
    def public_callables(proto):
        return {
            n for n in dir(proto) if not n.startswith("_") and callable(getattr(proto, n, None))
        }

    assert set(REQUIRED_PROVIDER_METHODS) == public_callables(ComputeProvider)
    assert set(OPTIONAL_PROVIDER_HOOKS) - {"placement_capability"} == public_callables(
        ProviderHooks
    )
