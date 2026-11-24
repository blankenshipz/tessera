"""A measurement unlock condition only narrows a go and fails closed (ADR 0051)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_campaign_gate_measurement import _attempt, _receipt, _refusal, _requirement
from test_campaign_gates import approve, approve_body, decide, finished_campaign
from test_campaign_standing import Sched

from tessera_control import campaign_gates, program_budget
from tessera_control.campaign_gate_models import MeasurementCondition


@pytest.fixture
def sched(tmp_path):
    return Sched(tmp_path)


@pytest.fixture(autouse=True)
def _reset():
    program_budget.reset()
    campaign_gates.configure_go_authority(accept_automation_labelled=False)
    yield
    program_budget.reset()
    campaign_gates.configure_go_authority(accept_automation_labelled=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "1.0", [1.0]])
def test_non_finite_or_non_numeric_recorded_value_refuses_even_with_a_go(sched, value):
    named = finished_campaign(sched)
    _receipt(sched, named, [_attempt(value)])
    decide(sched, named)
    assert "measurement_not_numeric" in _refusal(
        sched, named, _requirement(named, op="ge", value=0)
    )


def test_duplicate_named_measurement_is_not_recorded(sched):
    named = finished_campaign(sched)
    attempt = _attempt(1.0)
    attempt["measurements"].append({"name": "verdict", "value": 2.0})
    _receipt(sched, named, [attempt])
    decide(sched, named)
    assert "measurement_not_recorded" in _refusal(sched, named, _requirement(named))


def test_corrupt_receipt_fails_closed(sched):
    named = finished_campaign(sched)
    sched.env.execute(
        "INSERT INTO campaign_receipts VALUES (?, ?, ?, 'x')", named, "{not json", "d" * 64
    )
    decide(sched, named)
    assert "measurement_stage_missing" in _refusal(sched, named, _requirement(named))


def test_a_true_condition_never_stands_in_for_the_go(sched):
    named = finished_campaign(sched)
    _receipt(sched, named, [_attempt(5.0)])
    reason = _refusal(sched, named, _requirement(named))
    assert "no_human_go" in reason and "measurement" not in reason


def test_a_true_condition_does_not_override_a_no_go(sched):
    named = finished_campaign(sched)
    _receipt(sched, named, [_attempt(5.0)])
    decide(sched, named)
    decide(sched, named, decision="no_go", note="veto")
    assert "human_no_go" in _refusal(sched, named, _requirement(named))


def test_a_true_condition_does_not_admit_an_automation_go_without_opt_in(sched):
    named = finished_campaign(sched)
    _receipt(sched, named, [_attempt(5.0)])
    decide(sched, named, actor={**sched.env.owner, "automation_label": "ops-script"})
    assert "go_not_human_session" in _refusal(sched, named, _requirement(named))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_condition_value_is_rejected_at_the_model(bad):
    with pytest.raises(ValidationError):
        MeasurementCondition.model_validate({"stage": "s", "name": "n", "op": "ge", "value": bad})
    with pytest.raises(ValidationError):
        MeasurementCondition.model_validate({"stage": "s", "name": "n", "op": "ne", "value": 1})


def test_a_holding_condition_changes_nothing_but_the_receipt_measurement_check(sched):
    named = finished_campaign(sched)
    _receipt(sched, named, [_attempt(1.0)])
    decide(sched, named)
    submitted = sched.env.submit(spec=sched.spec())
    code, result = approve(sched, submitted, approve_body(submitted, unlock=_requirement(named)))
    assert code == 200 and result["status"] == "running"
    assert json.dumps(result)  # serialisable, no widened state
