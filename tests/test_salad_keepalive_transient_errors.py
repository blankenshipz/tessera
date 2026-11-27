"""The executor's keepalive does not fence an attempt on a transient error.

A held write lock (``sqlite3.OperationalError: database is locked``) or a timeout during a lease
renewal is counted and retried at the next renewal; only control's refusal (``Conflict``,
``Forbidden``, ``NotFound``) fences the attempt. Stopping the keepalive thread is bounded, and
the child's receipt carries the keepalive counts (also when finished again from disk). Fakes
only: no subprocess, no network, no live Salad.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from test_campaign_salad_executor import RECEIPT, FakeControl, Harness, item
from test_salad_executor_child_keepalive import LockedControl, _blocking_runner, _serial, _work

from tessera_control import campaign_salad_executor as executor_module
from tessera_control.campaign_salad_executor import KEEPALIVE_MARKER, _Keepalive
from tessera_control.store import Conflict, Forbidden


class Flaky:
    """A control whose renewals fail with the queued errors, then succeed."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.renewed = 0

    def heartbeat(self):
        return None

    def renew(self, lease_id, epoch):
        if self.errors:
            raise self.errors.pop(0)
        self.renewed += 1


def test_a_locked_database_during_renewal_is_retried_not_fenced():
    control = Flaky([sqlite3.OperationalError("database is locked"), TimeoutError()])
    keepalive = _Keepalive(control, {"att_review": ("lease_review", 3)}, 60)
    for _ in range(3):
        keepalive.beat()
    record = keepalive.snapshot()
    assert record["refused"] == {}
    assert record["transient"] == {"att_review": 2}
    assert control.renewed == 1
    assert "att_review" in keepalive.leases


def test_only_a_control_refusal_fences():
    for refusal in (Conflict("lease no longer current"), Forbidden("no")):
        control = Flaky([refusal])
        keepalive = _Keepalive(control, {"att_review": ("lease_review", 3)}, 60)
        keepalive.beat()
        keepalive.beat()
        assert keepalive.snapshot()["refused"] == {"att_review": type(refusal).__name__}
        assert control.renewed == 0  # never retried past the fence


def test_the_pass_keepalive_does_not_fence_on_a_locked_database(tmp_path):
    class Locked(FakeControl):
        def __init__(self):
            super().__init__()
            self.locks = 1

        def renew(self, lease_id, epoch):
            self._c("renew", lease_id, epoch)
            if self.locks:
                self.locks -= 1
                raise sqlite3.OperationalError("database is locked")

    h = Harness(tmp_path, Locked())
    h.control.items = [item("awaiting_review")]
    first = h.executor.run_once()
    assert ("lease_renewal_transient", "OperationalError") in [
        (a["action"], a["detail"]) for a in first
    ]
    assert "att_1" not in h.executor.fenced
    h.executor.run_once()
    assert h.names().count("renew") == 2  # renewed again on the next pass


def test_a_failed_heartbeat_does_not_fence_the_pass_keepalive(tmp_path):
    h = Harness(tmp_path)
    h.control.heartbeat_error = sqlite3.OperationalError("database is locked")
    h.control.items = [item("awaiting_review")]
    actions = h.executor.run_once()
    assert "heartbeat_failed" in [a["action"] for a in actions]
    assert "att_1" not in h.executor.fenced
    assert "renew" in h.names()


def test_a_failed_renewal_heartbeat_leaves_the_pass_end_heartbeat(tmp_path):
    # F-5: only a heartbeat that went through spares the pass-end fallback.
    class OnceDown(FakeControl):
        def heartbeat(self):
            self._c("heartbeat")
            if self.names_count("heartbeat") == 1:
                raise sqlite3.OperationalError("database is locked")

        def names_count(self, name):
            return [c[0] for c in self.calls].count(name)

    h = Harness(tmp_path, OnceDown())
    h.control.items = [item("awaiting_review")]
    h.executor.run_once()
    assert h.names().count("heartbeat") == 2
    assert "att_1" not in h.executor.fenced


def test_stopping_a_stuck_keepalive_is_bounded(monkeypatch):
    monkeypatch.setattr(executor_module, "KEEPALIVE_JOIN_SECONDS", 0.2)
    release = threading.Event()

    class Stuck(Flaky):
        def heartbeat(self):
            release.wait(10)

    keepalive = _Keepalive(Stuck([]), {}, 0.01)
    started = time.monotonic()
    try:
        with keepalive:
            time.sleep(0.05)
        assert time.monotonic() - started < 2
        assert keepalive.snapshot()["join_timed_out"] is True
    finally:
        release.set()


def test_the_receipt_carries_transient_keepalive_counts(tmp_path):
    control = LockedControl()
    locked = {"left": 2}

    def renew(lease_id, epoch):
        LockedControl.renew(control, lease_id, epoch)
        if lease_id == "lease_review" and locked["left"]:
            locked["left"] -= 1
            raise sqlite3.OperationalError("database is locked")

    control.renew = renew
    runner, _ = _blocking_runner(control, lambda: control.count("renew", "lease_review") >= 4)
    h = _serial(tmp_path, control, runner)
    control.items = _work()
    actions = h.executor.run_once()
    assert "att_review" not in h.executor.fenced
    assert "lease_renewal_transient" in [a["action"] for a in actions]
    outcomes = [c for c in control.calls if c[0] == "record_outcome"]
    assert outcomes
    counts = outcomes[-1][2]["live_receipt"]["executor_keepalive"]
    assert counts["renewal_transient_failures"] == 2
    assert counts["renewals_refused"] == 0
    # Finished again from disk (a restart), the receipt carries the same counts.
    route_dir = h.executor._route_dir("route_1")
    assert (route_dir / KEEPALIVE_MARKER).exists()
    again = h.executor._with_keepalive("route_1", dict(RECEIPT))
    assert again["executor_keepalive"] == counts
