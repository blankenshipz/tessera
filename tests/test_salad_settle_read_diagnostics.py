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
                raise self.errors.pop(0)
        return super().dispatch(route, action)


def run(errors, **kwargs):
    session = SettleFailing(errors, **kwargs)
    return session, drive_clocked(session)


def assert_closed(block):
    assert set(block) == {"call", "reason", "http_status_class", "attempts", "outcome"}
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
        (unavailable(detail="non_2xx_5xx"), "read_unavailable", "5xx", SETTLE_READ_ATTEMPTS),
        (unavailable(detail="reset"), "read_unavailable", "none", SETTLE_READ_ATTEMPTS),
        (unavailable(detail="timeout"), "timeout", "none", SETTLE_READ_ATTEMPTS),
        (unavailable(detail="non_2xx_4xx"), "read_unavailable", "4xx", 1),
        (unavailable(detail="non_2xx_3xx"), "read_unavailable", "3xx", 1),
        (Conflict(SECRET), "conflict_state", "none", 1),
        (RuntimeError(SECRET), "other", "none", 1),
    ],
)
def test_each_closed_reason_is_recorded_and_fails_closed(error, reason, status_class, attempts):
    session, receipt = run([error] * 10)
    assert_failed_closed(session, receipt)
    block = receipt["prestart_settle_read"]
    assert_closed(block)
    assert block == {
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
    session, receipt = run([TimeoutError(SECRET)] * 10)
    assert_failed_closed(session, receipt)
    assert receipt["prestart_settle_read"]["reason"] == "timeout"
    assert receipt["prestart_settle_read"]["attempts"] == SETTLE_READ_ATTEMPTS
    # The existing class of a bounded live operation that timed out is unchanged.
    assert receipt["prestart_failure"]["class"] == "wall_bound_exceeded"


@pytest.mark.parametrize("failures", range(1, SETTLE_READ_ATTEMPTS))
def test_a_transient_settle_read_succeeds_on_retry_within_the_budget(failures):
    session, receipt = run([unavailable(detail="non_2xx_5xx")] * failures)
    assert receipt["status"] == "passed", receipt
    assert receipt["prestart_failure"] is None
    assert session.actions.count("start") == 1
    assert receipt["prestart_settle"]["settled"] is True
    assert receipt["prestart_settle"]["polls"] == 1  # retries are not extra polls
    assert receipt["prestart_settle_read"] == {
        "call": "observe",
        "reason": "read_unavailable",
        "http_status_class": "5xx",
        "attempts": failures + 1,
        "outcome": "recovered",
    }
    assert session.now >= sum(SETTLE_READ_BACKOFF_SECONDS[:failures])


def test_heartbeats_continue_between_settle_read_attempts():
    session, receipt = run([unavailable(detail="timeout")] * (SETTLE_READ_ATTEMPTS - 1))
    assert receipt["status"] == "passed"
    # one beat before the read, one between each retry, then the run's own beats
    assert session.keepalives >= SETTLE_READ_ATTEMPTS


def test_bound_exhausted_fails_closed_with_the_reason():
    session, receipt = run([unavailable(detail="non_2xx_5xx")] * SETTLE_READ_ATTEMPTS)
    assert_failed_closed(session, receipt)
    assert receipt["prestart_settle_read"]["attempts"] == SETTLE_READ_ATTEMPTS
    assert receipt["prestart_settle_read"]["outcome"] == "failed"
    assert session.settle_observes == SETTLE_READ_ATTEMPTS


def test_conflict_state_is_not_retried():
    session, receipt = run([Conflict(SECRET)] * 2)
    assert_failed_closed(session, receipt)
    assert session.settle_observes == 1
    assert receipt["prestart_settle_read"]["attempts"] == 1


def test_a_deterministic_read_failure_is_not_retried():
    error = unavailable(reason="identity_mismatch", field="gpu_classes")
    session, receipt = run([error] * 10)
    assert_failed_closed(session, receipt)
    assert session.settle_observes == 1
    assert receipt["prestart_settle_read"]["reason"] == "read_unavailable"


def test_retries_never_pass_the_run_deadline():
    session = SettleFailing([unavailable(detail="non_2xx_5xx")] * 10)
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
    _, receipt = run([error] * 10)
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


def test_budget_constants_are_small_and_bounded():
    assert 1 < SETTLE_READ_ATTEMPTS <= 4
    assert len(SETTLE_READ_BACKOFF_SECONDS) == SETTLE_READ_ATTEMPTS - 1
    assert all(0 < pause <= 10 for pause in SETTLE_READ_BACKOFF_SECONDS)
    assert sum(SETTLE_READ_BACKOFF_SECONDS) < salad_live_check.GUARDIAN_SILENCE_SECONDS
