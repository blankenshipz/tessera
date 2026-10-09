"""Offline config/interface fixtures; no provider availability claims."""

import socket
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tessera_worker.execution_contracts import (
    ExecutionAdapter,
    ExecutionEnvelope,
    PreflightRequest,
    TargetBinding,
    TargetHealth,
)
from tessera_worker.salad_contract import SaladContractAdapter, SaladTargetConfig, SaladUnavailable


def config():
    return {
        "version": 1,
        "adapter_kind": "salad_gpu_future",
        "endpoint": "https://salad.example.invalid/api",
        "auth_reference": "credential-ref:operator-owned-salad",
        "image": "registry.example.invalid/research/mixfm@sha256:" + "a" * 64,
        "resources": {
            "cpu_cores": 4,
            "memory_mb": 16384,
            "gpu_class": "operator-selected",
            "gpu_count": 1,
            "disk_mb": 32768,
        },
        "timeouts": {
            "connect_seconds": 5,
            "request_seconds": 20,
            "startup_seconds": 300,
            "run_seconds": 600,
            "stop_seconds": 60,
        },
        "lease": {"duration_seconds": 120, "renew_every_seconds": 30},
        "labels": {"purpose": "contract-test", "owner": "fixture"},
    }


def test_disabled_and_opted_in_adapters_never_touch_network_or_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("contract-only adapter attempted network or credential access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    monkeypatch.setattr("pathlib.Path.read_text", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    disabled = SaladContractAdapter()
    assert disabled.health().status == "disabled"
    assert disabled.health().target is None
    c = SaladTargetConfig.model_validate(config())
    with pytest.raises(ValueError, match="opt_in"):
        SaladContractAdapter(c)
    with pytest.raises(ValueError):
        SaladContractAdapter(opt_in=True)
    adapter = SaladContractAdapter(c, opt_in=True)
    assert isinstance(adapter, ExecutionAdapter)
    target = c.binding()
    assert adapter.health(target).status == "unavailable"
    capabilities = adapter.capabilities(target)
    assert capabilities.preflight
    assert not any(
        (
            capabilities.submit,
            capabilities.observe,
            capabilities.cancel,
            capabilities.reconcile,
            capabilities.collect,
            capabilities.account,
        )
    )
    envelope = ExecutionEnvelope(
        "b" * 32,
        "c" * 64,
        target,
        "fixture-lease",
        1,
        datetime.now(UTC) + timedelta(seconds=60),
        {},
    )
    assert not adapter.preflight(PreflightRequest(target, envelope)).admissible
    for instance in (adapter, disabled):
        for operation in ("submit", "observe", "cancel", "reconcile", "collect", "account"):
            with pytest.raises(SaladUnavailable):
                getattr(instance, operation)(envelope)
    wrong = TargetBinding("salad_gpu_future", "1", "0" * 64)
    for operation in ("capabilities", "health"):
        with pytest.raises(SaladUnavailable, match="binding"):
            getattr(adapter, operation)(wrong)


@pytest.mark.parametrize(
    "path,value",
    [
        (("endpoint",), "http://example.invalid"),
        (("endpoint",), "https://user:secret@example.invalid"),
        (("endpoint",), "https://@example.invalid"),
        (("endpoint",), "https://.."),
        (("endpoint",), "https://example.invalid?token=secret"),
        (("endpoint",), "https://example.invalid/#fragment"),
        (("endpoint",), "https://example.invalid:bad"),
        (("endpoint",), "https://example.invalid\n"),
        (("endpoint",), "https://example.invalid\\@evil.invalid"),
        (("endpoint",), "https://other.example.invalid"),
        (("auth_reference",), "raw-secret"),
        (("auth_reference",), "env:SALAD_API_KEY"),
        (("image",), "registry.example.invalid/mixfm:latest"),
        (("image",), "registry.example.invalid/mixfm@sha256:abc"),
        (("resources", "gpu_count"), 0),
        (("resources", "gpu_count"), True),
        (("resources", "memory_mb"), 1),
        (("timeouts", "connect_seconds"), 30),
        (("timeouts", "request_seconds"), 120),
        (("timeouts", "run_seconds"), 0),
        (("lease", "renew_every_seconds"), 60),
        (("labels",), {}),
        (("labels",), {"bad key": "value"}),
        (("api_key",), "not-a-secret-fixture"),
        (("version",), 2),
    ],
)
def test_invalid_configuration_is_rejected(path, value):
    raw = config()
    parent = raw
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ValidationError):
        SaladTargetConfig.model_validate(raw)


def test_every_config_field_binds_target_and_labels_are_snapshotted():
    raw = config()
    c = SaladTargetConfig.model_validate(raw)
    adapter = SaladContractAdapter(c, opt_in=True)
    original = c.binding()
    reordered = deepcopy(raw)
    reordered["labels"] = dict(reversed(list(raw["labels"].items())))
    assert SaladTargetConfig.model_validate(reordered).binding() == original
    for path, value in [
        (("auth_reference",), "credential-ref:other"),
        (("endpoint",), "https://api.salad.com/api/public"),
        (("image",), raw["image"].replace("a" * 64, "b" * 64)),
        (("resources", "gpu_count"), 2),
        (("timeouts", "run_seconds"), 1200),
        (("lease", "duration_seconds"), 150),
        (("labels", "owner"), "other"),
    ]:
        changed = deepcopy(raw)
        parent = changed
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = value
        assert SaladTargetConfig.model_validate(changed).binding() != original
    c.labels["owner"] = "mutated"
    assert adapter.health().target == original
    with pytest.raises(ValueError):
        TargetHealth(None, "invented", "fixture")
