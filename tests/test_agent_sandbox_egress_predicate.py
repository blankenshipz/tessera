"""Control and worker agree on the egress-mode predicate; a half-marked package fails closed."""

import pytest
from test_agent_sandbox import durable_outputs
from test_agent_sandbox_egress import LOG, created, finish, setup_egress

from tessera_control import agent_sandbox as control
from tessera_control.agent_sandbox import AgentSandboxStore
from tessera_control.store import Conflict
from tessera_worker import agent_sandbox as worker
from tessera_worker.agent_sandbox import EGRESS_NETWORK_PROFILE, EGRESS_PROFILE_VERSION

BOTH = {"profile_version": EGRESS_PROFILE_VERSION, "network_profile": EGRESS_NETWORK_PROFILE}
NEITHER = {}
ONLY_VERSION = {"profile_version": EGRESS_PROFILE_VERSION}
ONLY_PROFILE = {"network_profile": EGRESS_NETWORK_PROFILE}


def test_both_markers_is_egress_in_worker_and_control():
    assert worker.is_egress_package(BOTH) is True
    assert control._is_egress(BOTH) is True


def test_neither_marker_is_network_none_in_worker_and_control():
    assert worker.is_egress_package(NEITHER) is False
    assert control._is_egress(NEITHER) is False
    assert control._egress(NEITHER) is None


@pytest.mark.parametrize("package", [ONLY_VERSION, ONLY_PROFILE])
def test_one_marker_is_refused_by_worker_and_control(package):
    with pytest.raises(ValueError, match="inconsistent egress profile markers"):
        worker.is_egress_package(package)
    with pytest.raises(Conflict, match="inconsistent egress markers"):
        control._is_egress(package)
    with pytest.raises(Conflict, match="inconsistent egress markers"):
        control._egress(package)


@pytest.mark.parametrize("dropped", ["profile_version", "network_profile"])
def test_cleanup_refuses_half_marked_package_and_deletes_nothing(tmp_path, monkeypatch, dropped):
    f = setup_egress(tmp_path)
    created(f)
    f.cluster.log = LOG
    finish(f)
    body, _ = durable_outputs(f)
    real = AgentSandboxStore._bound

    def half_marked(self, connection, live, operation_id):
        operation, package, expected, state = real(self, connection, live, operation_id)
        package = {k: v for k, v in package.items() if k != dropped}
        return operation, package, expected, state

    monkeypatch.setattr(AgentSandboxStore, "_bound", half_marked)
    with pytest.raises(Conflict, match="inconsistent egress markers"):
        f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)
    assert not [method for method, _ in f.cluster.calls if method == "DELETE"]
