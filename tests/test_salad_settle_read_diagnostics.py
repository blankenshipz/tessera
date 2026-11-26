"""The pre-start settle read leaves a closed reason on the receipt and retries only transients.

Fakes only: a session whose observe (or heartbeat) raises during the settle wait after a
successful create. No provider, network or secret is touched."""

import json

import pytest
from test_salad_live_check import LocalSession, drive_clocked

from tessera_control import salad_live_check
from tessera_control.salad_live_check import (
    HTTP_STATUS_CLASSES,
    SETTLE_READ_ATTEMPTS,
    SETTLE_READ_BACKOFF_SECONDS,
    SETTLE_READ_CALLS,
    SETTLE_READ_REASONS,
    SETTLE_READ_WINDOW_SECONDS,
)
from tessera_control.salad_server import ProviderReadUnavailable
from tessera_control.store import Conflict

SECRET = "tok_SECRET-https://example.invalid/x?key=abc Authorization: Bearer abc"


def unavailable(*, detail=None, reason="provider_unavailable", field=None, text=SECRET):
    error = ProviderReadUnavailable(text)
    error.failure = error.reason = reason
    error.detail, error.field = detail, field
    return error


class SettleFailing(LocalSession):
    """The first ``len(errors)`` settle observes (after create, before start) raise."""

    def __init__(self, errors, *, keepalive_error=None):
        super().__init__()
        self.now = 0.0
        self.errors = list(errors)
        self.settle_observes = 0
        self.keepalive_error = keepalive_error

    read_cost = 0.0  # clock seconds one failing settle read spends (a live read burns its deadline)

    def keepalive(self, route):
        self.keepalives += 1
        if self.keepalive_error is not None and "create" in self.actions:
            raise self.keepalive_error

    def dispatch(self, route, action):
        if (
            action == "observe"
            and "create" in self.actions
            and not {"start", "stop"} & set(self.actions)
        ):
            self.settle_observes += 1
            if self.errors:
                self.actions.append(action)
                self.now += self.read_cost
                raise self.errors.pop(0)
        return super().dispatch(route, action)


def run(errors, **kwargs):
    session = SettleFailing(errors, **kwargs)
    return session, drive_clocked(session)


def assert_closed(block):
    assert set(block) == {
        "call",
        "reason",
        "http_status_class",
        "attempts",
        "outcome",
        "elapsed_seconds",
    }
    assert 0 <= block["elapsed_seconds"] <= SETTLE_READ_WINDOW_SECONDS + 10
    assert block["call"] in SETTLE_READ_CALLS
    assert block["reason"] in SETTLE_READ_REASONS
    assert block["http_status_class"] in HTTP_STATUS_CLASSES
    assert type(block["attempts"]) is int and block["attempts"] >= 1
    assert block["outcome"] in {"recovered", "failed"}


def assert_failed_closed(session, receipt):
    assert receipt["status"] == "inconclusive"
    assert "start" not in session.actions  # never treated as settled
    assert session.cancelled and session.actions.count("stop") == 1
    assert receipt.get("prestart_settle") is None


@pytest.mark.parametrize(
    "error,reason,status_class,attempts",
    [
        (unavailable(detail="non_2xx_5xx"), "read_unavailable", "5xx", None),
        (unavailable(detail="reset"), "read_unavailable", "none", None),
        (unavailable(detail="timeout"), "timeout", "none", None),
        (unavailable(detail="non_2xx_4xx"), "read_unavailable", "4xx", 1),
        (unavailable(detail="non_2xx_3xx"), "read_unavailable", "3xx", 1),
        (Conflict(SECRET), "conflict_state", "none", 1),
        (RuntimeError(SECRET), "other", "none", 1),
    ],
)
def test_each_closed_reason_is_recorded_and_fails_closed(error, reason, status_class, attempts):
    session, receipt = run([error] * 100)
    assert_failed_closed(session, receipt)
    block = receipt["prestart_settle_read"]
    assert_closed(block)
    attempts = attempts or session.settle_observes  # transient: bounded by the window
    assert attempts > 1 or attempts == session.settle_observes == 1
    assert {k: v for k, v in block.items() if k != "elapsed_seconds"} == {
        "call": "observe",
        "reason": reason,
        "http_status_class": status_class,
        "attempts": attempts,
        "outcome": "failed",
    }
    assert session.settle_observes == attempts
    assert receipt["prestart_failure"]["class"] == "group_unsettled"
    assert receipt["prestart_failure"]["exception"] == type(error).__name__


def test_the_smoke_failure_shape_now_carries_a_closed_reason():
    # A plain Conflict at the settle read (the live smoke's exception class).
    session, receipt = run([Conflict("Remote observation budget exhausted")])
    assert receipt["prestart_failure"] == {
        "class": "group_unsettled",
        "exception": "Conflict",
        "reason": "conflict",  # closed settle-conflict reason (ADR 0174)
    }
    assert receipt["prestart_settle_read"]["reason"] == "conflict_state"
    assert receipt["prestart_settle_read"]["attempts"] == 1


def test_a_wall_bound_timeout_at_the_settle_read_is_reached_and_retried():
    session, receipt = run([TimeoutError(SECRET)] * 100)
    assert_failed_closed(session, receipt)
    assert receipt["prestart_settle_read"]["reason"] == "timeout"
    assert receipt["prestart_settle_read"]["attempts"] == session.settle_observes > 1
    # The existing class of a bounded live operation that timed out is unchanged.
    assert receipt["prestart_failure"]["class"] == "wall_bound_exceeded"


@pytest.mark.parametrize("failures", range(1, 9))
def test_a_transient_settle_read_succeeds_on_retry_within_the_budget(failures):
    session, receipt = run([unavailable(detail="non_2xx_5xx")] * failures)
    assert receipt["status"] == "passed", receipt
    assert receipt["prestart_failure"] is None
    assert session.actions.count("start") == 1
    assert receipt["prestart_settle"]["settled"] is True
    assert receipt["prestart_settle"]["polls"] == 1  # retries are not extra polls
    block = receipt["prestart_settle_read"]
    assert_closed(block)
    assert {k: v for k, v in block.items() if k != "elapsed_seconds"} == {
        "call": "observe",
        "reason": "read_unavailable",
        "http_status_class": "5xx",
        "attempts": failures + 1,
        "outcome": "recovered",
    }
    assert block["elapsed_seconds"] == pytest.approx(session.now, abs=0.1)
    assert session.now >= sum(SETTLE_READ_BACKOFF_SECONDS[:failures])


def test_heartbeats_continue_between_settle_read_attempts():
    failures = 6
    session, receipt = run([unavailable(detail="timeout")] * failures)
    assert receipt["status"] == "passed"
    # one beat before the read, one between each retry, then the run's own beats
    assert session.keepalives >= failures + 1


def test_bound_exhausted_fails_closed_with_the_reason():
    session, receipt = run([unavailable(detail="non_2xx_5xx")] * (SETTLE_READ_ATTEMPTS + 5))
    assert_failed_closed(session, receipt)
    assert receipt["prestart_settle_read"]["attempts"] == session.settle_observes
    assert receipt["prestart_settle_read"]["outcome"] == "failed"
    assert 1 < session.settle_observes <= SETTLE_READ_ATTEMPTS
    assert session.now <= SETTLE_READ_WINDOW_SECONDS


def test_conflict_state_is_not_retried():
    session, receipt = run([Conflict(SECRET)] * 2)
    assert_failed_closed(session, receipt)
    assert session.settle_observes == 1
    assert receipt["prestart_settle_read"]["attempts"] == 1


def test_a_deterministic_read_failure_is_not_retried():
    error = unavailable(reason="identity_mismatch", field="gpu_classes")
    session, receipt = run([error] * 100)
    assert_failed_closed(session, receipt)
    assert session.settle_observes == 1
    assert receipt["prestart_settle_read"]["reason"] == "read_unavailable"


def test_retries_never_pass_the_run_deadline():
    session = SettleFailing([unavailable(detail="non_2xx_5xx")] * 100)
    session.now = 0.0
    receipt = drive_clocked(session, max_seconds=1)
    assert_failed_closed(session, receipt)
    # The first pause is cut to the deadline; no retry starts after it.
    assert session.settle_observes == 2
    assert session.now <= 1


def test_a_keepalive_failure_during_settle_is_recorded_as_its_own_call():
    session = SettleFailing([], keepalive_error=Conflict(SECRET))
    # Nothing retained yet, so a refused heartbeat is a failure, not completion.
    session.proof["output_retained"] = False
    receipt = drive_clocked(session)
    assert_failed_closed(session, receipt)
    assert receipt["prestart_settle_read"] == {
        "call": "keepalive",
        "reason": "conflict_state",
        "http_status_class": "none",
        "attempts": 1,
        "outcome": "failed",
        "elapsed_seconds": 0.0,
    }
    assert session.settle_observes == 0


@pytest.mark.parametrize(
    "error",
    [
        unavailable(detail="non_2xx_5xx"),
        unavailable(detail=SECRET),
        Conflict(SECRET),
        RuntimeError(SECRET),
        TimeoutError(SECRET),
    ],
)
def test_secret_looking_exception_text_never_reaches_the_receipt(error):
    _, receipt = run([error] * 100)
    text = json.dumps(receipt)
    for fragment in ("SECRET", "example.invalid", "Bearer", "key=abc"):
        assert fragment not in text
    assert receipt["prestart_settle_read"]["http_status_class"] == "none" or (
        receipt["prestart_settle_read"]["http_status_class"] == "5xx"
    )


def test_a_clean_settle_leaves_the_field_absent_so_digests_are_unchanged():
    session, receipt = run([])
    assert receipt["status"] == "passed"
    assert "prestart_settle_read" not in receipt


def test_budget_constants_are_bounded():
    # A post-create window long enough for several 2 s reads plus Salad's create-to-GET lag,
    # never open-ended: a time window and a hard attempt cap, each pause under the guardian
    # silence bound, and the whole window inside the run's own default wall bound.
    assert 2 * 10 <= SETTLE_READ_WINDOW_SECONDS <= 120
    assert 1 < SETTLE_READ_ATTEMPTS <= 50
    assert all(0 < pause <= 10 for pause in SETTLE_READ_BACKOFF_SECONDS)
    assert max(SETTLE_READ_BACKOFF_SECONDS) < salad_live_check.GUARDIAN_SILENCE_SECONDS


def timeouts(count):
    return [unavailable(detail="timeout") for _ in range(count)]


def test_slow_first_reads_after_create_still_reach_start():
    # (a) create succeeds; the next reads each burn the 2 s read deadline and time out (more
    # than the old three attempts), then the created stopped group reads: start is dispatched.
    session = SettleFailing(timeouts(6))
    session.read_cost = 2.0
    receipt = drive_clocked(session)
    assert receipt["status"] == "passed", receipt
    assert session.actions.count("create") == 1 and session.actions.count("start") == 1
    assert session.actions.index("start") > session.actions.index("create")
    block = receipt["prestart_settle_read"]
    assert_closed(block)
    assert block["outcome"] == "recovered" and block["reason"] == "timeout"
    assert block["attempts"] == 7
    assert block["elapsed_seconds"] > 6 * 2.0


def test_reads_that_never_settle_past_the_window_fail_closed_without_start():
    # (b) create succeeds and every read times out past the whole window.
    session = SettleFailing(timeouts(100))
    session.read_cost = 2.0
    receipt = drive_clocked(session, max_seconds=1000)
    assert_failed_closed(session, receipt)
    assert receipt["prestart_failure"]["class"] == "group_unsettled"
    block = receipt["prestart_settle_read"]
    assert_closed(block)
    assert block["reason"] == "timeout" and block["outcome"] == "failed"
    assert block["attempts"] == session.settle_observes > 3
    assert (
        SETTLE_READ_WINDOW_SECONDS - 10
        <= block["elapsed_seconds"]
        <= SETTLE_READ_WINDOW_SECONDS + 2
    )
    assert session.now <= SETTLE_READ_WINDOW_SECONDS + 2  # bounded, not open-ended


def test_a_group_readable_on_the_first_read_starts_as_before():
    # (c)
    session, receipt = run([])
    assert receipt["status"] == "passed"
    assert session.settle_observes >= 1 and session.actions.count("start") == 1
    assert "prestart_settle_read" not in receipt
