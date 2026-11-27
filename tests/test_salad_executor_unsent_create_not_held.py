"""The executor does not hold an outcome for a group the trusted server never created.

The field run looped ``group_stop_held`` / ``group_identity_unrecorded`` for six minutes: its
only create effect was refused at the trusted server's use-time admission, so no provider group
was ever recorded and the guardian could never confirm one stopped. Such a route's outcome is
recorded at once and the runner's own proof decides (ADR 0205 amendment); a create that may
have been sent still holds as before."""

from __future__ import annotations

from test_salad_create_intent_refusal import ITEM, Control, make_executor

UNRESOLVED = {"status": "inconclusive", "reason": "custody_stop_or_reconnect_unresolved"}
DENIED = {
    "effect_id": "eff-create",
    "provenance": "provider",
    "proxy_reason": "admission_denied",
    "state": "unknown",
    "provider_id": None,
    "allocation_id": None,
    "termination_confirmed": False,
}


def _route(receipt=DENIED, provider_id=None):
    return {
        "provider_id": provider_id,
        "desired": "stop",
        "termination_confirmed": False,
        "reallocated": False,
        "effects": [{"id": "eff-create", "action": "create"}],
        "receipts": [dict(receipt)],
    }


def _executor(tmp_path, state):
    executor = make_executor(Control(state), tmp_path)
    executor._finish_deferred = {}
    asked = []
    executor._report_group_stop = lambda item: asked.append(item["attempt_id"])
    return executor, asked


def test_a_create_refused_at_admission_with_no_group_is_not_held(tmp_path):
    state = _route()
    executor, asked = _executor(tmp_path, state)
    assert executor._unconfirmed_stop_held(ITEM, UNRESOLVED, state) is False
    assert asked == [] and executor._finish_deferred == {}


def test_a_create_that_may_have_been_sent_still_holds(tmp_path):
    state = _route({**DENIED, "proxy_reason": "provider_unavailable"})
    executor, asked = _executor(tmp_path, state)
    assert executor._unconfirmed_stop_held(ITEM, UNRESOLVED, state) is True
    assert asked == ["att-1"] and "att-1" in executor._finish_deferred


def test_a_recorded_group_still_holds_for_the_guardians_stop(tmp_path):
    state = _route(provider_id="a" * 32)
    executor, asked = _executor(tmp_path, state)
    assert executor._unconfirmed_stop_held(ITEM, UNRESOLVED, state) is True
    assert asked == ["att-1"]
