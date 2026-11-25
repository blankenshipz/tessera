"""Pre-start provider read timeouts: bounded per-request retry, a long overall deadline, and a
closed reason for a refused settle observe. Fakes only; nothing is billable (0c) before an
instance is placed."""

import time

import pytest
from test_salad_guardian_prestart_read import poll, world  # noqa: F401  (fixture)
from test_salad_live_check import LocalSession, drive
from test_salad_safety_service import service_case  # noqa: F401  (fixture)

from tessera_control import salad_safety_service as service
from tessera_control.remote_execution import RouteRevisionChanged
from tessera_control.salad_guardian import DEFAULT_PRESTART_READ_DEADLINE_SECONDS
from tessera_control.salad_http import READ_DEADLINE_SECONDS
from tessera_control.store import Conflict


def test_request_timeout_is_separate_from_and_far_shorter_than_the_prestart_deadline():
    assert service.PROVIDER_READ_SECONDS == float(READ_DEADLINE_SECONDS)
    assert service.PROVIDER_READ_SECONDS * 10 <= DEFAULT_PRESTART_READ_DEADLINE_SECONDS
    assert service.PROVIDER_READ_BUDGET_SECONDS < DEFAULT_PRESTART_READ_DEADLINE_SECONDS


def test_default_backoff_sleep_is_jittered_within_the_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    for _ in range(50):
        service.SaladSafetyService._jittered_sleep(0.4)
    assert all(0.2 <= seconds <= 0.4 for seconds in slept) and len(set(slept)) > 1


def _timeout_reads(s, times):
    send, left = s._send, [times]

    def flaky(row, action, *args, **kwargs):
        if action in {"read_group", "read_instance"} and left[0]:
            left[0] -= 1
            raise TimeoutError("service_deadline")
        return send(row, action, *args, **kwargs)

    s._send = flaky


def test_group_read_timing_out_twice_then_answering_is_not_stopped(service_case):  # noqa: F811
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    _timeout_reads(s, 2)
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "armed" and status["stop_cause"] is None
    assert status["provider_read_retries"] == 2 and slept == [0.2, 0.4]
    assert "stop" not in provider.calls


def test_group_read_always_timing_out_stops_within_the_poststart_limit(service_case):  # noqa: F811
    s, b, request, token, clock, provider, *_ = service_case
    s.retry_sleep = lambda _: None
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    _timeout_reads(s, 10**6)
    clock[0] += 5
    s.loop()
    # The run already began, so the first failed poll is retried (q-2505) ...
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "armed" and status["stop_cause"] is None
    for wait in (5, 10):  # ... but the third consecutive failed poll stops it, as before
        s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
        clock[0] += wait  # the guardian's growing read-retry backoff
        s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "unknown" and status["stop_cause"] == "provider_unavailable"
    assert status["provider_read_retries"] == 6  # two in-pass retries on each of three polls


def test_guardian_read_timing_out_twice_then_answering_keeps_the_run(world):  # noqa: F811
    guardian, clock, seen, started, stops, _ = world
    seen["fail"] = TimeoutError()
    poll(guardian, clock)
    status = poll(guardian, clock)
    assert status["prestart_read_retries"]["count"] == 2 and stops == []
    seen["fail"] = None
    status = poll(guardian, clock)
    assert status["status"] == "armed" and stops == [] and status["stop_cause"] is None


def test_guardian_read_always_timing_out_until_the_deadline_stops_at_zero_cost(world):  # noqa: F811
    guardian, clock, seen, started, stops, make = world
    guardian = make("long.db")  # the default overall deadline
    seen["fail"] = TimeoutError()
    elapsed = 0
    while elapsed < DEFAULT_PRESTART_READ_DEADLINE_SECONDS - 5:
        status = poll(guardian, clock, 5)
        elapsed += 5
        assert stops == [] and status["stop_cause"] is None
    status = poll(guardian, clock, 30)
    assert status["stop_cause"] == "provider_unavailable" and len(stops) == 1
    assert not started[0]  # custody never began: nothing was billable


class ConflictingObserve(LocalSession):
    def __init__(self, conflicts, error):
        super().__init__()
        self.conflicts, self.error, self.observes = conflicts, error, 0

    def dispatch(self, route, action):
        if action == "observe":
            self.observes += 1
            if self.conflicts:
                self.conflicts -= 1
                raise self.error
        return super().dispatch(route, action)


def test_settle_route_revision_conflict_is_retried_once_and_the_run_proceeds():
    session = ConflictingObserve(1, RouteRevisionChanged("Route changed since it was evaluated."))
    receipt = drive(session, max_polls=4)
    assert receipt["settle_conflict_retries"] == 1
    assert receipt["prestart_failure"] is None and "start" in session.actions


def test_settle_route_revision_conflict_twice_fails_with_a_closed_reason():
    session = ConflictingObserve(9, RouteRevisionChanged("Route changed since it was evaluated."))
    receipt = drive(session, max_polls=4)
    assert receipt["settle_conflict_retries"] == 1 and session.observes >= 2
    assert receipt["prestart_failure"] == {
        "class": "group_unsettled",
        "exception": "RouteRevisionChanged",
        "reason": "route_revision_changed",
    }
    assert "start" not in session.actions and receipt["status"] == "inconclusive"


def test_other_conflicts_are_not_retried_and_get_a_closed_reason_without_text():
    session = ConflictingObserve(9, Conflict("SECRET text must not appear"))
    receipt = drive(session, max_polls=4)
    assert receipt["settle_conflict_retries"] == 0 and session.observes <= 2
    assert receipt["prestart_failure"]["reason"] == "conflict"
    assert "SECRET" not in str(receipt)
    assert "start" not in session.actions


@pytest.mark.parametrize("error", [RuntimeError("x"), ValueError("y")])
def test_non_conflict_errors_carry_no_reason(error):
    receipt = drive(ConflictingObserve(9, error), max_polls=4)
    assert "reason" not in receipt["prestart_failure"]
