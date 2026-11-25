"""End-of-attempt calls ride out a control stall (ADR 0188).

After a confirmed exit the worker reports the exit, transfers the sealed outputs to quarantine
and posts the run receipt. A transient control failure in any of them (the channel's "control
request is still in progress", a timeout, a 5xx) is retried with backoff within one bounded
window instead of ending the attempt; a retried call after a partly applied one neither
duplicates nor loses output; a refusal is never retried; a spent window fails closed as before
(custody kept, nothing claimed, no cancellation of the exited container). No network, no
provider, no money.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from test_development_worker_service import (
    _exit_after_start,
    _InspectingAdapter,
    _ReceiptChannel,
    _service,
    _unleased_delivery,
)

from tessera_worker.control_channel import ControlChannelError

STALL = "control request is still in progress"
MANIFEST = "/artifact-manifest"


class StallingChannel(_ReceiptChannel):
    """Fails chosen control calls like a stalled control: the request may still land."""

    def __init__(self, state, *, manifest_stalls=0, receipt_stalls=0, applied=True, error=None):
        super().__init__(state)
        self.manifest_stalls = manifest_stalls
        self.receipt_stalls = receipt_stalls
        self.applied = applied  # the stalled manifest post reached control; only the reply is lost
        self.error = error
        self.manifest_posts = 0

    def post(self, path, body, key):
        if path.endswith(MANIFEST):
            self.manifest_posts += 1
            if self.manifest_stalls:
                self.manifest_stalls -= 1
                if self.applied:
                    super().post(path, body, key)
                raise self.error or ControlChannelError(STALL)
        if path.endswith("/run-receipt") and self.receipt_stalls:
            self.receipt_stalls -= 1
            self.receipts.append((path, body, key))
            raise ControlChannelError(STALL)
        return super().post(path, body, key)


def _run(tmp_path, channel_factory, *, real_sleep=False, max_ticks=5):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = _InspectingAdapter()
    channel = channel_factory(state)
    now = [0.0]
    sleeps: list[float] = []
    flip = _exit_after_start(adapter)

    def sleep(seconds):
        flip(seconds)
        sleeps.append(seconds)
        if real_sleep:
            time.sleep(min(seconds, 3.0))
        now[0] += seconds

    service = _service(
        tmp_path, state, grant, channel=channel, adapter=adapter, max_ticks=max_ticks, sleep=sleep
    )
    service.clock = lambda: now[0]
    return service, channel, adapter, state, sleeps


def _manifests(state) -> int:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        return connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0]


def test_a_stalled_output_transfer_is_retried_and_the_attempt_completes(tmp_path):
    service, channel, adapter, state, _ = _run(
        tmp_path, lambda s: StallingChannel(s, manifest_stalls=2)
    )
    result = service.run()
    assert result["state"] == "exited" and result["quarantined"] is True, result
    assert result["run_receipt"]["status"] == "recorded"
    # The stalled posts reached control; the retries replayed them: one manifest, nothing lost.
    assert _manifests(state) == 1
    assert channel.manifest_posts == 3
    assert result["handoff_retries"]["calls"] == {"output_transfer": 2}
    assert "still in progress" in result["handoff_retries"]["last_failure"]
    assert "exhausted" not in result["handoff_retries"]
    assert "cancel" not in adapter.calls  # the exited container is never killed for a stall


def test_a_stall_that_outlasts_the_window_fails_closed(tmp_path):
    service, channel, adapter, state, sleeps = _run(
        tmp_path, lambda s: StallingChannel(s, manifest_stalls=10_000, applied=False)
    )
    with pytest.raises(ControlChannelError, match="still in progress"):
        service.run()
    retries = service._handoff_retries
    assert retries.exhausted == "output_transfer"
    # Bounded: backoff 1, 2, 4, 8, 8, ... inside the 180 s window, then fail closed.
    assert 10 <= retries.calls["output_transfer"] <= 40
    assert sum(sleeps) <= 180 + 2
    assert _manifests(state) == 0
    assert "cancel" not in adapter.calls
    # Custody is kept for the poller's later recovery: the sealed workspace is still there.
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_a_refused_output_transfer_is_not_retried(tmp_path):
    service, channel, _adapter, _state, _ = _run(
        tmp_path,
        lambda s: StallingChannel(
            s, manifest_stalls=1, applied=False, error=ControlChannelError("refused", 409)
        ),
    )
    with pytest.raises(ControlChannelError):
        service.run()
    assert channel.manifest_posts == 1
    assert service._handoff_retries.failed == 0


def test_a_stalled_run_receipt_is_retried_past_the_old_three_attempts(tmp_path):
    service, channel, _adapter, _state, _ = _run(
        tmp_path, lambda s: StallingChannel(s, receipt_stalls=4)
    )
    result = service.run()
    assert result["state"] == "exited" and result["run_receipt"]["status"] == "recorded", result
    assert len(channel.receipts) == 5
    assert len({key for _path, _body, key in channel.receipts}) == 1  # one idempotency key
    assert result["handoff_retries"]["calls"] == {"run_receipt": 4}


def test_a_stalled_exit_report_is_retried_through_the_outbox(tmp_path):
    class ExitReportStall(StallingChannel):
        stalled = False

        def post(self, path, body, key):
            if (
                path == "/worker/observations"
                and body.get("observed_state") == "exited"
                and not self.stalled
            ):
                self.stalled = True
                raise ControlChannelError(STALL)
            return super().post(path, body, key)

    # The outbox keeps its own delay (2 s first) in real time, so this test sleeps for real.
    service, _channel, adapter, state, _ = _run(tmp_path, ExitReportStall, real_sleep=True)
    result = service.run()
    assert result["state"] == "exited" and result["quarantined"] is True, result
    assert result["handoff_retries"]["calls"] == {"exit_report": 1}
    assert _manifests(state) == 1
    assert "cancel" not in adapter.calls
