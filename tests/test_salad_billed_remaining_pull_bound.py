"""Worst-case remaining billed time stays an upper bound while an image pull is accruing.

A group-level ``running`` read over a still pulling instance starts accrual but not the running
cap, so the stop deadline stays at the route deadline. The spend check's remaining-time estimate
must then reach that deadline, not stop at the running window."""

from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import GROUP, Provider, make_service, reservation


def _bound_service(tmp_path, clock, provider, b):
    tmp_path.chmod(0o700)
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
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
    return s


def test_the_bound_reaches_the_route_deadline_while_a_pull_accrues(tmp_path):
    clock = [100.0]
    provider = Provider()
    provider.state = "pending"
    b = reservation(seconds=3000, workload_seconds=60)
    s = _bound_service(tmp_path, clock, provider, b)
    try:
        row = s._row(b["reservation"])
        # not accruing yet: billing can last at most the running window
        assert row["first_running_at"] is None
        assert s._billed_remaining(row) == min(row["deadline"] - clock[0], row["running_seconds"])
        assert s._billed_remaining(row) == row["running_seconds"]

        provider.state = "running"
        provider.instance_state = "downloading"
        assignment = s._assignment(row)
        s._observe(assignment)
        for _ in range(5):
            clock[0] += 60
            s._observe(assignment)
            row = s._row(b["reservation"])
            assert row["accrual_only"] == 1 and row["seen_running_at"] is None
            # the pull can last until the route deadline: never the running window alone
            assert s._billed_remaining(row) == row["deadline"] - clock[0]
            assert s._billed_remaining(row) > row["running_seconds"]
            assert s._billed_remaining(row) >= s._stop_deadline(row) - clock[0]

        provider.instance_state = "running"
        s._observe(assignment)
        row = s._row(b["reservation"])
        latched = row["seen_running_at"]
        assert latched == clock[0]
        # once the cap starts, the stop deadline bounds it again
        assert s._billed_remaining(row) == s._stop_deadline(row) - clock[0]
        assert s._billed_remaining(row) == min(
            row["deadline"] - clock[0], latched + row["running_seconds"] - clock[0]
        )
    finally:
        s.close()


def test_no_running_window_keeps_the_route_deadline(tmp_path):
    clock = [100.0]
    provider = Provider()
    provider.state = "pending"
    b = reservation(seconds=3000, workload_seconds=60)
    s = _bound_service(tmp_path, clock, provider, b)
    try:
        row = dict(s._row(b["reservation"]))
        row["running_seconds"] = None
        assert s._billed_remaining(row) == row["deadline"] - clock[0]
    finally:
        s.close()


@pytest.mark.parametrize("instance_state", ["downloading", "running"])
def test_the_spend_check_fences_a_pull_that_could_pass_the_per_run_cap(tmp_path, instance_state):
    """At 11 c/h a 3500 s route window can bill about 10.7 c against a 10 c cap, though the
    60 s running window alone fits. A pull that may last to the route deadline is fenced at
    once; an instance that is itself running has its cap started and is not."""
    clock = [100.0]
    provider = Provider()
    provider.rate = "0.11"
    provider.state = "pending"
    b = reservation(seconds=3500, workload_seconds=60)
    cap = b["proposal"]["selection"]["salad"]["execution"]["max_cost_cents"]
    s = _bound_service(tmp_path, clock, provider, b)
    try:
        row = s._row(b["reservation"])
        rate = Decimal(row["rate"])
        assert rate * Decimal(str(row["deadline"] - clock[0])) * 100 / 3600 > cap
        assert rate * Decimal(str(row["running_seconds"])) * 100 / 3600 < cap
        s._budget(row, refresh=True)  # not accruing yet: the running window fits
        assert not s._row(b["reservation"])["budget_fenced"]

        provider.state = "running"
        provider.instance_state = instance_state
        clock[0] += 1
        s._observe(s._assignment(s._row(b["reservation"])))
        row = s._row(b["reservation"])
        if instance_state == "downloading":
            with pytest.raises(ValueError, match="exposure_fenced"):
                s._budget(row, refresh=True, tolerate=True)
            row = s._row(b["reservation"])
            assert row["budget_fenced"] == 1 and row["recovery"] == 1
        else:
            s._budget(row, refresh=True, tolerate=True)
            assert not s._row(b["reservation"])["budget_fenced"]
    finally:
        s.close()
