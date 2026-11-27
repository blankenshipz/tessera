"""A failed run's outcome waits (bounded) for the guardian's container-log tail of a recorded
non-zero exit, so a tail the guardian records a loop pass after ``stopped`` reaches the receipt.
Isolated fakes only: no live Salad, no network, no database."""

import json
from types import SimpleNamespace

from test_campaign_salad_executor import RECEIPT, Harness
from test_salad_completion_tail_retries import FinishControl, guardian_status, run_until_recorded

from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.salad_container_log import summarize_container_log
from tessera_control.salad_exit_watch import STDERR_UNAVAILABLE

FAILED = dict(RECEIPT, status="failed", reason="workload_failed")
TAIL = summarize_container_log([{"text_log": "ValueError: expected one input, found 0"}])


def _events(codes, names=("instance_started", "instance_exited")):
    return dict(outcome="read", events=list(names), exit_codes=list(codes), machines=1)


def _final(start_events=None, container_log=None, status="stopped"):
    value = guardian_status(status)
    if start_events is not None:
        value["start_events"] = start_events
    if container_log is not None:
        value["container_log"] = container_log
    return value


def _recorded(control):
    return next(c for c in control.calls if c[0] == "record_outcome")[2]


def _receipt(final, monkeypatch):
    """The campaign receipt control builds from the stored guardian final."""
    monkeypatch.setattr(SaladRouteProvider, "_late_bound_inputs", staticmethod(lambda c, a: None))
    monkeypatch.setattr(SaladRouteProvider, "_termination", staticmethod(lambda c, a, r: None))
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    row = {"phase": "done", "route_id": None, "guardian_final_json": json.dumps(final)}
    connection = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: row))
    attempt = {"id": 1, "salad_verdict": "failed", "salad_evidence_digest": "d" * 64}
    return provider.receipt_fields(connection, attempt)


def test_a_tail_recorded_after_stopped_reaches_the_final_and_the_receipt(tmp_path, monkeypatch):
    control = FinishControl(
        [_final(_events([1])), _final(_events([1]), TAIL)],
    )
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(FAILED))
    names = [a["action"] for a in actions]
    assert ("finish_deferred", "guardian_final") in [(a["action"], a["detail"]) for a in actions]
    assert "finish_retries_exhausted" not in names
    assert h.names().count("record_outcome") == 1
    final = _recorded(control)["guardian_final"]
    assert final["container_log"] == TAIL
    fields = _receipt(final, monkeypatch)
    assert fields["guardian_container_exit"] == {"exit_codes": [1], "stderr_tail": TAIL}
    # The deferred pass repeated no import or upload: those run once, on the recording pass.
    assert len(h.imports) <= 1


def test_a_crash_loop_then_kill_also_waits_for_its_tail(tmp_path):
    control = FinishControl([_final(_events([1, 137])), _final(_events([1, 137]), TAIL)])
    h = Harness(tmp_path, control)
    run_until_recorded(h, dict(FAILED))
    assert _recorded(control)["guardian_final"]["container_log"] == TAIL


def test_a_tail_that_never_arrives_is_recorded_after_the_bound_as_not_read(tmp_path, monkeypatch):
    from tessera_control import campaign_salad_executor as module

    monkeypatch.setattr(module, "FINISH_MAX_DEFERRALS", 2)
    control = FinishControl([_final(_events([1]))])
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(FAILED), passes=6)
    names = [a["action"] for a in actions]
    assert names.count("finish_deferred") == 2
    assert ("finish_retries_exhausted", "guardian_final") in [
        (a["action"], a["detail"]) for a in actions
    ]
    assert h.names().count("record_outcome") == 1
    final = _recorded(control)["guardian_final"]
    assert "container_log" not in final
    fields = _receipt(final, monkeypatch)
    assert fields["guardian_container_exit"]["stderr_tail"] == STDERR_UNAVAILABLE


def test_a_closed_unavailable_token_is_recorded_at_once(tmp_path):
    # The guardian's read failed or hit the quota: it recorded a token, nothing more will come.
    token = {"outcome": "unavailable", "reason": "log_quota_exhausted"}
    control = FinishControl([_final(_events([1]), token)])
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(FAILED))
    assert "finish_deferred" not in [a["action"] for a in actions]
    assert _recorded(control)["guardian_final"]["container_log"] == token


def test_a_failed_run_with_no_exit_record_is_recorded_at_once(tmp_path):
    for final in (_final(), _final(_events([137])), _final(_events([0]))):
        control = FinishControl([final])
        h = Harness(tmp_path / str(id(final)), control)
        actions = run_until_recorded(h, dict(FAILED))
        assert "finish_deferred" not in [a["action"] for a in actions]
        assert h.names().count("record_outcome") == 1
        assert h.names().count("guardian_final") == 1


def test_a_passed_run_is_unchanged(tmp_path):
    # A pass never waits on a tail (a non-zero exit record does not make it wait either).
    control = FinishControl([_final(_events([1]))])
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(RECEIPT))
    assert "finish_deferred" not in [a["action"] for a in actions]
    assert h.names().count("record_outcome") == 1
    assert _recorded(control)["manifest_id"] == "manifest_1"


def test_a_failed_run_whose_guardian_is_not_stopped_is_unchanged(tmp_path):
    control = FinishControl([_final(_events([1]), status="running")])
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(FAILED))
    assert "finish_deferred" not in [a["action"] for a in actions]
    assert h.names().count("record_outcome") == 1
