"""The running cap counts from the instance's own ``running``, never an image pull.

Accrual stays conservative (it may start at the first group-level ``running`` read); the running
window (the stop deadline) and the guardian's first-running latch start only on an instance-level
``running`` or, with no instance list, a positive ``running_count``."""

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import GROUP, Provider, make_service, reservation

from tessera_control.salad_safety_service import _instance_running, _running

COUNTS = ("allocating_count", "creating_count", "running_count", "stopping_count")


def _counts(**kw):
    return {k: kw.get(k, 0) for k in COUNTS}


@pytest.mark.parametrize("instance_state", ["creating", "downloading"])
def test_group_running_over_a_pulling_instance_is_not_an_instance_running(instance_state):
    observation = {"state": "running", "instances": [{"state": instance_state, "machine_id": "m"}]}
    assert _running(observation) is True  # accrual stays conservative
    assert _instance_running(observation) is False
    assert _instance_running({"state": "pending", "instances": [{"state": "running"}]}) is True


def test_no_instance_list_latches_on_a_positive_running_count_only():
    running = {"state": "running", "instance_status_counts": _counts(running_count=1)}
    creating = {"state": "running", "instance_status_counts": _counts(creating_count=1)}
    assert _instance_running(running) is True
    assert _instance_running(creating) is False
    # counts that cannot be read stay fail closed
    assert _instance_running({"state": "running"}) is True
    assert _instance_running({**creating, "instance_counts_complete": False}) is True
    assert _instance_running({"state": "pending", "instance_status_counts": _counts()}) is False


def test_the_running_cap_counts_from_the_instance_not_the_pull(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    provider.state = "running"
    provider.instance_state = "downloading"
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        b = reservation(seconds=3000, workload_seconds=60)
        s.loop()
        s.admin(b)
        s.admin(
            dict(
                operation="bind",
                reservation=b["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        assignment = s._assignment(s._row(b["reservation"]))
        seen = s._observe(assignment)
        row = s._row(b["reservation"])
        # accrual started at the first group-level read; the cap did not
        assert row["first_running_at"] is not None and row["seen_running"] == 1
        assert row["seen_running_at"] is None and row["accrual_only"] == 1
        assert seen.state == "pending"
        assert s._stop_deadline(row) == row["deadline"]
        for _ in range(10):
            clock[0] += 60
            s._observe(assignment)
        assert s._row(b["reservation"])["seen_running_at"] is None
        assert s._stop_deadline(s._row(b["reservation"])) == row["deadline"]
        provider.instance_state = "running"
        latched = clock[0]
        assert s._observe(assignment).state == "running"
        row = s._row(b["reservation"])
        assert row["seen_running_at"] == latched
        assert s._stop_deadline(row) == min(row["deadline"], latched + row["running_seconds"])
        # a later read never moves the latch
        clock[0] += 30
        s._observe(assignment)
        assert s._row(b["reservation"])["seen_running_at"] == latched
    finally:
        s.close()
