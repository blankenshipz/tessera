"""Salad executor work from blocked or paused campaigns, late imports and refused work.

Control lists a blocked or paused campaign's ``route_started`` attempts for keepalive only
(v0.104.0): the executor renews them and never launches. Fakes only; no subprocess, no network."""

import json

import pytest
from test_campaign_salad_executor import RECEIPT, Harness, item
from test_salad_executor_concurrent_routes import (
    ConcurrentControl,
    GatedRunner,
    actions,
    executor_for,
)

import tessera_control.campaign_salad_executor as module
from tessera_control.store import Conflict


def write_receipt(tmp_path, route="route_1"):
    receipts = tmp_path / "work" / route / "receipts"
    receipts.mkdir(parents=True, mode=0o700)
    (tmp_path / "work" / route / "outputs").mkdir(mode=0o700)
    target = receipts / module.RECEIPT_NAME
    target.write_text(json.dumps(RECEIPT))
    target.chmod(0o600)


def notes(log, action):
    return [a for a in log if a["action"] == action]


# -- (a) a blocked or paused campaign's started route is kept alive, never launched ------------


@pytest.mark.parametrize("status", ["blocked", "paused_delegation_lapsed"])
def test_a_started_route_of_a_held_campaign_is_renewed_and_not_launched(tmp_path, status):
    h = Harness(tmp_path)
    h.control.items = [item("route_started", campaign_status=status)]
    log = h.executor.run_once()
    assert h.names() == ["heartbeat", "renew"]
    assert h.runs == []
    assert [(a["action"], a["detail"]) for a in log] == [("launch_held", status)]
    # It stays held on every pass, renewed each time.
    h.executor.run_once()
    assert h.names().count("renew") == 2 and "mark_executing" not in h.names() and h.runs == []


@pytest.mark.parametrize("status", ["running", "stopping", "cancel_requested", None])
def test_a_started_route_of_a_launchable_campaign_still_launches(tmp_path, status):
    h = Harness(tmp_path)
    extra = {} if status is None else {"campaign_status": status}
    h.control.items = [item("route_started", **extra)]
    log = h.executor.run_once()
    assert len(h.runs) == 1
    assert h.names().count("mark_executing") == 1 and h.names().count("record_outcome") == 1
    assert notes(log, "launch_held") == []


@pytest.mark.parametrize("status", ["blocked", "paused_delegation_lapsed"])
def test_concurrent_mode_holds_a_blocked_route_and_launches_a_running_one(tmp_path, status):
    control = ConcurrentControl(limit=2)
    control.items = [
        item("route_started", attempt_id="att_1", route_id="route_1", campaign_status=status),
        item("route_started", attempt_id="att_2", route_id="route_2", campaign_status="running"),
    ]
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    log = executor.run_once()
    runner.wait_started("route_2")
    assert ("launch_held", status) in actions(log, "att_1")
    assert ("renew", "lease_1", 3) in control.calls
    assert ("mark_executing", "att_1") not in control.calls
    assert ("mark_executing", "att_2") in control.calls
    assert "route_1" not in runner.threads and set(executor._in_flight) == {"att_2"}
    runner.finish(executor, "route_2")
    executor.run_once()


# -- (b) an executing attempt is finished from its receipt whatever the campaign status --------


def test_an_executing_attempt_of_a_blocked_campaign_with_a_receipt_is_finished(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("executing", campaign_status="blocked")]
    write_receipt(tmp_path)
    log = h.executor.run_once()
    assert h.runs == [] and h.names().count("record_outcome") == 1
    assert notes(log, "launch_held") == []


# -- (c) a late-importable unknown attempt ------------------------------------------------------


def test_an_unknown_attempt_with_a_receipt_is_recorded_once(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("unknown", campaign_status="blocked")]
    write_receipt(tmp_path)
    log = h.executor.run_once()
    assert h.runs == [] and h.names().count("record_outcome") == 1
    assert len(notes(log, "late_import")) == 1
    assert "mark_executing" not in h.names()


def test_an_unknown_attempt_without_a_receipt_is_noted_once_and_left_alone(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("unknown", campaign_status="blocked")]
    first = h.executor.run_once()
    second = h.executor.run_once()
    assert len(notes(first + second, "late_import_no_receipt")) == 1
    assert notes(second, "late_import_no_receipt") == []
    assert h.runs == []
    for name in ("renew", "mark_executing", "record_outcome", "upload"):
        assert name not in h.names()


# -- (d) work control stops listing after refusing the executor's call --------------------------


def test_work_withdrawn_after_a_refused_call_is_journaled_once(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("executing")]
    write_receipt(tmp_path)

    def refuse(attempt_id, payload):
        h.control._c("record_outcome", attempt_id, payload)
        raise Conflict("campaign is blocked")

    h.control.record_outcome = refuse
    first = h.executor.run_once()
    assert ("error", "Conflict") in [(a["action"], a["detail"]) for a in first]
    assert notes(first, "work_withdrawn_after_refusal") == []

    h.control.items = []
    second = h.executor.run_once()
    withdrawn = notes(second, "work_withdrawn_after_refusal")
    assert [(a["attempt_id"], a["detail"]) for a in withdrawn] == [("att_1", "execute")]

    third = h.executor.run_once()
    assert notes(third, "work_withdrawn_after_refusal") == []


def test_a_listed_attempt_after_a_refusal_is_not_reported_withdrawn(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("executing")]
    write_receipt(tmp_path)
    calls = []

    def refuse(attempt_id, payload):
        calls.append(attempt_id)
        raise Conflict("no")

    h.control.record_outcome = refuse
    h.executor.run_once()
    again = h.executor.run_once()
    assert len(calls) == 2 and notes(again, "work_withdrawn_after_refusal") == []
