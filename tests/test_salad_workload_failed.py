# ruff: noqa: F811
"""The workload's closed failure report: the guardian records ``workload_failed:<reason>``,
stops at once, and the verdict is ``failed`` with that reason, never a provider move."""

import json
from contextlib import contextmanager

import pytest
from test_salad_live_check import LocalSession, drive
from test_salad_safety_service import service_case  # noqa: F401

from tessera_control import campaign_salad
from tessera_control.salad_live_check import _stop_cause
from tessera_control.salad_workload_contract import WORKLOAD_FAILURE_REASONS

FAILED = "/v1/workload/failed"


def report(service, request, token, reason="entry", exit_code=3, **extra):
    body = dict(binding=request, reason=reason, exit_code=exit_code, **extra)
    return service.workload(FAILED, body, token)


def test_a_failure_report_records_the_cause_and_stops_the_group_at_once(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    reply = report(s, request, token)
    assert reply == dict(
        schema="tessera.workload-failed.v1",
        operation=request["operation"],
        epoch=request["epoch"],
        recorded=True,
    )
    status = s.guardian.status(request["operation"])
    assert status["stop_cause"] == "workload_failed:entry" and status["workload_exit_code"] == 3
    clock[0] += 5
    s.loop()
    assert "stop" in provider.calls
    shown = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert shown["stop_cause"] == "workload_failed:entry"
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    assert s.admin(dict(operation="status", reservation=b["reservation"]))["status"] == "stopped"
    assert s.guardian.status(request["operation"])["allocation_uncertain"] is False


def test_a_second_container_cannot_begin_again_or_change_the_cause(service_case):
    s, _, request, token, clock, provider, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    report(s, request, token)
    with pytest.raises(ValueError, match="consumed|rejected"):
        s.workload("/v1/workload/begin-once", dict(binding=request), token)
    with pytest.raises(ValueError, match="rejected"):
        report(s, request, token, reason="output", exit_code=1)
    assert s.guardian.status(request["operation"])["stop_cause"] == "workload_failed:entry"
    assert s.guardian.status(request["operation"])["workload_exit_code"] == 3


def test_a_replayed_report_is_acknowledged_after_the_stop_began(service_case):
    s, _, request, token, clock, provider, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    first = report(s, request, token)
    clock[0] += 5
    s.loop()  # the guardian now reads "unknown" while it stops the group
    assert report(s, request, token) == first


def test_a_report_before_begin_once_is_refused_and_records_nothing(service_case):
    s, _, request, token, *_ = service_case
    with pytest.raises(ValueError, match="rejected"):
        report(s, request, token)
    assert s.guardian.status(request["operation"])["stop_cause"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        dict(reason="because"),
        dict(reason="entry; stop"),
        dict(reason=None),
        dict(exit_code=True),
        dict(exit_code="3"),
        dict(exit_code=256),
        dict(exit_code=-256),
        dict(message="Traceback ..."),
    ],
)
def test_the_report_is_closed(service_case, mutate):
    s, _, request, token, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    body = dict(binding=request, reason="entry", exit_code=3)
    body.update(mutate)
    with pytest.raises(ValueError, match="rejected"):
        s.workload(FAILED, body, token)
    assert s.guardian.status(request["operation"])["stop_cause"] is None


def test_a_report_without_an_exit_code_value_is_allowed_as_null(service_case):
    s, _, request, token, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    assert report(s, request, token, reason="output_plan", exit_code=None)["recorded"]
    assert s.guardian.status(request["operation"])["workload_exit_code"] is None


def test_a_report_after_another_stop_decision_does_not_overwrite_the_cause(service_case):
    s, b, request, token, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    with pytest.raises(ValueError, match="rejected"):
        report(s, request, token)
    assert "workload_failed" not in json.dumps(s.guardian.status(request["operation"]))


# -------------------------------------------------------------- verdict projection


@pytest.mark.parametrize("reason", WORKLOAD_FAILURE_REASONS)
def test_the_stop_cause_projection_keeps_only_the_closed_reasons(reason):
    assert _stop_cause(f"workload_failed:{reason}") == f"workload_failed:{reason}"


@pytest.mark.parametrize(
    "value", ["workload_failed:Entry", "workload_failed:", "workload_failed:x y"]
)
def test_an_unknown_workload_failure_reason_is_never_echoed(value):
    assert _stop_cause(value) == "invalid_code"


def test_the_live_receipt_ends_with_the_workloads_own_reason():
    session = LocalSession()
    session.proof.update(
        output_retained=False, stop_confirmed=True, stop_cause="workload_failed:entry"
    )
    receipt = drive(session)
    assert receipt["status"] != "passed"
    assert receipt["reason"] == "workload_failed:entry"
    assert receipt["guardian_stop_cause"] == "workload_failed:entry"


class _Store:
    @contextmanager
    def runner_connection(self):
        yield None


class _Runner:
    store = _Store()

    def _one(self, *args):
        return None


def _driver(monkeypatch, receipt):
    driver = campaign_salad.SaladStageDriver.__new__(campaign_salad.SaladStageDriver)
    driver.runner = _Runner()
    state = dict(
        reallocated=False,
        termination_confirmed=True,
        provider_status="stopped",
        receipts=[dict(provenance="provider")],
    )
    driver.route_state = lambda route_id: state
    driver._evidence = lambda *a: "d" * 64
    monkeypatch.setattr(
        campaign_salad,
        "current_operation_reconciliation",
        lambda connection, operation: dict(observed_state="exited", termination_confirmed=True),
    )
    row = dict(
        route_id="r", operation_id="op", manifest_id=None, live_receipt_json=json.dumps(receipt)
    )
    return driver, row


def test_a_workload_failure_verifies_failed_with_its_reason_not_a_provider_move(monkeypatch):
    receipt = dict(
        status="inconclusive",
        reason="workload_failed:entry",
        stop_confirmed=True,
        guardian_stop_cause="workload_failed:entry",
        custody_work_begun=True,
    )
    driver, row = _driver(monkeypatch, receipt)
    verdict, detail, evidence = driver.verify_outcome(None, {}, row)
    assert verdict == "failed" and "workload_failed:entry" in detail
    assert "allocation changed" not in detail and evidence
