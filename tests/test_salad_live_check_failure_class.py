"""Pre-start failures map to a closed class on the receipt; exception text never does."""

import json
from unittest.mock import patch

import pytest
from test_salad_live_check import LocalSession, drive

from tessera_control.salad_live_check import (
    PRESTART_FAILURE_CLASSES,
    PrestartStepError,
    SaladLiveSession,
)

SECRET = "tok_SECRET-https://example.invalid/x?key=abc"


class Boom(Exception):
    pass


def failing(step):
    session = LocalSession()
    session.expects_input = True
    if step == "route_state_unreadable":
        session.state = lambda route: (_ for _ in ()).throw(Boom(SECRET))
    elif step in {"create_failed", "start_failed"}:
        session.fail = step.split("_")[0]
        session.dispatch_error = Boom(SECRET)
        original = session.dispatch

        def dispatch(route, action):
            if action == session.fail:
                raise Boom(SECRET)
            return original(route, action)

        session.dispatch = dispatch
    elif step == "group_unsettled":
        session.settling = 99
    elif step == "ticket_mint_failed":
        session.deliver_error = PrestartStepError("ticket_mint_failed")
    elif step == "input_delivery_unconfirmed":
        session.deliver_error = PrestartStepError("input_delivery_unconfirmed")
    elif step == "wall_bound_exceeded":
        session.deliver_error = TimeoutError(SECRET)
    elif step == "prestart_unclassified":
        session.deliver_error = Boom(SECRET)
    return session


@pytest.mark.parametrize("step", sorted(PRESTART_FAILURE_CLASSES))
def test_each_class_is_reached_and_status_stays_inconclusive(step):
    receipt = drive(failing(step))
    assert receipt["prestart_failure"]["class"] == step
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "custody_stop_or_reconnect_unresolved"
    assert "start" not in failing(step).actions
    assert SECRET not in json.dumps(receipt)


def test_exception_class_name_kept_never_text():
    receipt = drive(failing("create_failed"))
    assert receipt["prestart_failure"] == {"class": "create_failed", "exception": "Boom"}
    assert "SECRET" not in json.dumps(receipt)


def test_wrapped_cause_type_is_recorded():
    session = failing("ticket_mint_failed")
    session.deliver_error = None
    error = PrestartStepError("ticket_mint_failed")
    error.__cause__ = KeyError(SECRET)
    session.deliver_error = error
    receipt = drive(session)
    assert receipt["prestart_failure"] == {"class": "ticket_mint_failed", "exception": "KeyError"}


def test_unknown_class_tag_falls_back_by_name():
    session = failing("prestart_unclassified")
    session.deliver_error = PrestartStepError("made_up_class")
    assert drive(session)["prestart_failure"]["class"] == "prestart_unclassified"


def test_no_failure_leaves_none_and_post_start_failure_is_not_prestart():
    assert drive(LocalSession())["prestart_failure"] is None
    session = LocalSession()
    session.proof = dict(session.proof, output_retained=False)
    assert drive(session)["prestart_failure"] is None


def test_create_rejection_keeps_its_own_reason_without_prestart_class():
    session = LocalSession()
    original = session.dispatch

    def dispatch(route, action):
        if action == "create":
            return {
                "receipts": [
                    {
                        "proxy_reason": "provider_rejected_request",
                        "provider_rejection": {"status": 400},
                    }
                ]
            }
        return original(route, action)

    session.dispatch = dispatch
    receipt = drive(session)
    assert receipt["reason"] == "provider_rejected_request"
    assert receipt["prestart_failure"] is None


def concrete(server_error=None, status=None, exchange_error=None):
    session = object.__new__(SaladLiveSession)
    state = {
        "proposal": {"selection": {"manifests": {"data": "d" * 64}}},
    }

    class Server:
        def deliver_inputs(self, route):
            if server_error:
                raise server_error

    session.server = Server()
    session.state = lambda route: state
    session._body = lambda route, what: {}

    def exchange(body):
        if exchange_error:
            raise exchange_error
        return {"input_delivery": status}

    session._exchange = exchange
    return session


class Declared:
    snapshot_digest = "d" * 64


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"server_error": RuntimeError(SECRET)}, "ticket_mint_failed"),
        ({"exchange_error": RuntimeError(SECRET)}, "input_delivery_unconfirmed"),
        ({"status": {"ticket_held": False}}, "input_delivery_unconfirmed"),
        ({"status": {"ticket_held": True, "snapshot_digest": "x"}}, "input_delivery_unconfirmed"),
    ],
)
def test_concrete_deliver_inputs_classes(kwargs, expected):
    with patch("tessera_control.salad_server.route_input", lambda proposal: Declared()):
        with pytest.raises(PrestartStepError) as raised:
            concrete(**kwargs).deliver_inputs("r")
    assert raised.value.failure_class == expected
    assert SECRET not in str(raised.value)


def test_concrete_deliver_inputs_timeout_stays_timeout():
    with patch("tessera_control.salad_server.route_input", lambda proposal: Declared()):
        with pytest.raises(TimeoutError):
            concrete(server_error=TimeoutError("x")).deliver_inputs("r")


class UnknownAfterStart(LocalSession):
    """Provider keeps reporting status unknown once started and never retains output."""

    def dispatch(self, route, action):
        state = super().dispatch(route, action)
        if action == "observe" and "start" in self.actions:
            return dict(state, provider_status="unknown")
        return state


def test_unknown_status_after_start_reaches_poll_bound_and_returns_a_receipt():
    # The pre-start observe backoff loops; the failure handler must still read the step
    # tracker afterwards and return a receipt rather than raise.
    session = UnknownAfterStart()
    session.proof = dict(session.proof, output_retained=False)
    receipt = drive(session, max_polls=6)
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "custody_stop_or_reconnect_unresolved"
    assert receipt["prestart_failure"] is None
    assert receipt["cancel_recorded"] is True


def test_reconnect_failure_after_state_read_gets_no_prestart_class():
    # prior launches exist, so create/start are skipped; a later failure is post-start.
    session = LocalSession(prior=True)
    session.proof = dict(session.proof, output_retained=False)
    receipt = drive(session, max_polls=2)
    assert receipt["status"] == "inconclusive"
    assert receipt["prestart_failure"] is None
    assert "create" not in session.actions and "start" not in session.actions


def test_unreadable_state_on_reconnect_is_still_a_prestart_class():
    session = LocalSession(prior=True)
    session.state = lambda route: (_ for _ in ()).throw(Boom(SECRET))
    receipt = drive(session)
    assert receipt["prestart_failure"]["class"] == "route_state_unreadable"
    assert SECRET not in json.dumps(receipt)
