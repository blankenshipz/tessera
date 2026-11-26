"""Bounded retries of a failed step before the Salad create effect (fakes only, no real sleeps)."""

import sqlite3

import pytest
from test_salad_live_check import LocalSession, drive_clocked

from tessera_control import salad_precreate as pc
from tessera_control.salad_live_check import (
    CancelUnrecorded,
    GuardianCancelUndelivered,
    SaladLiveSession,
)
from tessera_control.salad_precreate import CreateNotAttempted, dispatch_create
from tessera_control.salad_server import (
    GuardianRefused,
    GuardianUnavailable,
    ProviderReadUnavailable,
)


def read_failure(reason):
    error = ProviderReadUnavailable("Fresh provider preflight unavailable.")
    error.reason, error.field = reason, None
    return error


class FlakyCreate(LocalSession):
    def __init__(self, failures, error_factory):
        super().__init__()
        self.now = 0.0
        self.failures, self.error_factory = failures, error_factory
        self.create_attempts = 0
        self.posts = 0

    def dispatch(self, route, action):
        if action == "create":
            self.create_attempts += 1
            if self.failures:
                self.failures -= 1
                raise self.error_factory()
            self.posts += 1
        if action == "stop" and not create_effects(self):
            # What SaladLiveSession.dispatch does: no stop for a never-created group.
            self.actions.append(action)
            raise CreateNotAttempted()
        return super().dispatch(route, action)


def create_effects(session):
    return [e for e in session.record["effects"] if e["action"] == "create"]


def test_create_preflight_transient_read_is_retried_and_create_post_sent_once():
    session = FlakyCreate(1, lambda: read_failure("timeout"))
    receipt = drive_clocked(session)
    assert receipt["status"] == "passed", receipt
    assert session.create_attempts == 2 and session.posts == 1
    assert len(create_effects(session)) == 1
    assert receipt["create_preflight_retries"] == 1
    assert receipt["reason"] != pc.CREATE_NOT_ATTEMPTED


def test_deterministic_precreate_failure_is_not_retried_and_sends_no_stop():
    session = FlakyCreate(99, lambda: read_failure("identity_mismatch"))
    receipt = drive_clocked(session)
    assert session.create_attempts == 1
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == pc.CREATE_NOT_ATTEMPTED
    assert "create_preflight_retries" not in receipt
    assert not [e for e in session.record["effects"] if e["action"] == "stop"]
    assert create_effects(session) == []


def test_plain_conflict_is_not_retried():
    session = FlakyCreate(99, lambda: ValueError("conflict"))
    receipt = drive_clocked(session)
    assert session.create_attempts == 1
    assert receipt["reason"] == pc.CREATE_NOT_ATTEMPTED
    assert receipt["reason"] != "custody_stop_or_reconnect_unresolved"
    assert not [e for e in session.record["effects"] if e["action"] == "stop"]


def test_all_attempts_exhausted_reports_create_not_attempted():
    session = FlakyCreate(99, lambda: read_failure("timeout"))
    receipt = drive_clocked(session)
    assert session.create_attempts == pc.CREATE_PREFLIGHT_ATTEMPTS
    assert receipt["reason"] == pc.CREATE_NOT_ATTEMPTED
    assert receipt["create_preflight_retries"] == pc.CREATE_PREFLIGHT_ATTEMPTS - 1
    assert not [e for e in session.record["effects"] if e["action"] == "stop"]
    assert session.posts == 0


class Harness:
    """A dispatch that fails with the given errors in order, then succeeds."""

    def __init__(self, errors, state=None, state_error=None):
        self.errors = list(errors)
        self.state, self.state_error = state, state_error
        self.calls, self.sleeps, self.now = 0, [], 0.0
        self.receipt = {}

    def dispatch(self, action):
        assert action == "create"
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"ok": True}

    def read_state(self):
        if self.state_error:
            raise self.state_error
        return self.state if self.state is not None else {"effects": []}

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, deadline=1000.0):
        return dispatch_create(
            self.dispatch,
            self.read_state,
            clock=lambda: self.now,
            sleep=self.sleep,
            deadline=deadline,
            receipt=self.receipt,
        )


def test_effect_appearing_after_failure_propagates_without_retry():
    h = Harness([TimeoutError("slow")], state={"effects": [{"action": "create"}]})
    with pytest.raises(TimeoutError):
        h.run()
    assert h.calls == 1 and h.sleeps == []
    assert "reason" not in h.receipt and "create_preflight_retries" not in h.receipt


def test_state_read_failure_means_no_retry():
    h = Harness([TimeoutError("slow")], state_error=sqlite3.OperationalError("locked"))
    with pytest.raises(TimeoutError):
        h.run()
    assert h.calls == 1 and h.sleeps == []
    assert "reason" not in h.receipt


def test_never_sleeps_past_the_deadline():
    h = Harness([TimeoutError("slow"), TimeoutError("slow")])
    assert h.run(deadline=3.0) == {"ok": True}
    assert h.sleeps == [2, 1.0]  # second pause (4 s) is cut to the 1 s left
    h = Harness([TimeoutError("slow")] * 3)
    h.now = 5.0
    with pytest.raises(TimeoutError):
        h.run(deadline=3.0)
    assert h.calls == 1 and h.sleeps == []
    assert h.receipt["reason"] == pc.CREATE_NOT_ATTEMPTED


def test_receipt_without_a_retry_has_no_retry_key():
    h = Harness([])
    assert h.run() == {"ok": True}
    assert h.receipt == {}


def test_backoff_and_retry_count_on_the_receipt():
    h = Harness([TimeoutError()] * 3)
    assert h.run() == {"ok": True}
    assert h.sleeps == [2, 4, 8] and h.receipt == {"create_preflight_retries": 3}


def test_guardian_refusal_codes_retry_only_when_transient():
    h = Harness([GuardianRefused("single_operation_orphan_exposure")])
    assert h.run() == {"ok": True} and h.calls == 2
    h = Harness([GuardianRefused("budget_insufficient")])
    with pytest.raises(GuardianRefused):
        h.run()
    assert h.calls == 1 and h.receipt["reason"] == pc.CREATE_NOT_ATTEMPTED


def test_guardian_unavailable_is_retried():
    h = Harness([GuardianUnavailable()])
    assert h.run() == {"ok": True} and h.calls == 2


def session_with(state=None, **attrs):
    session = object.__new__(SaladLiveSession)
    for name, value in attrs.items():
        setattr(session, name, value)
    if state is not None:
        session.state = lambda route: state
    return session


def test_stop_dispatch_without_create_effect_raises_create_not_attempted():
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError("dispatcher touched: " + name)

    session = session_with({"effects": []}, dispatcher=Untouchable(), server=Untouchable())
    with pytest.raises(CreateNotAttempted):
        session.dispatch("route", "stop")


class FakeStore:
    def __init__(self, error=None):
        self.error, self.calls = error, 0
        self.worker = object()

    def worker_cancel(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error


class FakeSafety:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    def cancel(self, state):
        self.calls += 1
        if self.error:
            raise self.error


def cancel_session(store, safety):
    server = type("Server", (), {})()
    server.store, server.safety, server.worker = store, safety, store.worker
    return session_with({"desired": "run", "revision": 3, "proposal_digest": "d"}, server=server)


def test_cancel_with_no_guardian_reservation_is_recorded_not_unrecorded():
    store, safety = FakeStore(), FakeSafety(GuardianRefused("reservation_missing"))
    cancel_session(store, safety).cancel("route")  # no exception
    assert store.calls == 1 and safety.calls == 1


def test_cancel_other_guardian_refusal_still_raises():
    # Any refusal other than reservation_missing stays an undelivered guardian cancel (the
    # completion tail retries it); the refusal itself is the cause.
    session = cancel_session(FakeStore(), FakeSafety(GuardianRefused("guardian_loop_unready")))
    with pytest.raises(GuardianCancelUndelivered) as caught:
        session.cancel("route")
    assert isinstance(caught.value.__cause__, GuardianRefused)
    assert caught.value.__cause__.code == "guardian_loop_unready"


def test_cancel_unrecorded_at_control_still_raises_even_if_guardian_has_no_reservation():
    store, safety = (
        FakeStore(sqlite3.OperationalError("locked")),
        FakeSafety(GuardianRefused("reservation_missing")),
    )
    with pytest.raises(CancelUnrecorded) as caught:
        cancel_session(store, safety).cancel("route")
    assert caught.value.guardian_cancel_sent is False
