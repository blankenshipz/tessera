"""A CPU-only group runs at Salad's lowest priority, so its readback never requires ``high``;
a GPU group still requires its configured priority."""

import pytest
from test_salad_http import GPU, ID, MARKER, Factory, group, selection

from tessera_control.remote_execution_models import ExecutionSelection
from tessera_control.salad_http import SaladHTTPTransport
from tessera_control.salad_secrets import SecretBuffer

RATE = {"vcpu_hour_usd": "0.005", "memory_gb_hour_usd": "0.001"}


def cpu_selection() -> ExecutionSelection:
    raw = selection().model_dump(mode="json")
    raw["resources"] |= {"gpu_count": 0, "gpu_class": None}
    raw["salad"]["execution"]["cpu_rate"] = RATE
    return ExecutionSelection.model_validate(raw)


def cpu_group(**changes):
    data = group()
    data["container"]["resources"]["gpu_classes"] = []
    data.update(changes)
    return data


def read(chosen, data):
    secret = SecretBuffer(bytearray(MARKER))
    with secret:
        return SaladHTTPTransport(chosen, connection_factory=Factory((200, data))).send(
            "read_group", chosen, ID, secret
        )


@pytest.mark.parametrize("level", ["high", "batch"])
def test_cpu_group_readback_accepts_the_sent_or_the_lowest_priority(level):
    result = read(cpu_selection(), cpu_group(priority=level))
    assert result.status == "observed", result.reason


def test_cpu_group_readback_accepts_a_null_or_absent_priority():
    assert read(cpu_selection(), cpu_group(priority=None)).status == "observed"
    data = cpu_group()
    del data["priority"]
    assert read(cpu_selection(), data).status == "observed"


@pytest.mark.parametrize("level", ["low", "medium", "urgent"])
def test_cpu_group_readback_still_refuses_an_unknown_priority(level):
    result = read(cpu_selection(), cpu_group(priority=level))
    assert result.reason == "identity_mismatch"


@pytest.mark.parametrize("level", ["batch", None, "low"])
def test_gpu_group_readback_still_requires_its_configured_priority(level):
    data = group()
    data["priority"] = level
    assert GPU in data["container"]["resources"]["gpu_classes"]
    assert read(selection(), data).reason == "identity_mismatch"
