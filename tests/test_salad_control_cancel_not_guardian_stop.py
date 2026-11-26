"""A control cancel the guardian records as ``control_cancel`` is not the guardian's own stop.

Since the guardian records any control cancel (operator, campaign, or this driver's own) as its
first stop cause ``control_cancel``, the driver must not class the cancel it then sends as
``guardian_stop_cause``: an operator cancel of a running route that ends in a verified refusal
stays ``control_refused`` and is labelled a running-route cancel.

Isolated orchestration (fake sessions) only; no provider.
"""

from test_salad_live_check_control_refusal import RETIRED, ProbedSession, tagged
from test_salad_live_check_running_cancel import cancelled_before_output, drive

from tessera_control.salad_live_check import RUNNING_CANCEL_PREFIX
from tessera_control.store import Forbidden


def test_operator_cancel_recorded_as_control_cancel_is_not_classed_as_a_guardian_stop():
    # An operator cancelled the running route: the guardian records control_cancel and control
    # revokes the worker, so the next beat is refused with a verified revocation.
    error = tagged(Forbidden(RETIRED), "WorkerRegistryStore.heartbeat")
    session = ProbedSession(stop_cause="control_cancel", beat_errors=[None, None, error])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["guardian_stop_cause"] == "control_cancel"
    cause = receipt["cancel_cause"]
    assert cause["class"] == "control_refused"
    assert cause["class"] != "guardian_stop_cause"
    assert cause["last_provider_status"] == "running"
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "control_refused"


def test_a_real_guardian_stop_cause_is_still_the_guardians_stop():
    error = tagged(Forbidden(RETIRED), "WorkerRegistryStore.heartbeat")
    session = ProbedSession(stop_cause="heartbeat_silence", beat_errors=[None, None, error])
    receipt = drive(session)
    assert receipt["guardian_stop_cause"] == "heartbeat_silence"
    assert receipt["cancel_cause"]["class"] == "guardian_stop_cause"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)
