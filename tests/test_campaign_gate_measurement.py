"""Measurement predicates on a prior receipt; operator opt-in for automation go (ADR 0051)."""

from __future__ import annotations

import json

import pytest
from test_campaign_gates import (
    approve,
    approve_body,
    decide,
    finished_campaign,
    go_requirement,
    refusals,
)
from test_campaign_standing import Sched

from tessera_control import campaign_gates, program_budget
from tessera_control.campaign_gate_models import UnlockRequirement
from tessera_control.campaign_gates import requirement_digest
from tessera_control.store import Conflict, canonical_digest


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


def _receipt(sched, campaign_id, attempts, stage="s1"):
    receipt = {"stages": [{"stage_key": stage, "attempts": attempts}]}
    sched.env.execute("DELETE FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    sched.env.execute(
        "INSERT INTO campaign_receipts VALUES (?, ?, ?, 'x')",
        campaign_id,
        json.dumps(receipt),
        canonical_digest(receipt),  # the reader verifies it
    )


def _attempt(value, name="verdict", status="succeeded"):
    return {"attempt": 1, "status": status, "measurements": [{"name": name, "value": value}]}


def _requirement(campaign_id, **condition):
    body = {"stage": "s1", "name": "verdict", "op": "ge", "value": 1.0, **condition}
    return [{"campaign_id": campaign_id, "accept_human_go": True, "measurement": body}]


def _refusal(sched, named, requirement):
    submitted = sched.env.submit(spec=sched.spec())
    with pytest.raises(Conflict) as error:
        approve(sched, submitted, approve_body(submitted, unlock=requirement))
    return str(error.value)


def test_condition_narrows_a_human_go_and_both_must_hold(sched):
    named = finished_campaign(sched)
    _receipt(sched, named, [_attempt(1.0)])
    requirement = _requirement(named)
    # The condition holds but there is no go: the basis still refuses.
    assert "no_human_go" in _refusal(sched, named, requirement)
    decide(sched, named)
    submitted = sched.env.submit(spec=sched.spec())
    code, result = approve(sched, submitted, approve_body(submitted, unlock=requirement))
    assert code == 200 and result["status"] == "running"


@pytest.mark.parametrize(
    ("attempts", "condition", "code"),
    [
        ([_attempt(0.0)], {}, "measurement_condition_false"),
        ([_attempt(1.0), _attempt(1.0)], {}, "measurement_attempt_not_unique"),
        ([_attempt(1.0, status="failed")], {}, "measurement_attempt_not_unique"),
        ([_attempt(1.0, name="other")], {}, "measurement_not_recorded"),
        ([_attempt(None)], {}, "measurement_not_numeric"),
        ([_attempt(1.0)], {"stage": "absent"}, "measurement_stage_missing"),
    ],
)
def test_every_unevaluable_condition_refuses_even_with_a_go(sched, attempts, condition, code):
    named = finished_campaign(sched)
    _receipt(sched, named, attempts)
    decide(sched, named)
    assert code in _refusal(sched, named, _requirement(named, **condition))


def test_a_missing_receipt_refuses_and_a_go_cannot_bypass_the_condition(sched):
    named = finished_campaign(sched)
    decide(sched, named)
    assert "measurement_receipt_missing" in _refusal(sched, named, _requirement(named))


def test_requirement_digest_is_unchanged_without_a_measurement():
    plain = UnlockRequirement.model_validate({"campaign_id": "c1", "accept_human_go": True})
    assert "measurement" not in plain.model_dump(mode="json")
    with_condition = UnlockRequirement.model_validate(
        {
            "campaign_id": "c1",
            "accept_human_go": True,
            "measurement": {"stage": "s", "name": "n", "op": "eq", "value": 1},
        }
    )
    assert requirement_digest(plain) != requirement_digest(with_condition)


def test_automation_go_counts_only_where_the_operator_opted_in(sched):
    named = finished_campaign(sched)
    labelled = {**sched.env.owner, "automation_label": "ops-script"}
    decide(sched, named, actor=labelled)
    gated = sched.create(unlock=go_requirement(named))
    sched.tick()
    assert refusals(sched, gated["id"]) == ["unlock_not_satisfied:0:go_not_human_session"]
    campaign_gates.configure_go_authority(accept_automation_labelled=True)
    sched.tick()
    assert len(sched.starts(gated["id"])) == 1


def test_the_unverified_desktop_owner_label_never_counts(sched):
    named = finished_campaign(sched)
    campaign_gates.configure_go_authority(accept_automation_labelled=True)
    desktop = {**sched.env.owner, "automation_label": "desktop-owner"}
    _, record = decide(sched, named, actor=desktop)
    assert record["decision_class"] == "owner_automation_unverified"
    assert "go_not_human_session" in _refusal(sched, named, go_requirement(named))


def test_a_no_go_still_vetoes_when_automation_go_is_accepted(sched):
    named = finished_campaign(sched)
    campaign_gates.configure_go_authority(accept_automation_labelled=True)
    labelled = {**sched.env.owner, "automation_label": "ops-script"}
    decide(sched, named, actor=labelled)
    decide(sched, named, decision="no_go", actor=labelled, note="veto")
    assert "human_no_go" in _refusal(sched, named, go_requirement(named))


def test_a_fresh_serve_defaults_to_a_persons_go_only():
    from tessera_control.cli import parser

    command = parser().parse_args(["serve", "--state-dir", "state", "--port", "1"])
    assert command.accept_automation_gate_go is False
    flagged = parser().parse_args(
        ["serve", "--state-dir", "state", "--port", "1", "--accept-automation-gate-go"]
    )
    assert flagged.accept_automation_gate_go is True
