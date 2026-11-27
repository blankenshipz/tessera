"""Pre-start input delivery is retried, bounded, while the guardian does not answer.

After a guardian restart its IPC can be busy for a while. Input delivery happens with the group
created but stopped (unbilled), so a ``GuardianUnavailable`` there is retried after a short pause
and a beat, a bounded number of times and within a bounded window; each retry is recorded on the
receipt. A refusal or any other failure is not retried. Fakes only: no provider, no network.
"""

from __future__ import annotations

import pytest
from test_salad_live_check import LocalSession, drive

from tessera_control.salad_live_check import (
    DELIVERY_RETRY_ATTEMPTS,
    DELIVERY_RETRY_SECONDS,
    PrestartStepError,
)
from tessera_control.salad_server import GuardianRefused, GuardianUnavailable


def _mint_failed(cause):
    error = PrestartStepError("ticket_mint_failed")
    error.__cause__ = cause
    return error


class FlakySession(LocalSession):
    """Delivery fails with the queued errors, then succeeds."""

    def __init__(self, errors, *, clock_step=0.0):
        super().__init__()
        self.expects_input = True
        self.errors = list(errors)
        self.now = 0.0
        self.clock_step = clock_step

    def deliver_inputs(self, route):
        self.input_calls.append("deliver")
        self.now += self.clock_step
        if self.errors:
            raise self.errors.pop(0)


def test_an_unavailable_guardian_is_retried_and_the_run_starts():
    session = FlakySession(
        [_mint_failed(GuardianUnavailable()), _mint_failed(GuardianUnavailable())]
    )
    receipt = drive(session)
    assert session.input_calls.count("deliver") == 3
    assert receipt["input_delivered"] is True
    assert "start" in session.actions
    assert receipt["prestart_failure"] is None
    retries = receipt["prestart_delivery_retries"]
    assert [r["exception"] for r in retries] == ["GuardianUnavailable"] * 2
    assert [r["pause_seconds"] for r in retries] == [2, 4]


def test_the_retry_is_bounded_then_fails_before_start():
    errors = [_mint_failed(GuardianUnavailable()) for _ in range(DELIVERY_RETRY_ATTEMPTS + 5)]
    session = FlakySession(errors)
    receipt = drive(session)
    assert session.input_calls.count("deliver") == DELIVERY_RETRY_ATTEMPTS + 1
    assert "start" not in session.actions
    assert receipt["prestart_failure"] == {
        "class": "ticket_mint_failed",
        "exception": "GuardianUnavailable",
    }
    assert len(receipt["prestart_delivery_retries"]) == DELIVERY_RETRY_ATTEMPTS


def test_the_retry_window_is_bounded_in_time():
    from test_salad_live_check import drive_clocked

    # Each delivery call takes 12 s (the IPC timeout): the window allows one retry only.
    errors = [_mint_failed(GuardianUnavailable()) for _ in range(10)]
    session = FlakySession(errors, clock_step=12.0)
    receipt = drive_clocked(session, max_seconds=600)
    calls = session.input_calls.count("deliver")
    assert 1 < calls <= DELIVERY_RETRY_ATTEMPTS + 1
    spent = sum(r["pause_seconds"] for r in receipt["prestart_delivery_retries"])
    assert spent + 12.0 * (calls - 1) <= DELIVERY_RETRY_SECONDS + 12.0
    assert "start" not in session.actions


@pytest.mark.parametrize(
    "cause",
    [GuardianRefused("guardian_loop_unready"), KeyError("x"), None],
    ids=["refused", "other", "uncaused"],
)
def test_anything_but_an_unavailable_guardian_is_not_retried(cause):
    error = PrestartStepError("ticket_mint_failed") if cause is None else _mint_failed(cause)
    session = FlakySession([error])
    receipt = drive(session)
    assert session.input_calls.count("deliver") == 1
    assert "prestart_delivery_retries" not in receipt
    assert "start" not in session.actions
