"""A running stage survives a control stall of about two minutes; fencing is unchanged (ADR 0189).

The worker renews every 10 s and control's lease lasts 150 s, so a stall that begins right when a
renewal is due (the worst phase) is survived for up to 135 s. A longer stall still cancels the
stage before control's lease expires, and control never renews an expired lease or a lease whose
epoch moved on.

The worker tests reuse ADR 0178's simulated timeline: worker and control share one clock, and
every control call made during the stall gets no answer and uses up the whole limit it was given.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import test_execution as execution_fixtures
from test_input_delivery import _delivery
from test_worker_lease_renewal_stall import HANG, RENEW, _control_lease_expiry, _timed

from tessera_control import execution as control_execution
from tessera_control.store import (
    Conflict,
    Store,
    _connect,
    _parse_utc,
    _utc_string,
    canonical_digest,
)

LEASE = control_execution.LEASE_SECONDS


# The fixture task approves 60 s. A stall test needs a stage that runs for minutes, so the fixture
# task is created and approved with a longer duration: the plan, the operation's duration bound
# and the lease's hard expiry all follow it. Nothing is edited after approval.
LONG_TASK_SECONDS = 3600


class _LongTaskStore(Store):
    def create_task(self, principal, project_id, key, digest, payload):
        payload = {**payload, "max_duration_seconds": LONG_TASK_SECONDS}
        return super().create_task(principal, project_id, key, digest, payload)

    def revise_task_plan(self, principal, task_id, key, digest, payload):
        payload = {**payload, "max_duration_seconds": LONG_TASK_SECONDS}
        return super().revise_task_plan(principal, task_id, key, canonical_digest(payload), payload)


def _long_timed(tmp_path, monkeypatch, fail, **keywords):
    monkeypatch.setattr(execution_fixtures, "Store", _LongTaskStore)
    return _timed(tmp_path, monkeypatch, fail, **keywords)


def _stall(seconds, *, from_renewal=3):
    """Every control call hangs from the ``from_renewal``-th renewal for ``seconds``."""
    window = {"start": None}

    def fail(channel, path):
        now = channel.timeline.seconds
        if window["start"] is None and path == RENEW and channel.renew_attempts == from_renewal:
            window["start"] = now
        start = window["start"]
        return HANG if start is not None and now < start + seconds else None

    return fail, window


def _answered_renewals_after(channel, moment):
    """Renewal attempts that started at or after ``moment`` (all are answered after a stall)."""
    return [started for path, _, started in channel.calls if path == RENEW and started >= moment]


def test_the_lease_leaves_two_minutes_of_stall_margin():
    from tessera_worker.control_channel import RENEWAL_INTERVAL_SECONDS, WORKER_LEASE_SECONDS
    from tessera_worker.runtime import LEASE_SAFETY_MARGIN_SECONDS
    from tessera_worker.service import DevelopmentWorkerConfig

    assert WORKER_LEASE_SECONDS == LEASE == 150
    assert DevelopmentWorkerConfig.renew_after_seconds == RENEWAL_INTERVAL_SECONDS == 10.0
    # Worst phase: the stall begins as a renewal falls due, 10 s after the last confirmed one.
    assert LEASE - RENEWAL_INTERVAL_SECONDS - LEASE_SAFETY_MARGIN_SECONDS >= 120


def test_a_110_second_control_stall_is_survived(tmp_path, monkeypatch):
    """The incident, four times longer: renewals every 10 s, control silent for 110 s."""
    stall = 110.0
    fail, window = _stall(stall)
    service, channel, adapter, state = _long_timed(tmp_path, monkeypatch, fail, max_ticks=220)

    result = service.run()

    start = window["start"]
    assert start is not None and start >= 15  # two renewals were confirmed first
    assert channel.timeline.seconds > start + stall + 30  # the run went on well past the stall
    assert result["reason"] == "tick_limit_cancellation_requested"
    assert adapter.calls.count("cancel") == 1  # only the tick-limit cancellation
    retries = result["lease_renewal_retries"]
    # Reports and authority reads hang during the stall too, so few renewals fit in it.
    assert retries["failed_attempts"] >= 2
    assert retries["recovered_stalls"] == 1
    assert retries["unconfirmed_attempts"] == 0
    # A renewal sent after the stall was answered, and control's lease was extended past it.
    after = _answered_renewals_after(channel, start + stall)
    assert after
    assert _control_lease_expiry(state) > channel.timeline.base + timedelta(
        seconds=after[-1] + LEASE - 1
    )


def test_the_same_stall_killed_the_stage_on_a_30_second_lease(tmp_path, monkeypatch):
    """The stall test is real: with the old 30 s lease the same stall stops the stage."""
    monkeypatch.setattr(control_execution, "LEASE_SECONDS", 30)
    fail, window = _stall(110.0)
    service, _, adapter, state = _long_timed(tmp_path, monkeypatch, fail, max_ticks=220)

    result = service.run()

    # Since ADR 0188 (303, hand-off retry) the same kill can be recorded as quarantine_failed.
    assert result["reason"] in {
        "heartbeat_or_renewal_failure",
        "observation_delivery_pending",
        "quarantine_failed",
    }
    cancelled_at = adapter.cancelled_at[0]
    assert cancelled_at < _control_lease_expiry(state)


def test_a_stall_longer_than_the_margin_still_cancels_before_control_expires(tmp_path, monkeypatch):
    """Fail closed as before: a 200 s stall outlasts the lease; the cancel lands in time."""
    fail, window = _stall(200.0)
    service, channel, adapter, state = _long_timed(tmp_path, monkeypatch, fail, max_ticks=400)

    result = service.run()

    # Stopped by the renewal deadline, or by the tick's authority check near it (ADR 0178).
    # Since ADR 0188 (303, hand-off retry) the same cancel can be recorded as quarantine_failed.
    assert result["reason"] in {
        "heartbeat_or_renewal_failure",
        "observation_delivery_pending",
        "quarantine_failed",
    }
    if result["reason"] == "heartbeat_or_renewal_failure":
        assert "lease expired before a renewal was confirmed" in result["renewal_failure"]
    cancelled_at = adapter.cancelled_at[0]
    expiry = _control_lease_expiry(state)
    assert cancelled_at < expiry  # the stage stopped while control's lease was still live
    stalled_for = (cancelled_at - channel.timeline.base).total_seconds() - window["start"]
    # It held on through the whole margin (about 135 s), not a 30 s lease's 15 s.
    assert stalled_for > 120
    # Control's lease was never extended during the stall.
    assert (expiry - channel.timeline.base).total_seconds() < window["start"] + LEASE


# -- control: fencing is unchanged -----------------------------------------------------------


def _renew(execution, worker, lease, key, epoch=None):
    epoch = lease["epoch"] if epoch is None else epoch
    body = {"lease_id": lease["id"], "expected_epoch": epoch}
    return execution.renew_execution_lease(worker, lease["id"], key, canonical_digest(body), epoch)


def test_control_never_renews_an_expired_lease(tmp_path):
    _, execution, _, worker, _, lease = _delivery(tmp_path)
    issued = _parse_utc(lease["issued_at"])
    assert _parse_utc(lease["expires_at"]) == min(
        issued + timedelta(seconds=LEASE), _parse_utc(lease["hard_expires_at"])
    )
    execution._now = lambda: _parse_utc(lease["expires_at"]) - timedelta(seconds=1)
    status, renewed = _renew(execution, worker, lease, "renew-live")
    assert status == 200
    expired_at = _parse_utc(renewed["expires_at"])
    execution._now = lambda: expired_at + timedelta(seconds=1)
    with pytest.raises(Conflict, match="no longer current"):
        _renew(execution, worker, lease, "renew-late")


def test_control_refuses_a_renewal_after_the_epoch_moved_on(tmp_path):
    """A reassignment or revocation advances the epoch; the old holder is fenced at once."""
    _, execution, _, worker, grant, lease = _delivery(tmp_path)
    with _connect(execution.database_path) as connection:
        connection.execute(
            "UPDATE execution_grants SET epoch = epoch + 1 WHERE id = ?", (grant["id"],)
        )
    # The lease itself still has well over two minutes left, yet the renewal is refused.
    with pytest.raises(Conflict, match="epoch is no longer current"):
        _renew(execution, worker, lease, "renew-fenced")
    with pytest.raises(Conflict, match="no longer current"):
        _renew(execution, worker, lease, "renew-stale-epoch", epoch=lease["epoch"] + 1)


def test_the_longer_lease_never_passes_its_hard_expiry(tmp_path):
    _, execution, _, worker, _, lease = _delivery(tmp_path)
    now = _parse_utc(lease["issued_at"])
    hard = now + timedelta(seconds=40)
    with _connect(execution.database_path) as connection:
        connection.execute(
            "UPDATE execution_leases SET hard_expires_at = ? WHERE id = ?",
            (_utc_string(hard), lease["id"]),
        )
    execution._now = lambda: now + timedelta(seconds=5)
    _, renewed = _renew(execution, worker, lease, "renew-capped")
    assert _parse_utc(renewed["expires_at"]) == hard
