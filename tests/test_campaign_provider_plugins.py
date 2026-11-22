"""Provider plugin loading and descriptor validation; fakes only."""

from __future__ import annotations

import dataclasses
import sys
import textwrap
from types import SimpleNamespace

import pytest
from test_campaign_providers import _descriptor, _Fake

from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as cp
from tessera_control.campaign_providers import (
    OutputLimits,
    ProviderHooks,
    get_provider,
    register_provider,
    unregister_provider,
)


def _bad(**changes):
    return _Fake(dataclasses.replace(_descriptor("bad-cpu"), **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "Bad_ID"},
        {"provider_id": ""},
        {"kind": "Not A Kind"},
        {"runtimes": frozenset()},
        {"target": "some-target"},  # a worker grant has no route target
        {"kind": "route_vendor", "target": None},  # a route kind needs its target
        {"output_limits": OutputLimits(0, 10, 10)},
        {"output_limits": OutputLimits(1, -5, 10)},
        {"output_limits": OutputLimits(1, 10, 20)},
        {"output_limits": OutputLimits(True, 10, 10)},
        {"billable": "yes"},
        {"worker_principal_id": None},
    ],
)
def test_malformed_descriptor_is_refused_at_registration(changes):
    with pytest.raises(ValueError):
        register_provider(_bad(**changes))
    with pytest.raises(KeyError):
        get_provider(changes.get("provider_id") or "bad-cpu")


def test_a_new_route_kind_is_accepted_without_core_edits():
    provider = _bad(provider_id="vendor-x", kind="route_vendor", target="vendor-x-target")
    register_provider(provider)
    try:
        assert get_provider("vendor-x").descriptor.kind == "route_vendor"
    finally:
        unregister_provider("vendor-x")


def test_hooks_protocol_matches_the_salad_provider():
    from tessera_control.campaign_provider_salad import SaladRouteProvider

    assert issubclass(SaladRouteProvider, ProviderHooks)
    assert not isinstance(get_provider("local-docker"), ProviderHooks)


PLUGIN_SOURCE = textwrap.dedent(
    """
    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_providers import register_provider

    LOADED = []

    def register_from_config(section):
        LOADED.append(section)
        for item in section["instances"]:
            register_provider(worker.WorkerGrantProvider(worker._descriptor(
                item["id"], item["principal"])))
    """
)


@pytest.fixture
def plugin_module(tmp_path, monkeypatch):
    (tmp_path / "tessera_test_vendor_plugin.py").write_text(PLUGIN_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "tessera_test_vendor_plugin", raising=False)
    yield "tessera_test_vendor_plugin"
    for pid in ("vendor-a", "vendor-b"):
        unregister_provider(pid)
        cp._PLUGIN_MODULES.pop(pid, None)
    sys.modules.pop("tessera_test_vendor_plugin", None)


def test_a_module_named_in_the_operator_file_registers_its_providers(plugin_module):
    cp.configure_providers(
        {"plugins": {plugin_module: {"instances": [{"id": "vendor-a", "principal": "w-a"}]}}}
    )
    assert get_provider("vendor-a").descriptor.worker_principal_id == "w-a"
    assert cp.plugin_of("vendor-a") == plugin_module
    assert cp.plugin_of("local-docker") is None


def test_an_installed_module_that_the_file_does_not_name_is_never_loaded(plugin_module):
    cp.configure_providers(None)
    assert plugin_module not in sys.modules
    with pytest.raises(KeyError):
        get_provider("vendor-a")


def test_entry_point_plugins_load_by_name(plugin_module, monkeypatch):
    module = __import__(plugin_module)
    entry = SimpleNamespace(name="vendor-ep", load=lambda: module)
    monkeypatch.setattr(
        cp.importlib.metadata,
        "entry_points",
        lambda group: [entry] if group == cp.ENTRY_POINT_GROUP else [],
    )
    cp.load_plugin("vendor-ep", {"instances": [{"id": "vendor-b", "principal": "w-b"}]})
    assert get_provider("vendor-b").descriptor.worker_principal_id == "w-b"


def test_a_missing_or_incomplete_plugin_is_a_clear_error(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not installed"):
        cp.load_plugin("tessera_no_such_plugin", {})
    with pytest.raises(ValueError, match="Invalid provider plugin name"):
        cp.load_plugin("../evil", {})
    (tmp_path / "tessera_empty_plugin.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ValueError, match="no register_from_config"):
        cp.load_plugin("tessera_empty_plugin", {})
    sys.modules.pop("tessera_empty_plugin", None)


def test_a_plugin_cannot_take_a_builtin_id(plugin_module):
    with pytest.raises(ValueError, match="already registered"):
        cp.configure_providers(
            {"plugins": {plugin_module: {"instances": [{"id": "local-docker", "principal": "x"}]}}}
        )
    assert get_provider("local-docker").descriptor.provider_id == "local-docker"


def test_the_providers_file_model_accepts_a_plugins_section():
    parsed = worker.ProvidersFile.model_validate({"plugins": {"m": {"a": 1}}})
    assert parsed.plugins == {"m": {"a": 1}}


@pytest.mark.parametrize(
    "changes",
    [{"runtimes": ["docker-cpu-v1"]}, {"delegable_steps": ["create_task"]}],
)
def test_descriptor_collection_types_are_value_errors_not_type_errors(changes):
    with pytest.raises(ValueError):
        register_provider(_bad(**changes))


def test_a_billable_provider_must_declare_third_party():
    with pytest.raises(ValueError, match="third_party"):
        register_provider(_bad(billable=True, third_party=False))


def test_a_plugin_that_fails_midway_leaves_no_registration(tmp_path, monkeypatch):
    (tmp_path / "tessera_half_plugin.py").write_text(
        "from tessera_control import campaign_provider_worker as worker\n"
        "from tessera_control.campaign_providers import register_provider\n"
        "def register_from_config(section):\n"
        "    register_provider(worker.WorkerGrantProvider(worker._descriptor('half-a', 'w')))\n"
        "    raise RuntimeError('boom')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RuntimeError):
        cp.load_plugin("tessera_half_plugin", {})
    with pytest.raises(KeyError):
        get_provider("half-a")
    sys.modules.pop("tessera_half_plugin", None)
