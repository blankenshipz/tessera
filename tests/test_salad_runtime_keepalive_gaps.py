"""Keepalive gaps of the Salad executor: a lease taken earlier in the pass, the finish, own lease.

A serial pass blocks for minutes in a route's finish (termination confirmation, guardian final,
import, upload) and, after packaging, a lease taken earlier in the same pass was not renewed by
the next route's child keepalive. The running route's own lease was also unrenewed in its child's
tail and during its finish. Fakes only: no subprocess, no network, no live Salad.
"""

from __future__ import annotations

import json
import threading
import time

from test_campaign_salad_executor import OPERATION, RECEIPT, Harness, item
from test_salad_executor_child_keepalive import LockedControl, _blocking_runner, _serial
from test_salad_executor_concurrent_routes import (
    ConcurrentControl,
    GatedRunner,
    executor_for,
    started,
)

from tessera_control import campaign_salad_executor as module

WAIT = 5.0
BEATS = 3


class GapControl(LockedControl):
    """LockedControl with a distinct lease for a freshly acquired grant and a blockable upload."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pending.add("grant_a")
        self.upload_seen = {}
        self.block_upload = False

    def acquire_lease(self, grant_id, key):
        lease = super().acquire_lease(grant_id, key)
        return dict(id="lease_a", epoch=3) if grant_id == "grant_a" else lease

    def upload_outputs(self, lease_id, epoch, operation_id, directory, files):
        if self.block_upload:
            self._block_until_kept_alive()
        return super().upload_outputs(lease_id, epoch, operation_id, directory, files)

    def _block_until_kept_alive(self):
        base = {
            "review": self.count("renew", "lease_review"),
            "own": self.count("renew", "lease_1"),
            "beats": self.count("heartbeat"),
        }

        def reached():
            return (
                self.count("renew", "lease_review") - base["review"] >= BEATS
                and self.count("renew", "lease_1") - base["own"] >= BEATS
                and self.count("heartbeat") - base["beats"] >= BEATS
            )

        deadline = time.monotonic() + WAIT
        while not reached() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.upload_seen = {
            "reached": reached(),
            "review": self.count("renew", "lease_review") - base["review"],
            "own": self.count("renew", "lease_1") - base["own"],
            "beats": self.count("heartbeat") - base["beats"],
        }


class GapConcurrent(GapControl, ConcurrentControl):
    pass


def _review():
    return item("awaiting_review", attempt_id="att_review", operation_id="op_review", route_id=None)


def _quiet(control):
    after = len(control.calls)
    time.sleep(0.05)
    assert len(control.calls) == after


def _write_receipt(tmp_path):
    receipts = tmp_path / "work" / "route_1" / "receipts"
    receipts.mkdir(parents=True, mode=0o700)
    (tmp_path / "work" / "route_1" / "outputs").mkdir(mode=0o700)
    target = receipts / module.RECEIPT_NAME
    target.write_text(json.dumps(RECEIPT))
    target.chmod(0o600)


def test_a_lease_taken_earlier_in_the_pass_is_renewed_during_the_next_childs_run(tmp_path):
    control = GapControl()
    control.items = [
        item(
            "awaiting_lease",
            attempt_id="att_a",
            grant_id="grant_a",
            admitted_attempt_id="adm_a",
            route_id=None,
            operation_id=OPERATION,
        ),
        item("route_started"),
    ]
    runner, seen = _blocking_runner(control, lambda: control.count("renew", "lease_a") >= BEATS)
    harness = _serial(tmp_path, control, runner)

    log = harness.executor.run_once()

    assert control.count("acquire", "grant_a") == 1
    assert seen["done"] is True
    assert "att_a" not in harness.executor.fenced
    assert not [a for a in log if a["action"] in {"lease_renewal_refused", "error"}]
    _quiet(control)


def test_a_blocked_serial_finish_keeps_the_other_lease_the_own_lease_and_the_registry_alive(
    tmp_path,
):
    control = GapControl()
    control.items = [item("route_started"), _review()]
    control.block_upload = True
    harness = Harness(tmp_path, control)
    harness.executor.keepalive_seconds = 0.01

    harness.executor.run_once()

    assert control.upload_seen["reached"] is True, control.upload_seen
    assert control.count("record_outcome") == 1
    assert "att_review" not in harness.executor.fenced
    assert "att_1" not in harness.executor.fenced
    _quiet(control)


def test_a_blocked_concurrent_harvest_finish_keeps_the_other_lease_alive(tmp_path):
    control = GapConcurrent(limit=2)
    control.items = [*started((1,)), _review()]
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    executor.keepalive_seconds = 0.01
    executor.run_once()
    runner.wait_started("route_1")
    control.block_upload = True
    runner.release.setdefault("route_1", threading.Event()).set()
    for run in list(executor._in_flight.values()):
        run.thread.join(WAIT)

    executor.run_once()  # harvests the finished child; its finish blocks on the upload

    assert control.upload_seen["reached"] is True, control.upload_seen
    assert control.count("record_outcome") == 1
    assert "att_review" not in executor.fenced
    _quiet(control)


def test_a_refused_running_lease_is_noted_and_never_fences_the_route(tmp_path):
    control = GapControl()
    control.items = [item("route_started")]
    inner, seen = _blocking_runner(control, lambda: control.count("renew", "lease_1") >= 2)

    def runner(config_path, receipt_dir, timeout):
        # The pass's own pre-launch renewal went through; control refuses from here on.
        control.refuse_lease.add("lease_1")
        return inner(config_path, receipt_dir, timeout)

    harness = _serial(tmp_path, control, runner)

    log = harness.executor.run_once()

    assert seen["done"] is True
    refused = [a for a in log if a["action"] == "running_lease_renewal_refused"]
    assert refused and refused[0]["detail"] == "Conflict"
    assert "att_1" not in harness.executor.fenced
    assert not [a for a in log if a["action"] == "lease_renewal_refused"]
    assert control.count("record_outcome") == 1


def test_a_resumed_executing_attempt_finishes_under_the_keepalive(tmp_path):
    control = GapControl()
    control.items = [item("executing"), _review()]
    control.block_upload = True
    harness = Harness(tmp_path, control)
    harness.executor.keepalive_seconds = 0.01
    _write_receipt(tmp_path)

    harness.executor.run_once()

    assert harness.runs == []
    assert control.upload_seen["reached"] is True, control.upload_seen
    assert control.count("record_outcome") == 1
    _quiet(control)


def test_a_fenced_attempt_is_not_renewed_during_a_finish_and_nothing_runs_after_it(tmp_path):
    control = GapControl()
    control.items = [item("route_started"), _review()]
    harness = Harness(tmp_path, control)
    harness.executor.keepalive_seconds = 0.01
    harness.executor.fenced["att_review"] = "Conflict"
    control.block_upload = True
    # Only the registry and the running route's own lease are beaten here.
    deadline_calls = {}

    def upload_gate():
        base = control.count("heartbeat")
        end = time.monotonic() + WAIT
        while control.count("heartbeat") - base < BEATS and time.monotonic() < end:
            time.sleep(0.005)
        deadline_calls["beats"] = control.count("heartbeat") - base

    control._block_until_kept_alive = upload_gate

    harness.executor.run_once()

    assert deadline_calls["beats"] >= BEATS
    assert control.count("renew", "lease_review") == 0
    assert control.count("record_outcome") == 1
    _quiet(control)
