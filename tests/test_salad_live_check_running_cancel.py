"""A still-running route is not cancelled for a stall; every cancel says why; a failed cancel
is never reported as a settled stop.

Isolated orchestration only (fake sessions and a fake control): never provider evidence.
"""

import json
import sqlite3

import pytest
from test_salad_live_check import LocalSession

from tessera_control import campaign_salad
from tessera_control.campaign_salad_executor import (
    GROUP_STOP_MARKER,
    LIVE_CHECK_STDERR_UNCLASSIFIED,
    _interrupted_after_custody,
    live_check_stderr_class,
)
from tessera_control.salad_live_check import (
    CANCEL_ATTEMPTS,
    CANCEL_CAUSE_CLASSES,
    CANCEL_UNRECORDED_REASON,
    PRECUSTODY_REALLOCATION,
    RUNNING_CANCEL_PREFIX,
    CancelUnrecorded,
    ControlStall,
    SaladLiveSession,
    _drive,
)
from tessera_control.salad_server import GuardianRefused, ProviderReadUnavailable

SECRET = "tok_SECRET-https://example.invalid/x?key=abc"


def read_timeout():
    error = ProviderReadUnavailable("Fresh provider preflight unavailable.")
    error.failure, error.detail = "provider_unavailable", "timeout"
    return error


class RunningSession(LocalSession):
    """After start the group reads ``running``; output is retained after ``retain_after``
    successful observes (never, when None). Scripted errors per call, a clock per sleep."""

    def __init__(self, *, retain_after=None, observe_errors=(), beat_errors=(), stop_cause=None):
        super().__init__()
        self.now = 0.0
        self.retain_after = retain_after
        self.observe_errors = list(observe_errors)
        self.beat_errors = list(beat_errors)
        self.proof = dict(self.proof, output_retained=False, stop_confirmed=False)
        self.stop_cause = stop_cause
        self.cancel_errors = []
        self.cancel_calls = 0
        self.running_observes = 0

    def dispatch(self, route, action):
        if action == "observe" and "start" in self.actions and not self.cancelled:
            if self.observe_errors:
                self.actions.append(action)
                error = self.observe_errors.pop(0)
                if error is not None:
                    raise error
            state = super().dispatch(route, action)
            self.running_observes += 1
            if self.retain_after is not None and self.running_observes > self.retain_after:
                self.proof.update(output_retained=True, stop_confirmed=True)
            if self.stop_cause is not None:
                self.proof["stop_cause"] = self.stop_cause
            return dict(state, provider_status="running")
        return super().dispatch(route, action)

    def keepalive(self, route):
        self.keepalives += 1
        if "start" in self.actions and self.beat_errors:
            error = self.beat_errors.pop(0)
            if error is not None:
                raise error

    def cancel(self, route):
        self.cancel_calls += 1
        if self.cancel_errors:
            error = self.cancel_errors.pop(0)
            if error is not None:
                raise error
        super().cancel(route)


def drive(session, *, max_polls=40, max_seconds=400):
    def sleep(seconds):
        session.now += seconds

    return _drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=max_seconds,
        max_polls=max_polls,
        sleep=sleep,
        clock=lambda: session.now,
    )


def cancelled_before_output(session):
    """The driver's own cancel came before the output was retained."""
    return session.cancelled and not session.proof["output_retained"]


# --- stalls while running are skipped, not cancelled ----------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        read_timeout,
        lambda: TimeoutError("bounded live operation expired"),
        lambda: sqlite3.OperationalError("database is locked"),
    ],
)
def test_running_route_survives_repeated_observe_stalls(error):
    # Five failed reads in a row: past the earlier bound of three, but the group runs.
    session = RunningSession(retain_after=2, observe_errors=[None] + [error() for _ in range(5)])
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["observe_transient_failures"] == 5
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["last_provider_status"] == "running"


def test_running_route_survives_a_stalled_guardian_status_read():
    session = RunningSession(retain_after=3)
    calls = {"n": 0}
    original = session.evidence

    def evidence(route):
        calls["n"] += 1
        if 2 <= calls["n"] <= 6:
            raise sqlite3.OperationalError("database is locked")
        return original(route)

    session.evidence = evidence
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["observe_transient_failures"] == 5


def test_running_route_survives_control_stalls_while_the_guardian_takes_its_beat():
    # The lease renewal stalls on a control write lock five beats in a row; the guardian
    # heartbeat itself still goes out, so nothing is silent and nothing is cancelled.
    stalls = [None] + [ControlStall(beat_delivered=True) for _ in range(5)]
    session = RunningSession(retain_after=6, beat_errors=stalls)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["control_stalls"] == 5
    assert receipt["guardian_refusal"] is None


def test_a_deterministic_read_failure_while_running_stays_bounded():
    def mismatch():
        error = ProviderReadUnavailable("x")
        error.failure, error.detail = "identity_mismatch", None
        return error

    session = RunningSession(observe_errors=[None] + [mismatch() for _ in range(5)])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["observe_transient_failures"] == 2
    assert receipt["cancel_cause"]["class"] == "observe_timeout"
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "observe_timeout"


def test_observe_stalls_before_running_keep_the_earlier_bound():
    # The group never read running after start: three failed reads still end the run.
    session = RunningSession(observe_errors=[read_timeout() for _ in range(5)])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["last_provider_status"] is None
    assert receipt["observe_transient_failures"] == 2
    assert receipt["cancel_cause"]["class"] == "observe_timeout"
    assert receipt["reason"] == "custody_stop_or_reconnect_unresolved"


# --- a cancel that does happen records a closed reason ----------------------------------------


def test_heartbeat_stop_only_on_a_running_route_is_recorded_not_a_preemption():
    # The incident shape: the group reads running, the guardian refuses the heartbeat with
    # stop_only and has recorded no stop cause of its own.
    session = RunningSession(beat_errors=[None, None, GuardianRefused("stop_only")])
    session.proof.update(custody_work_begun=True, provider_stop_confirmed=True)
    receipt = drive(session)
    assert cancelled_before_output(session)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "guardian_refusal"
    assert cause["guardian_refusal"] == "stop_only"
    assert cause["last_provider_status"] == "running"
    assert cause["exception"] == "GuardianRefused"
    assert receipt["guardian_stop_cause"] is None
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "guardian_refusal"
    # Neither the executor nor the campaign driver reads it as a provider move.
    assert not _interrupted_after_custody(receipt)
    assert not campaign_salad._guardian_precustody_evidence(receipt)
    assert not campaign_salad._guardian_interruption_evidence(receipt)
    assert receipt["reason"] != PRECUSTODY_REALLOCATION


def test_heartbeat_silence_on_a_blocked_control_is_recorded_as_silence_bound():
    # Control is write-locked and the guardian beat cannot go out either: tolerated only while
    # the next beat still lands inside the silence bound, then cancelled with that reason.
    locked = [None] + [sqlite3.OperationalError("database is locked") for _ in range(10)]
    session = RunningSession(beat_errors=locked)
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "silence_bound"
    assert receipt["cancel_cause"]["heartbeat_silent_seconds"] >= 10
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "silence_bound"
    assert receipt["control_stalls"] >= 1


def test_a_guardian_stop_cause_keeps_the_guardians_word():
    session = RunningSession(beat_errors=[None, None, GuardianRefused("stop_only")])
    session.stop_cause = "missed_poll"
    receipt = drive(session)
    assert receipt["cancel_cause"]["class"] == "guardian_stop_cause"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


def test_a_budget_refusal_is_a_budget_cancel():
    session = RunningSession(beat_errors=[None, None, GuardianRefused("budget_binding_expired")])
    receipt = drive(session)
    assert receipt["cancel_cause"]["class"] == "budget"
    assert receipt["reason"] == "custody_stop_or_reconnect_unresolved"


def test_the_deadline_is_a_deadline_cancel():
    session = RunningSession()
    receipt = drive(session, max_polls=40, max_seconds=50)
    assert receipt["cancel_cause"]["class"] == "deadline"
    assert receipt["cancel_cause"]["last_provider_status"] == "running"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


def test_cancel_cause_is_closed_and_a_non_refusal_carries_no_text():
    # Only a control refusal adds bounded evidence (cancel_cause["refusal"]); any other
    # error keeps its class name only.
    session = RunningSession(observe_errors=[None, RuntimeError(SECRET)])
    receipt = drive(session)
    assert receipt["cancel_cause"]["class"] in CANCEL_CAUSE_CLASSES
    assert receipt["cancel_cause"]["class"] == "error"
    assert receipt["cancel_cause"]["exception"] == "RuntimeError"
    assert receipt["cancel_cause"]["refusal"] is None
    assert SECRET not in json.dumps(receipt)


# --- a failed cancel is visible and never a settled stop --------------------------------------


def test_a_cancel_that_stalls_once_is_retried_and_recorded():
    session = RunningSession(observe_errors=[None, RuntimeError("x")])
    session.cancel_errors = [sqlite3.OperationalError("database is locked")]
    receipt = drive(session)
    assert session.cancel_calls == 2
    assert receipt["cancel_recorded"] is True and receipt["cancel_failure"] is None


def test_a_failed_cancel_is_not_reported_as_a_settled_stop():
    session = RunningSession(observe_errors=[None, RuntimeError("x")])
    locked = CancelUnrecorded(guardian_cancel_sent=True)
    locked.__cause__ = sqlite3.OperationalError("database is locked")
    session.cancel_errors = [locked] * CANCEL_ATTEMPTS
    receipt = drive(session)
    assert session.cancel_calls == CANCEL_ATTEMPTS
    assert receipt["cancel_recorded"] is False
    assert receipt["cancel_failure"] == {
        "exception": "OperationalError",
        "attempts": CANCEL_ATTEMPTS,
        "guardian_cancel_sent": True,
    }
    assert receipt["reason"] == CANCEL_UNRECORDED_REASON
    assert receipt["status"] == "inconclusive"
    assert receipt["stop_confirmed"] is False


def test_a_refused_cancel_is_not_retried_and_never_passes():
    # Even with the output retained and the guardian's stop confirmed, an unrecorded cancel
    # fails closed rather than reporting a settled stop.
    session = RunningSession(retain_after=1)
    session.cancel_errors = [ValueError("refused")]
    receipt = drive(session)
    assert session.cancel_calls == 1
    assert receipt["cancel_recorded"] is False
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == CANCEL_UNRECORDED_REASON


# --- the concrete session: the guardian is still reached when control stalls -----------------


class _Store:
    def __init__(self, cancel_error=None, renew_error=None):
        self.cancel_error, self.renew_error = cancel_error, renew_error
        self.calls = []

    def worker_cancel(self, *args, **kwargs):
        self.calls.append("worker_cancel")
        if self.cancel_error:
            raise self.cancel_error

    def renew_execution_lease(self, *args):
        self.calls.append("renew")
        if self.renew_error:
            raise self.renew_error


class _Safety:
    def __init__(self, calls, error=None):
        self.calls, self.error = calls, error

    def cancel(self, state):
        self.calls.append("guardian_cancel")
        if self.error:
            raise self.error


class _Server:
    def __init__(self, store, safety_error=None, heartbeat_error=None):
        self.store, self.worker = store, object()
        self.safety = _Safety(store.calls, safety_error)
        self.heartbeat_error = heartbeat_error

    def heartbeat(self, route):
        self.store.calls.append("guardian_heartbeat")
        if self.heartbeat_error:
            raise self.heartbeat_error


@pytest.fixture(autouse=True)
def _unbounded_wall(monkeypatch):
    """The SIGALRM wall bound needs a free real-time timer; the test runner may hold it."""
    from contextlib import nullcontext

    monkeypatch.setattr("tessera_control.salad_live_check._wall_bound", lambda s: nullcontext())


def concrete(**kwargs):
    session = object.__new__(SaladLiveSession)
    session.server = _Server(
        _Store(kwargs.get("cancel_error"), kwargs.get("renew_error")),
        kwargs.get("safety_error"),
        kwargs.get("heartbeat_error"),
    )
    state = {
        "desired": "run",
        "revision": 3,
        "proposal_digest": "p" * 64,
        "proposal": {"lease_id": "lease", "epoch": 1},
    }
    session.state = lambda route: state
    session._registry_heartbeat = lambda: None
    return session


def test_concrete_cancel_still_sends_the_guardian_cancel_when_control_is_locked():
    session = concrete(cancel_error=sqlite3.OperationalError("database is locked"))
    with pytest.raises(CancelUnrecorded) as raised:
        session.cancel("route")
    assert raised.value.guardian_cancel_sent is True
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert session.server.store.calls == ["worker_cancel", "guardian_cancel"]


def test_concrete_cancel_reports_a_guardian_cancel_that_did_not_go_out():
    session = concrete(
        cancel_error=sqlite3.OperationalError("database is locked"),
        safety_error=RuntimeError(SECRET),
    )
    with pytest.raises(CancelUnrecorded) as raised:
        session.cancel("route")
    assert raised.value.guardian_cancel_sent is False


def test_concrete_keepalive_beats_the_guardian_through_a_control_stall():
    session = concrete(renew_error=sqlite3.OperationalError("database is locked"))
    with pytest.raises(ControlStall) as raised:
        session.keepalive("route")
    assert raised.value.beat_delivered is True
    assert session.server.store.calls == ["renew", "guardian_heartbeat"]


def test_concrete_keepalive_refusal_is_never_hidden_by_a_stall():
    session = concrete(
        renew_error=sqlite3.OperationalError("database is locked"),
        heartbeat_error=GuardianRefused("stop_only"),
    )
    with pytest.raises(GuardianRefused):
        session.keepalive("route")


def test_concrete_keepalive_other_renewal_errors_stay_terminal():
    from tessera_control.store import Conflict

    session = concrete(renew_error=Conflict("lease expired"))
    with pytest.raises(Conflict):
        session.keepalive("route")
    assert "guardian_heartbeat" not in session.server.store.calls


# --- the executor: stderr is classified, an unrecorded cancel is held and reconciled ---------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (None, None),
        (b"", None),
        (b"Traceback...\n  File x\nsqlite3.OperationalError: database is locked\n",
         "OperationalError"),
        (b"TimeoutError\n", "TimeoutError"),
        (("warning: " + SECRET + "\n").encode(), LIVE_CHECK_STDERR_UNCLASSIFIED),
    ],
)  # fmt: skip
def test_live_check_stderr_is_reduced_to_a_class_name(stderr, expected):
    assert live_check_stderr_class(stderr) == expected


def _executor(tmp_path, stop):
    from test_campaign_salad_executor import Harness, item
    from test_termination_evidence_reconcile import GuardianControl

    h = Harness(tmp_path, GuardianControl(stop))
    (tmp_path / "work" / "route_1").mkdir(parents=True, mode=0o700)
    return h, item("executing")


UNRECORDED = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": CANCEL_UNRECORDED_REASON,
    "stop_confirmed": False,
    "provider_stop_confirmed": False,
    "cancel_recorded": False,
    "cancel_failure": {"exception": "OperationalError", "attempts": 3},
}


def test_an_unrecorded_cancel_is_held_until_the_guardian_confirms_the_stop(tmp_path):
    from test_termination_evidence_reconcile import GROUP

    h, work = _executor(tmp_path, {"status": "armed", "reason": "armed", "provider_id": GROUP})
    h.executor._finish(work, dict(UNRECORDED))
    notes = [(a["action"], a["detail"]) for a in h.executor.log]
    assert ("cancel_unrecorded", "OperationalError") in notes
    assert ("cancel_unrecorded_held", "") in notes
    assert "record_outcome" not in h.names()
    # The guardian confirms the exact group stopped: reported, then the outcome is recorded.
    h.control.stop = {"status": "stopped", "reason": "exact_terminal_and_sealed",
                      "provider_id": GROUP}  # fmt: skip
    h.executor._group_stop_next.clear()
    h.executor._finish(work, dict(UNRECORDED))
    assert h.names().count("report_group_stopped") == 1
    assert "record_outcome" in h.names()
    marker = json.loads((tmp_path / "work" / "route_1" / GROUP_STOP_MARKER).read_text())
    assert marker["reported"] is True


def test_an_unrecorded_cancel_hold_is_bounded(tmp_path):
    from test_termination_evidence_reconcile import GROUP

    from tessera_control.campaign_salad_executor import CANCEL_UNRECORDED_HOLD_SECONDS

    h, work = _executor(tmp_path, {"status": "armed", "reason": "armed", "provider_id": GROUP})
    now = {"t": 0.0}
    h.executor.clock = lambda: now["t"]
    h.executor._finish(work, dict(UNRECORDED))
    assert "record_outcome" not in h.names()
    now["t"] = CANCEL_UNRECORDED_HOLD_SECONDS
    h.executor._finish(work, dict(UNRECORDED))
    assert "record_outcome" in h.names()
    assert "report_group_stopped" not in h.names()


def test_a_provider_confirmed_stop_is_not_held(tmp_path):
    h, work = _executor(tmp_path, None)
    h.executor._finish(work, dict(UNRECORDED, provider_stop_confirmed=True))
    assert "record_outcome" in h.names()
