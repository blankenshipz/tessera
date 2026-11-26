"""Running latch: once a start effect exists, no failed, blocked or unknown read and no internal
classifier error lifts the running-route hold (C2, H10, M3).

Only an affirmative non-running provider observation, a reallocation, a guardian stop cause,
the deadline, the budget or a verified revocation ends it. Isolated orchestration only (fake
sessions, a fake transport and server): never a provider.
"""

import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_salad_http import Factory, run
from test_salad_live_check import LocalSession
from test_salad_live_check_running_cancel import (
    DeployingSession,
    RunningSession,
    cancelled_before_output,
    drive,
    read_timeout,
)

from tessera_control import salad_live_check
from tessera_control.salad_http import SaladHTTPTransport
from tessera_control.salad_proxy_contract import ProxyRequest
from tessera_control.salad_running_latch import RunningLatch, observation
from tessera_control.salad_safety_service import ServiceUnavailable
from tessera_control.salad_server import GuardianRefused
from tessera_control.store import Forbidden
from tessera_worker.salad_remote import SaladTrustedAdapter

EFFECT = "e" * 32


def failed_readback(state, *, status="unknown", reason="provider_unavailable"):
    """The durable route state an observe returns after a failed provider read was recorded:
    the latest receipt (for the latest effect) carries a non-``observed`` proxy reason."""
    effects = list(state.get("effects", [])) or [{"action": "observe"}]
    effects[-1] = dict(effects[-1], id=EFFECT)
    return dict(
        state,
        effects=effects,
        provider_status=status,
        receipts=[{"effect_id": EFFECT, "state": status, "proxy_reason": reason}],
    )


def observed_readback(state, status):
    effects = list(state.get("effects", [])) or [{"action": "observe"}]
    effects[-1] = dict(effects[-1], id=EFFECT)
    return dict(
        state,
        effects=effects,
        provider_status=status,
        receipts=[{"effect_id": EFFECT, "state": status, "proxy_reason": "observed"}],
    )


class ScriptedSession(RunningSession):
    """After start each observe returns ``script[n]`` (a callable of the running state) while
    the script lasts, then the running state."""

    def __init__(self, script, **kwargs):
        super().__init__(**kwargs)
        self.script = list(script)
        self.observed_at = []

    def dispatch(self, route, action):
        state = super().dispatch(route, action)
        if action == "observe" and "start" in self.actions and not self.cancelled:
            self.observed_at.append(self.now)
            if self.script:
                return self.script.pop(0)(state)
        return state


# --- C2: a failed observe readback never lifts the hold ----------------------------------------


@pytest.mark.parametrize(
    "reason", ["provider_unavailable", "admission_denied", "credential_unavailable", None]
)
def test_failed_observe_readback_does_not_lift_running_hold(reason):
    # The group reads running, then one observe returns a recorded failed read ("unknown",
    # or a blocked claim). A later unexpected error and a long control stall are still held.
    script = [
        lambda s: s,  # running
        lambda s: failed_readback(s, reason=reason),
        lambda s: failed_readback(s, reason=reason),
    ]
    beats = [None, None, None] + [sqlite3.OperationalError("database is locked")] * 6
    session = ScriptedSession(script, retain_after=12, beat_errors=beats)
    original = session.evidence
    calls = {"n": 0}

    def evidence(route):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ValueError("unexpected")
        return original(route)

    session.evidence = evidence
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["last_provider_status"] == "running"
    assert receipt["running_latch"] == {"state": "seen_running", "cleared_by": None}
    assert receipt["running_errors_held"] >= 1
    assert receipt["cancel_cause"]["class"] == "execution_complete"


def test_a_failed_read_carrying_a_stopped_observation_does_not_clear_the_latch():
    # A failed read may still carry the group's last observation (e.g. identity mismatch); its
    # "stopped" is not the provider's affirmative word, so the latch and status stay.
    script = [
        lambda s: s,
        lambda s: failed_readback(s, status="stopped", reason="admission_denied"),
    ]
    session = ScriptedSession(script, retain_after=3, observe_errors=[None, None, ValueError("x")])
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["last_provider_status"] == "running"
    assert receipt["running_errors_held"] == 1


def test_a_failed_read_on_a_latched_route_keeps_the_normal_observe_cadence():
    # Before the latch a failed "unknown" read stretched the next observe to 30 s; now only an
    # affirmative pre-start observation does.
    script = [lambda s: s] + [lambda s: failed_readback(s)] * 3
    session = ScriptedSession(script, retain_after=5)
    drive(session)
    gaps = [b - a for a, b in zip(session.observed_at, session.observed_at[1:], strict=False)]
    assert gaps[:4] == [10, 10, 10, 10], gaps


def test_an_affirmative_unknown_observation_still_slows_the_cadence():
    # An exact read that reports the instance still allocating ("unknown", proxy_reason
    # observed) is an affirmative pre-start observation: the slow cadence applies.
    script = [lambda s: observed_readback(s, "unknown")] * 2
    session = ScriptedSession(script, retain_after=4)
    drive(session)
    gaps = [b - a for a, b in zip(session.observed_at, session.observed_at[1:], strict=False)]
    assert gaps[:2] == [30, 30], gaps


def test_an_affirmative_stopped_observation_clears_the_latch():
    script = [lambda s: s, lambda s: observed_readback(s, "stopped")]
    session = ScriptedSession(script, observe_errors=[None, None, ValueError("boom")])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["running_latch"] == {"state": "cleared", "cleared_by": "provider_stopped"}
    assert receipt["last_provider_status"] == "stopped"
    assert receipt["cancel_cause"]["class"] == "error"


def test_a_reallocation_clears_the_latch():
    script = [lambda s: s, lambda s: dict(failed_readback(s, reason=None), reallocated=True)]
    session = ScriptedSession(script, observe_errors=[None, None, ValueError("boom")])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["running_latch"] == {"state": "cleared", "cleared_by": "reallocated"}
    assert receipt["last_provider_status"] == "unknown"
    assert receipt["cancel_cause"]["class"] == "error"


def test_a_running_cancel_after_a_failed_read_is_labelled_a_running_route_cancel():
    # The route read running, then a failed read; a genuine cancel cause that is not the
    # guardian's, the deadline or the budget is still visible as a running-route cancel.
    script = [lambda s: s, lambda s: failed_readback(s)]
    retired = Forbidden("Worker is retired.")
    session = ScriptedSession(script, observe_errors=[None, None, retired])
    session.control_readiness = lambda: "ready"
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "control_refused"
    assert receipt["reason"] == salad_live_check.RUNNING_CANCEL_PREFIX + "control_refused"


# --- C2: a wall-bound TimeoutError inside perform is not recorded as unknown ------------------


class _Store:
    def __init__(self, claim_error=None, action="observe"):
        self.claim_error, self.action = claim_error, action

    def get(self, worker, route_id):
        return {"effects": [{"action": self.action, "id": EFFECT}], "provider_id": "p" * 32}

    def claim_proxy(self, *args, **kwargs):
        if self.claim_error is not None:
            raise self.claim_error
        return {"provider_id": "p" * 32, "allocation_id": None, "desired": "run"}


class _Transport:
    def __init__(self, error):
        self.error = error

    def send(self, operation, selection, provider_id, secret):
        raise self.error


def fake_server(*, send_error=None, claim_error=None, action="observe"):
    from tessera_control.salad_server import TrustedSaladServer

    server = object.__new__(TrustedSaladServer)
    server.worker = {}
    server.store = _Store(claim_error, action)
    server.policy = SimpleNamespace(
        management=SimpleNamespace(reference="ref", version=1),
        target=SimpleNamespace(selection=None),
    )
    server.resolver = SimpleNamespace(resolve=lambda ref, version: nullcontext(b"secret"))
    server._credential = lambda: None
    server.transport = _Transport(send_error)
    server.safety = SimpleNamespace(ready=lambda state: None, cancel=lambda state: None)
    server._revoke_inputs = lambda state: None
    return server


def proxy_request(operation):
    return ProxyRequest(route_id="exact-route", effect_id=EFFECT, operation=operation)


@pytest.mark.parametrize("operation", ["read_group", "read_instance"])
def test_wall_bound_timeout_inside_perform_is_not_recorded_as_unknown(operation):
    expired = TimeoutError("bounded live operation expired")
    server = fake_server(send_error=expired)
    with pytest.raises(TimeoutError):
        server.request(proxy_request(operation), preflight=None)
    # The trusted adapter's perform (the observe's provider call) raises it too, so no
    # "unknown" readback is built from the timeout.
    adapter = SaladTrustedAdapter(server, "exact-route")
    state = {"id": "exact-route", "provider_id": "p" * 32, "effects": [{"id": EFFECT}]}
    with pytest.raises(TimeoutError):
        adapter.perform("observe", None, state)


def test_a_wall_bound_timeout_while_claiming_a_read_is_not_recorded_as_blocked():
    server = fake_server(claim_error=TimeoutError("bounded live operation expired"))
    with pytest.raises(TimeoutError):
        server.request(proxy_request("read_group"), preflight=None)


def test_other_failures_and_non_read_operations_keep_their_closed_results():
    server = fake_server(send_error=RuntimeError("boom"))
    result = server.request(proxy_request("read_group"), preflight=None)
    assert (result.status, result.reason) == ("unknown", "provider_unavailable")
    server = fake_server(send_error=TimeoutError("bounded live operation expired"), action="stop")
    result = server.request(proxy_request("stop"), preflight=None)
    assert result.status == "unknown"
    server = fake_server(claim_error=RuntimeError("locked"))
    result = server.request(proxy_request("read_group"), preflight=None)
    assert (result.status, result.reason) == ("blocked", "admission_denied")


def test_transport_send_reraises_a_timeout_outside_its_request_on_an_exact_read(monkeypatch):
    def expired(self, *args, **kwargs):
        raise TimeoutError("bounded live operation expired")

    monkeypatch.setattr(SaladHTTPTransport, "_request", expired)
    with pytest.raises(TimeoutError):
        run(Factory(), "read_group")
    # A create keeps its closed "not sent" result; a stop keeps its unknown result.
    assert run(Factory(), "create_stopped", None).reason == "request_not_sent"
    assert run(Factory(), "stop").status == "unknown"


def test_a_provider_read_timeout_is_still_a_closed_failed_read():
    # The 2 s provider read deadline (a socket timeout inside the request) stays a failed read.
    result = run(Factory(TimeoutError("The read operation timed out")), "read_group")
    assert (result.status, result.reason, result.detail) == (
        "unknown",
        "provider_unavailable",
        "timeout",
    )


# --- H10: the provisioning window after start is protected -------------------------------------


class PullingSession(RunningSession):
    """After start the group reads ``pending`` (image pull) for ``pull`` observes, then
    running."""

    def __init__(self, pull, **kwargs):
        super().__init__(**kwargs)
        self.pull = pull

    def dispatch(self, route, action):
        state = super().dispatch(route, action)
        if action == "observe" and state.get("provider_status") == "running" and self.pull:
            self.pull -= 1
            return dict(state, provider_status="pending")
        return state


def test_control_stall_during_image_pull_does_not_cancel_started_route():
    locked = [None] + [sqlite3.OperationalError("database is locked") for _ in range(8)]
    session = PullingSession(4, retain_after=6, beat_errors=locked)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["control_stalls"] == 8
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["running_latch"] == {"state": "seen_running", "cleared_by": None}


@pytest.mark.parametrize(
    "errors",
    [
        # three guardian status read timeouts, an unverified refusal, an unexpected error
        dict(observe_errors=[ServiceUnavailable("timeout")] * 4),
        dict(observe_errors=[read_timeout() for _ in range(4)]),
        dict(observe_errors=[Forbidden("Unauthorized")] * 2),
        dict(observe_errors=[ValueError("unexpected")] * 2),
        dict(beat_errors=[None] + [GuardianRefused("internal")] * 2),
        dict(beat_errors=[None] + [GuardianRefused("service_deadline")] * 2),
    ],
)
def test_transients_during_image_pull_do_not_cancel_started_route(errors):
    session = PullingSession(3, retain_after=5, **errors)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"


def test_the_guardian_still_governs_a_started_route():
    # The guardian's own allocation deadline still ends a started route that never ran.
    session = DeployingSession(beat_errors=[None] + [GuardianRefused("internal")] * 3)
    session.stop_cause = "allocation_timeout"
    receipt = drive(session)
    assert receipt["reason"] == "allocation_timeout"
    assert receipt["cancel_cause"]["class"] == "guardian_stop_cause"


def test_a_terminal_guardian_refusal_still_cancels_a_started_route():
    session = DeployingSession(beat_errors=[None, GuardianRefused("budget_insufficient")])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "budget"


def test_the_deadline_still_ends_a_started_route():
    session = DeployingSession(observe_errors=[read_timeout() for _ in range(80)])
    receipt = drive(session, max_seconds=60)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "deadline"


def test_a_route_with_no_start_effect_keeps_the_earlier_bound():
    # A reconnect to a created but never started group: no latch, three failed reads end it.
    class Unstarted(LocalSession):
        def __init__(self):
            super().__init__()
            self.record["effects"] = [{"action": "create"}]
            self.proof["output_retained"] = False
            self.now = 0.0

        def dispatch(self, route, action):
            if action == "observe":
                self.actions.append(action)
                raise read_timeout()
            return super().dispatch(route, action)

    receipt = drive(Unstarted())
    assert "running_latch" not in receipt
    assert receipt["observe_transient_failures"] == 2
    assert receipt["cancel_cause"]["class"] == "observe_timeout"


def test_a_reconnect_to_a_started_route_is_latched():
    session = LocalSession(prior=True)
    session.now = 0.0
    session.proof["output_retained"] = False
    session.record["provider_status"] = "pending"
    calls = {"n": 0}
    original = session.dispatch

    def dispatch(route, action):
        if action == "observe":
            calls["n"] += 1
            if calls["n"] <= 4:
                session.actions.append(action)
                raise read_timeout()
            session.proof["output_retained"] = True
        return original(route, action)

    session.dispatch = dispatch
    receipt = drive(session, max_polls=10)
    assert receipt["status"] == "passed", receipt
    assert receipt["observe_transient_failures"] == 4


# --- M3: hold()'s own classifier errors fail toward hold ---------------------------------------


class Sentinel(Exception):
    pass


@pytest.mark.parametrize("session_class", [RunningSession, DeployingSession])
def test_hold_classifier_exception_does_not_cancel_running_or_started_route(
    monkeypatch, session_class
):
    original = salad_live_check._guardian_code

    def broken(error):
        if isinstance(error, Sentinel):
            raise RuntimeError("classifier bug")
        return original(error)

    monkeypatch.setattr(salad_live_check, "_guardian_code", broken)
    session = session_class(observe_errors=[None, Sentinel("x"), Sentinel("y")])
    session.retain_after = 3
    if session_class is DeployingSession:
        session.proof["output_retained"] = False

        def evidence(route, proof=session.proof):
            if session.running_observes > 3:
                proof["output_retained"] = True
            return proof

        session.evidence = evidence
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["running_classifier_errors_held"] == 2
    held = receipt["last_held_error"]
    assert held["exception"] == "Sentinel"
    assert held["classifier_error"] == "RuntimeError"
    assert held["step"] == "observe"


def test_hold_classifier_clock_failure_holds_a_latched_route():
    session = RunningSession(retain_after=3, observe_errors=[None, ValueError("x")])
    ticks = {"n": 0, "broken": False}
    original_dispatch = session.dispatch

    def dispatch(route, action):
        try:
            return original_dispatch(route, action)
        except ValueError:
            ticks["broken"] = True  # the next clock() read (inside hold) raises once
            raise

    def clock():
        if ticks["broken"]:
            ticks["broken"] = False
            raise OSError("clock")
        return session.now

    session.dispatch = dispatch
    receipt = salad_live_check._drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=400,
        max_polls=40,
        sleep=lambda s: setattr(session, "now", session.now + s),
        clock=clock,
    )
    assert receipt["status"] == "passed", receipt
    assert receipt["running_classifier_errors_held"] == 1


# --- the latch itself ---------------------------------------------------------------------------


def test_observation_requires_this_observes_own_observed_readback():
    assert observation(observed_readback({}, "stopped")) == ("stopped", True)
    assert observation(failed_readback({}, status="stopped", reason="identity_mismatch")) == (
        "stopped",
        False,
    )
    # A durable receipt with no closed token (proxy_reason None) is a failed read.
    assert observation(failed_readback({}, reason=None))[1] is False
    # The latest receipt belongs to an older effect: not this observe's readback.
    stale = observed_readback({}, "stopped")
    stale["effects"] = [{"id": "f" * 32}]
    assert observation(stale)[1] is False
    # Isolated sessions with no receipts are taken at their word; malformed states never raise.
    assert observation({"provider_status": "stopped"}) == ("stopped", True)
    assert observation(["running"]) == (None, False)


def test_latch_engages_on_start_or_running_and_never_clears_on_a_failed_read():
    latch = RunningLatch()
    assert latch.receipt() is None and not latch.engaged()
    assert latch.note_observe(failed_readback({}, status="stopped")) is True  # unlatched
    latch.note_start()
    assert latch.receipt() == {"state": "started", "cleared_by": None}
    assert latch.note_observe(failed_readback({})) is False
    assert latch.note_observe(observed_readback({}, "running")) is True
    assert latch.receipt()["state"] == "seen_running"
    for status in ("unknown", "pending", "deploying"):
        latch.note_observe(observed_readback({}, status))
        assert latch.engaged()
    latch.note_observe(observed_readback({}, "deleted"))
    assert latch.receipt() == {"state": "cleared", "cleared_by": "provider_deleted"}
    # Once cleared it stays cleared (a new start never happens on the same route).
    latch.note_observe(observed_readback({}, "running"))
    assert not latch.engaged()
