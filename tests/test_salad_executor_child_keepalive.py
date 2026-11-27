"""The Salad executor keeps its worker and its other leases alive while a route's child runs.

Field shape: a serial executor (the default) blocked for most of an hour inside one route's live
check let every other attempt's lease (an ``awaiting_review`` one included) lapse and its
registry heartbeat go stale, so the worker read as disconnected. Fakes only: no subprocess, no
network, no live Salad.
"""

from __future__ import annotations

import threading
import time

from test_campaign_salad_executor import RECEIPT, FakeControl, Harness, item
from test_salad_executor_concurrent_routes import (
    ConcurrentControl,
    GatedRunner,
    executor_for,
    started,
)

from tessera_control.store import Conflict

WAIT = 5.0


class LockedControl(FakeControl):
    """The fake control with calls recorded under a lock (two threads call it here)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lock = threading.Lock()
        self.refuse_lease = set()

    def _c(self, name, *args):
        with self.lock:
            self.calls.append((name, *args))

    def operation(self, operation_id):
        lease = "lease_review" if operation_id == "op_review" else "lease_1"
        return dict(lease_id=lease, epoch=3, grant_id="grant_1")

    def renew(self, lease_id, epoch):
        super().renew(lease_id, epoch)
        if lease_id in self.refuse_lease:
            raise Conflict("Execution grant epoch is no longer current.")

    def count(self, name, *args):
        with self.lock:
            return sum(1 for call in self.calls if call[: 1 + len(args)] == (name, *args))


def _blocking_runner(control, until):
    """A child that runs until ``until()`` holds (or WAIT passes), then returns a receipt."""
    seen = {}

    def runner(config_path, receipt_dir, timeout):
        deadline = time.monotonic() + WAIT
        while not until() and time.monotonic() < deadline:
            time.sleep(0.005)
        seen["done"] = until()
        return RECEIPT

    return runner, seen


def _serial(tmp_path, control, runner):
    harness = Harness(tmp_path, control, runner=runner)
    harness.executor.keepalive_seconds = 0.01
    return harness


def _work():
    return [
        item("route_started"),
        item("awaiting_review", attempt_id="att_review", operation_id="op_review", route_id=None),
    ]


def test_other_leases_and_the_registry_stay_alive_while_a_serial_child_runs(tmp_path):
    control = LockedControl()
    control.items = _work()
    runner, seen = _blocking_runner(
        control,
        lambda: control.count("renew", "lease_review") >= 3 and control.count("heartbeat") >= 4,
    )
    harness = _serial(tmp_path, control, runner)

    log = harness.executor.run_once()

    assert seen["done"] is True
    # The running route's own lease is its child's to renew, never the keepalive's: every
    # renewal of ``lease_1`` is the pass's own (once, before launch).
    assert control.count("renew", "lease_1") == 1
    assert "att_review" not in harness.executor.fenced
    assert not [a for a in log if a["action"] in {"lease_renewal_refused", "heartbeat_failed"}]
    # The keepalive stopped with the child: no call arrives after the pass returned.
    after = len(control.calls)
    time.sleep(0.05)
    assert len(control.calls) == after


def test_a_refused_renewal_during_the_child_fences_that_attempt_once(tmp_path):
    control = LockedControl()
    control.items = _work()
    control.refuse_lease.add("lease_review")
    runner, seen = _blocking_runner(control, lambda: control.count("heartbeat") >= 4)
    harness = _serial(tmp_path, control, runner)

    log = harness.executor.run_once()

    assert seen["done"] is True
    # Refused once, never retried past the fence (not by the keepalive, not by the pass).
    assert control.count("renew", "lease_review") == 1
    assert harness.executor.fenced["att_review"] == "Conflict"
    refused = [a for a in log if a["action"] == "lease_renewal_refused"]
    assert [(a["attempt_id"], a["detail"]) for a in refused] == [("att_review", "Conflict")]


def test_a_failed_registry_heartbeat_is_noted_and_retried(tmp_path):
    control = LockedControl()
    control.items = [item("route_started")]
    inner, seen = _blocking_runner(control, lambda: control.count("heartbeat") >= 3)

    def runner(config_path, receipt_dir, timeout):
        # The registry starts refusing once the child runs (the pass's own beat went through).
        control.heartbeat_error = RuntimeError("registry unavailable")
        return inner(config_path, receipt_dir, timeout)

    harness = _serial(tmp_path, control, runner)

    log = harness.executor.run_once()

    assert seen["done"] is True
    noted = [a for a in log if a["action"] == "heartbeat_failed" and a["attempt_id"] is None]
    assert len(noted) == 1 and noted[0]["detail"].startswith("RuntimeError x")


def test_a_fenced_attempt_is_not_renewed_by_the_keepalive(tmp_path):
    control = LockedControl()
    control.items = _work()
    runner, seen = _blocking_runner(control, lambda: control.count("heartbeat") >= 3)
    harness = _serial(tmp_path, control, runner)
    harness.executor.fenced["att_review"] = "Conflict"

    harness.executor.run_once()

    assert seen["done"] is True
    assert control.count("renew", "lease_review") == 0


def test_concurrent_pass_heartbeats_the_registry_with_only_children_in_flight(tmp_path):
    control = ConcurrentControl(limit=2)
    control.items = started((1,))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    executor.run_once()
    runner.wait_started("route_1")
    before = [c[0] for c in control.calls].count("heartbeat")

    executor.run_once()  # only the in-flight route is listed: it is skipped

    assert [c[0] for c in control.calls].count("heartbeat") == before + 1
    runner.finish(executor, "route_1")
    executor.run_once()
