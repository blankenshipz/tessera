"""New plans must declare version-2 bound gates; legacy plans stay readable and untouched.

Synthetic fixtures test bookkeeping, not scientific performance.
"""

import sqlite3

import pytest
from test_gate_evidence_bound_gates import bound_plan, linked_journey, seed
from test_program_evaluations import prepared as prepared_v1
from test_program_evaluations import protocol as protocol_v1
from test_program_interactions import ProgramJourney


def plan_rows(j):
    return [r for r in j.context()["reviews"] if r["scope"]["action"] == "plan"]


def stored(j):
    connection = sqlite3.connect(j.state / "tessera.sqlite3")
    try:
        return connection.execute(
            "SELECT id, scope_json, scope_digest, status FROM decision_requests ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize("proposer", ["owner", "agent"])
def test_strict_prose_and_v1_plans_are_rejected_without_a_row(tmp_path, proposer):
    j = ProgramJourney(tmp_path)
    token = j.agent if proposer == "agent" else None
    before = stored(j)
    prose = j.propose(j.scope(), strict=True, status=409, token=token)
    assert "version-2" in prose["detail"]
    v1 = j.propose(
        j.scope(evaluation_protocol=protocol_v1(j.variant["id"])),
        strict=True,
        status=409,
        token=token,
    )
    assert "version-2" in v1["detail"]
    assert plan_rows(j) == []
    assert stored(j) == before


def test_strict_v2_plan_is_accepted(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    exp_variant = seed(j)
    p, _ = bound_plan(j, exp_variant)
    again = j.propose(j.scope(evaluation_protocol=p), strict=True)
    assert again["status"] == "pending"
    assert any(r["id"] == again["id"] for r in plan_rows(j))


def test_legacy_v1_plan_evaluation_and_go_remain_usable_and_labelled(tmp_path):
    j = ProgramJourney(tmp_path)
    finding, _, r = prepared_v1(j)  # seeds the v1 plan as legacy history
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    j.resolve(evaluation)
    go = j.propose(
        j.scope(
            "go_no_go",
            recommendation="go",
            finding_ids=[finding["id"]],
            evaluation_id=evaluation["id"],
        )
    )
    j.resolve(go)
    rows = {v["id"]: v for v in j.context()["reviews"]}
    assert rows[evaluation["id"]]["evaluation_report"]["status"] == "pass"
    assert rows[evaluation["id"]]["gate_evidence_binding"] == "unbound_caller_reported_legacy"
    assert rows[go["id"]]["gate_evidence_binding"] == "unbound_caller_reported_legacy"
    assert rows[go["id"]]["is_effective_review"]
    # A new plan attempt after the rule is refused; the legacy history is unchanged.
    j.propose(j.scope(), strict=True, status=409)
    assert j.context()["execution_granted"] is False


def test_legacy_prose_plan_go_is_labelled(tmp_path):
    j = ProgramJourney(tmp_path)
    j.assess()
    finding = j.finding()
    j.resolve(j.propose())  # prose-only plan seeded as legacy
    j.resolve(j.propose(j.scope("source_use")))
    go = j.propose(j.scope("go_no_go", recommendation="go", finding_ids=[finding["id"]]))
    j.resolve(go)
    row = next(v for v in j.context()["reviews"] if v["id"] == go["id"])
    assert row["gate_evidence_binding"] == "none_prose_only_legacy"


@pytest.mark.parametrize("protocol", [None, "v1"])
def test_pending_legacy_proposal_can_still_be_resolved_unchanged(tmp_path, protocol):
    j = ProgramJourney(tmp_path)
    scope = (
        j.scope() if protocol is None else j.scope(evaluation_protocol=protocol_v1(j.variant["id"]))
    )
    pending = j.propose(scope)  # legacy history; not approved yet
    before = next(r for r in plan_rows(j) if r["id"] == pending["id"])
    assert before["status"] == "pending"
    # No patch here: the rule is live for resolution, which is not a proposal.
    assert j.resolve(pending)["status"] == "approved"
    after = next(r for r in plan_rows(j) if r["id"] == pending["id"])
    assert after["status"] == "approved"
    assert after["scope"] == before["scope"]
    assert after["scope_digest"] == before["scope_digest"]


def test_rejected_new_plan_does_not_rewrite_legacy_rows(tmp_path):
    j = ProgramJourney(tmp_path)
    legacy = j.propose()
    before = stored(j)
    assert any(row[0] == legacy["id"] for row in before)
    j.propose(j.scope(evaluation_protocol=protocol_v1(j.variant["id"])), strict=True, status=409)
    j.propose(j.scope(), strict=True, status=409, token=j.agent)
    assert stored(j) == before
