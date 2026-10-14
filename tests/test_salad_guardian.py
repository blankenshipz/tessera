import subprocess
import sys
from dataclasses import replace

import pytest

from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_guardian import (
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
)


@pytest.fixture
def case(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    b = CustodyBinding("tenant", "project", "operation", 1, "artifact")
    c = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    c.register(b, byte_cap=10, file_cap=1)
    a = GuardianAssignment(
        b,
        "org",
        "provider-project",
        "group-id",
        "attempt-operation",
        "allocation",
        "sha256:image",
        200,
    )
    state = ["running"]
    stops = []

    def observe(a):
        if isinstance(state[0], Exception):
            raise state[0]
        if isinstance(state[0], ProviderObservation):
            return state[0]
        return ProviderObservation(
            a.organization,
            a.provider_project,
            a.group_id,
            a.group_name,
            a.allocation_id,
            a.image_digest,
            state[0],
            clock[0],
        )

    def stop(a):
        stops.append(a)
        return "pending"

    kw = dict(custody=c, observe=observe, stop=stop, clock=lambda: clock[0])
    g = SaladGuardian(tmp_path / "guardian.db", **kw)
    g.arm(a)
    yield g, a, c, clock, state, stops, kw, tmp_path
    try:
        g.close()
    except Exception:
        pass


@pytest.mark.parametrize(
    "elapsed,reason", [(100, "deadline"), (30, "control_disconnect"), (11, "missed_poll")]
)
def test_independent_enforcement(case, elapsed, reason):
    g, a, c, clock, state, stops, _, _ = case
    clock[0] += elapsed
    s = g.tick(a.binding.operation)
    assert s["status"] == "unknown" and s["reason"] == reason
    assert stops == [a] and s["orphan"] and s["pending_exposure"]
    assert c.seal(a.binding)["writers_closed"]
    g.heartbeat(a.binding.operation)
    assert g.status(a.binding.operation)["status"] == "unknown"


def test_restart_is_stop_only_and_retry_is_narrow(case):
    g, a, _, clock, _, stops, kw, tmp = case
    g.close()
    recovered = SaladGuardian(tmp / "guardian.db", **kw)
    try:
        recovered.arm(a)  # exact reconnect cannot renew consumed execution
        recovered.tick(a.binding.operation)
        recovered.tick(a.binding.operation)
        assert stops == [a]
        clock[0] += a.stop_retry_seconds
        recovered.tick(a.binding.operation)
        assert stops == [a, a]
        with pytest.raises(ValueError, match="mismatch"):
            recovered.arm(replace(a, group_id="new"))
    finally:
        recovered.close()


def test_single_service_owner(case):
    _, _, _, _, _, _, kw, tmp = case
    with pytest.raises(ValueError, match="already_owned"):
        SaladGuardian(tmp / "guardian.db", **kw)


def test_202_is_pending_actual_readback_and_seal_required(case):
    g, a, _, _, state, stops, _, _ = case
    g.cancel(a.binding.operation)
    assert g.tick(a.binding.operation)["status"] == "unknown"
    assert len(stops) == 1
    state[0] = "stopped"
    s = g.tick(a.binding.operation)
    assert s["status"] == "stopped" and s["manifest"]["writers_closed"]
    assert s["node_writers"] == "unknown" and s["cost"] == "unknown"
    assert s["quarantine"]


@pytest.mark.parametrize("state", ["unknown", "pending", RuntimeError("DO_NOT_LOG")])
def test_outage_unknown_never_stopped_and_stop_recovery_available(case, state):
    g, a, _, _, states, stops, _, _ = case
    states[0] = state
    g.cancel(a.binding.operation)
    s = g.tick(a.binding.operation)
    assert s["status"] == "unknown" and s["orphan"] and len(stops) == 1
    assert "DO_NOT_LOG" not in str(s)


@pytest.mark.parametrize(
    "field,value",
    [
        ("group_id", "foreign"),
        ("project", "foreign"),
        ("image_digest", "foreign"),
        ("organization", "foreign"),
    ],
)
def test_wrong_identity_cannot_stop_foreign_group(case, field, value):
    g, a, _, clock, states, stops, _, _ = case
    states[0] = replace(
        ProviderObservation(
            a.organization,
            a.provider_project,
            a.group_id,
            a.group_name,
            a.allocation_id,
            a.image_digest,
            "stopped",
            clock[0],
        ),
        **{field: value},
    )
    s = g.tick(a.binding.operation)
    assert s["status"] == "unknown" and not stops


def test_reallocation_sticky_original_orphan_not_adopted(case):
    g, a, _, clock, states, stops, _, _ = case
    states[0] = ProviderObservation(
        a.organization,
        a.provider_project,
        a.group_id,
        a.group_name,
        "replacement",
        a.image_digest,
        "stopped",
        clock[0],
    )
    s = g.tick(a.binding.operation)
    assert s["allocation_uncertain"] and s["orphan"] and stops == [a]
    states[0] = "stopped"
    assert g.tick(a.binding.operation)["status"] == "unknown"


def test_stale_readback_and_clock_rollback(case):
    g, a, _, clock, states, _, _, _ = case
    states[0] = ProviderObservation(
        a.organization,
        a.provider_project,
        a.group_id,
        a.group_name,
        a.allocation_id,
        a.image_digest,
        "stopped",
        1,
    )
    assert g.tick(a.binding.operation)["status"] == "unknown"
    clock[0] = 99
    states[0] = "running"
    assert g.tick(a.binding.operation)["reason"] == "clock_uncertain"


def test_seal_failure_cannot_record_stopped(case, monkeypatch):
    g, a, c, _, states, _, _, _ = case
    states[0] = "stopped"

    def fail(_):
        raise ValueError("integrity")

    monkeypatch.setattr(c, "seal_operation", fail)
    with pytest.raises(ValueError):
        g.tick(a.binding.operation)
    assert g.status(a.binding.operation)["status"] != "stopped"


def test_custody_failure_does_not_disable_stop(case, monkeypatch):
    g, a, c, _, _, stops, _, _ = case

    def fail(_):
        raise ValueError("disk_failed")

    monkeypatch.setattr(c, "close_writers", fail)
    g.cancel(a.binding.operation)
    assert g.tick(a.binding.operation)["status"] == "unknown"
    assert stops == [a]


def test_actual_service_death_and_durable_stop_only_recovery(case):
    g, a, _, _, _, stops, kw, tmp = case
    g.close()
    # A separately running service takes ownership and dies without orderly close.
    program = """
import os, sys
from pathlib import Path
from tessera_control.salad_custody import SaladCustody
from tessera_control.salad_guardian import SaladGuardian
root = Path(sys.argv[1])
c = SaladCustody(root / 'custody.db')
g = SaladGuardian(root / 'guardian.db', custody=c,
                  observe=lambda a: None, stop=lambda a: 'pending')
os._exit(19)
"""
    run = subprocess.run([sys.executable, "-c", program, str(tmp)], capture_output=True)
    assert run.returncode == 19, run.stderr.decode()
    recovered = SaladGuardian(tmp / "guardian.db", **kw)
    try:
        assert recovered.tick(a.binding.operation)["status"] == "unknown"
        assert stops == [a]
    finally:
        recovered.close()
