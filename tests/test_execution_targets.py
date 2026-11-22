from pathlib import Path

import pytest
from pydantic import ValidationError

import tessera_worker.remote_dispatch as remote_dispatch
from tessera_control import execution_targets as et
from tessera_control.remote_execution_models import ExecutionSelection
from tessera_worker import adapter_registry
from tessera_worker.remote_dispatch import RemoteDispatcher, RoutePreflight


def test_builtin_targets_registered():
    assert {"kubernetes", "salad_container", "salad_inference"} <= set(et.target_names())
    assert et.get_target("kubernetes").admission_hook is None
    assert et.get_target("salad_container").admission_hook is not None


def test_registry_rejects_unknown_and_duplicate():
    with pytest.raises(KeyError):
        et.get_target("nope")
    with pytest.raises(ValueError):
        et.register_target(et.TargetSpec("kubernetes"))
    with pytest.raises(ValidationError):
        ExecutionSelection.model_validate(dict(target="nope", target_digest="a" * 64))


def test_fake_registered_target_validates_and_hook_resolves():
    calls = []
    name = "fake_vendor_x"
    et.register_target(et.TargetSpec(name, lambda *a: calls.append(a) or {"ok": 1}))
    try:
        assert et._validate_target(name) == name
        assert et.get_target(name).admission_hook(1, 2, 3, 4) == {"ok": 1}
        assert calls == [(1, 2, 3, 4)]
    finally:
        et._TARGETS.pop(name)
    with pytest.raises(ValueError):
        et._validate_target(name)


def test_adapter_registry():
    assert {"kubernetes-job", "docker-container-v1"} <= set(adapter_registry.adapter_kinds())
    assert callable(adapter_registry.adapter_factory("kubernetes-job"))
    with pytest.raises(KeyError):
        adapter_registry.adapter_factory("nope")
    with pytest.raises(ValueError):
        adapter_registry.register_adapter("kubernetes-job", lambda: None)


def test_no_salad_reference_in_dispatch_or_route_core():
    root = Path(remote_dispatch.__file__).parent
    assert "SaladTrustedAdapter" not in (root / "remote_dispatch.py").read_text()
    core = (root.parent / "tessera_control" / "remote_execution.py").read_text()
    assert "salad_budget_binding" not in core


def _dispatch_with(adapter, monkeypatch):
    """Drive RemoteDispatcher up to the preflight decision via stubs."""

    class Sel:
        target_digest = "d"

        def model_dump(self, mode=None):
            return {"x": 1}

    class Proposal:
        selection = Sel()

    class Pkg:
        proposal = Proposal()
        org_id, project_id, task_id = "o", "p", "t"

    store = type("S", (), {})()
    store.get = lambda w, r: dict(
        proposal={}, routing_package={}, proposal_digest="x", project_id="p", task_id="t"
    )
    monkeypatch.setattr(
        remote_dispatch.RemoteProposal, "model_validate", staticmethod(lambda v: Pkg.proposal)
    )
    monkeypatch.setattr(
        remote_dispatch.RemoteExecutionPackage, "model_validate", staticmethod(lambda v: Pkg)
    )
    monkeypatch.setattr(remote_dispatch, "canonical_digest", lambda v: "x")
    d = RemoteDispatcher(store, {"org_id": "o"}, {"d": adapter})
    d.worker = {"org_id": "o"}
    with pytest.raises(remote_dispatch.Conflict, match="preflight blocked"):
        d.dispatch("r", "create")


def test_dispatcher_prefers_preflight_for(monkeypatch):
    seen = []

    class A:
        def preflight(self, proposal):
            seen.append("preflight")
            return RoutePreflight(False, "x")

        def preflight_for(self, action, proposal):
            seen.append(("for", action))
            return RoutePreflight(False, "x")

    _dispatch_with(A(), monkeypatch)
    assert seen == [("for", "create")]


def test_dispatcher_falls_back_to_preflight(monkeypatch):
    seen = []

    class A:
        def preflight(self, proposal):
            seen.append("preflight")
            return RoutePreflight(False, "x")

    _dispatch_with(A(), monkeypatch)
    assert seen == ["preflight"]


def test_paid_salad_targets_always_carry_the_budget_hook():
    from tessera_control.execution_targets import (
        TargetSpec,
        get_target,
        register_target,
        require_admission_hook,
    )

    for name in ("salad_container", "salad_inference"):
        spec = get_target(name)
        assert spec.billable and spec.admission_hook is not None
        assert require_admission_hook(name) is spec.admission_hook
    with pytest.raises(ValueError, match="no admission hook"):
        require_admission_hook("kubernetes")
    with pytest.raises(ValueError, match="requires an admission hook"):
        register_target(TargetSpec("unhooked-paid-vendor", billable=True))
