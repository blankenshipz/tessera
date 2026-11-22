"""Human gate decisions, conditional unlock and the program budget (ADR 0045). Fakes only."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from test_campaign_standing import Sched

from tessera_control import program_budget
from tessera_control.campaign_gate_models import UnlockRequirement
from tessera_control.campaign_gates import (
    effective_decision,
    evaluate_unlock,
    parse_unlock_json,
)
from tessera_control.store import Conflict, Forbidden, StoreError, canonical_digest


@pytest.fixture
def sched(tmp_path):
    return Sched(tmp_path)


@pytest.fixture(autouse=True)
def _reset_budget():
    program_budget.reset()
    yield
    program_budget.reset()


def decide(sched, campaign_id, decision="go", actor=None, note="reviewed", revision=None):
    row = sched.env.query("SELECT revision FROM campaigns WHERE id=?", campaign_id)[0]
    body = {
        "expected_revision": revision or row["revision"],
        "decision": decision,
        "note": note,
    }
    sched.counter += 1
    return sched.env.store.record_gate_decision(
        actor or sched.env.human(),
        campaign_id,
        f"gate-{sched.counter}",
        canonical_digest(body),
        body,
    )


def finished_campaign(sched, status="succeeded") -> str:
    """A campaign started by a throwaway standing approval, then finished by fiat."""
    standing = sched.create(max_starts=1, max_total_budget_cents=5)
    sched.tick()
    (start,) = sched.starts(standing["id"])
    sched.finish(start["campaign_id"], status)
    return start["campaign_id"]


def go_requirement(campaign_id: str) -> list[dict]:
    return [{"campaign_id": campaign_id, "accept_human_go": True}]


def refusals(sched, standing_id):
    return sched.refusals(standing_id)


# ---- the human gate record ----


def test_human_records_go_and_it_is_attributed(sched):
    campaign = finished_campaign(sched)
    code, record = decide(sched, campaign)
    assert code == 201 and record["decision"] == "go"
    assert record["decision_class"] == "human_session"
    assert record["scientific_validity"] == "not_assessed"
    (event,) = sched.env.events("campaign.gate_decision_recorded")
    assert event["payload"]["gate_decision_id"] == record["id"]
    listed = sched.env.store.list_gate_decisions(sched.env.human(), campaign)
    assert listed["effective"]["id"] == record["id"]


def test_an_external_reader_sees_the_decision_but_not_who_decided(sched):
    campaign = finished_campaign(sched)
    _, record = decide(sched, campaign)
    reader = sched.env.agent(["campaign:read"], "gate-reader")
    seen = sched.env.store.list_gate_decisions(reader, campaign)
    assert seen["effective"]["id"] == record["id"] and seen["effective"]["note"] == "reviewed"
    for item in [*seen["gate_decisions"], seen["effective"]]:
        assert "decided_by_principal_id" not in item and "decided_credential_id" not in item
    human = sched.env.store.list_gate_decisions(sched.env.human(), campaign)
    assert "decided_by_principal_id" in human["effective"]


def test_agents_workers_and_external_scopes_never_write_a_gate_decision(sched):
    campaign = finished_campaign(sched)
    external = sched.env.agent(["campaign:submit", "campaign:read", "campaign:cancel"], "ext")
    for actor in (sched.env.agent(name="plain"), external, sched.env.worker):
        with pytest.raises(Forbidden):
            decide(sched, campaign, actor=actor)
    unlabelled = {k: v for k, v in sched.env.owner.items() if k != "automation_label"}
    with pytest.raises(Forbidden):
        decide(sched, campaign, actor=unlabelled)
    assert sched.env.query("SELECT count(*) AS n FROM campaign_gate_decisions")[0]["n"] == 0


def test_gate_decision_needs_a_finished_resolved_current_campaign(sched):
    standing = sched.create(max_starts=2, max_total_budget_cents=10)
    sched.tick()
    (start,) = sched.starts(standing["id"])
    running = start["campaign_id"]
    with pytest.raises(Conflict, match="finished"):
        decide(sched, running)
    sched.finish(running, "succeeded")
    sched.env.execute("UPDATE campaigns SET held_cents=3 WHERE id=?", running)
    with pytest.raises(Conflict, match="resolved"):
        decide(sched, running)
    sched.env.execute("UPDATE campaigns SET held_cents=0 WHERE id=?", running)
    with pytest.raises(Conflict, match="revision"):
        decide(sched, running, revision=99)
    sched.finish(running, "cancelled")
    with pytest.raises(Conflict, match="finished"):
        decide(sched, running)


def test_decisions_are_append_only_and_the_latest_is_effective(sched):
    campaign = finished_campaign(sched)
    decide(sched, campaign, "go")
    decide(sched, campaign, "no_go", note="retract")
    with (
        sched.env_db()
        if hasattr(sched, "env_db")
        else sqlite3.connect(sched.env.state / "tessera.sqlite3") as connection
    ):
        connection.row_factory = sqlite3.Row
        assert effective_decision(connection, campaign)["decision"] == "no_go"
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM campaign_gate_decisions")


# ---- standing approvals with an unlock ----


def test_standing_start_waits_for_a_human_go_then_records_it_on_the_receipt_data(sched):
    named = finished_campaign(sched)
    gated = sched.create(unlock=go_requirement(named))
    sched.tick(2)
    assert sched.starts(gated["id"]) == []
    assert refusals(sched, gated["id"]) == ["unlock_not_satisfied:0:no_human_go"]
    _, record = decide(sched, named)
    sched.tick()
    (start,) = sched.starts(gated["id"])
    (evaluation,) = sched.env.query(
        "SELECT * FROM campaign_unlock_evaluations WHERE campaign_id=?", start["campaign_id"]
    )
    assert evaluation["basis_kind"] == "human_gate_go"
    assert evaluation["gate_decision_id"] == record["id"]
    assert evaluation["standing_approval_id"] == gated["id"]
    assert evaluation["named_campaign_id"] == named
    assert evaluation["approval_id"] == start["approval_id"]


def test_a_later_no_go_blocks_further_starts_but_not_running_campaigns(sched):
    named = finished_campaign(sched)
    gated = sched.create(
        unlock=go_requirement(named),
        max_starts=3,
        max_total_budget_cents=15,
        max_concurrent=2,
    )
    decide(sched, named, "go")
    sched.tick()
    assert len(sched.starts(gated["id"])) == 1
    decide(sched, named, "no_go", note="retracted")
    sched.tick(2)
    assert len(sched.starts(gated["id"])) == 1  # the veto stops later starts only
    assert sched.world.status(sched.starts(gated["id"])[0]["campaign_id"]) == "running"
    assert refusals(sched, gated["id"])[-1] == "unlock_not_satisfied:0:human_no_go"


def test_unfinished_or_cancelled_named_campaign_does_not_unlock(sched):
    standing = sched.create(max_starts=1, max_total_budget_cents=5)
    sched.tick()
    (start,) = sched.starts(standing["id"])
    named = start["campaign_id"]  # still running
    gated = sched.create(unlock=go_requirement(named))
    sched.tick()
    assert refusals(sched, gated["id"]) == ["unlock_not_satisfied:0:campaign_not_finished"]
    sched.finish(named, "cancelled")
    sched.tick()
    assert refusals(sched, gated["id"])[-1] == "unlock_not_satisfied:0:campaign_not_finished"
    assert sched.starts(gated["id"]) == []


def test_unlock_naming_a_missing_campaign_is_refused_at_creation(sched):
    with pytest.raises(Exception, match="not found"):
        sched.create(unlock=go_requirement("campaign_missing"))
    with pytest.raises(StoreError):
        sched.create(unlock=[{"campaign_id": "x"}])  # accepts nothing


def test_unlock_is_immutable_and_rechecked_for_retries(sched):
    named = finished_campaign(sched)
    decide(sched, named, "go")
    gated = sched.create(
        unlock=go_requirement(named), retry_max=1, max_starts=3, max_total_budget_cents=15
    )
    stored = sched.standing(gated["id"])["unlock_json"]
    assert parse_unlock_json(stored)[0].campaign_id == named
    sched.tick()
    sched.finish_all(gated["id"], "failed")
    decide(sched, named, "no_go", note="stop")
    sched.tick(3)
    assert len(sched.starts(gated["id"])) == 1  # the retry is not an unconditional path
    assert refusals(sched, gated["id"])[-1] == "unlock_not_satisfied:0:human_no_go"


def test_invalid_stored_requirements_fail_closed(sched):
    named = finished_campaign(sched)
    decide(sched, named, "go")
    gated = sched.create(unlock=go_requirement(named))
    sched.env.execute(
        "UPDATE campaign_standing_approvals SET unlock_json='[{\"nope\":1}]' WHERE id=?",
        gated["id"],
    )
    sched.tick()
    assert sched.starts(gated["id"]) == []
    assert refusals(sched, gated["id"]) == ["unlock_not_satisfied:stored_requirements_invalid"]


# ---- approve_campaign with an unlock ----


def approve_body(campaign, **extra):
    return {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
        **extra,
    }


def approve(sched, campaign, body):
    sched.counter += 1
    return sched.env.store.approve_campaign(
        sched.env.human(), campaign["id"], f"ap-{sched.counter}", canonical_digest(body), body
    )


def test_approve_with_unlock_refuses_then_admits_and_records(sched):
    named = finished_campaign(sched)
    submitted = sched.env.submit(spec=sched.spec())
    body = approve_body(submitted, unlock=go_requirement(named))
    with pytest.raises(Conflict, match="unlock_not_satisfied:0:no_human_go"):
        approve(sched, submitted, body)
    # Nothing was written by the refused approval.
    assert sched.world.status(submitted["id"]) == "submitted"
    assert (
        sched.env.query(
            "SELECT count(*) AS n FROM campaign_approvals WHERE campaign_id=?", submitted["id"]
        )[0]["n"]
        == 0
    )
    _, record = decide(sched, named)
    code, result = approve(sched, submitted, body)
    assert code == 200 and result["status"] == "running"
    (evaluation,) = sched.env.query(
        "SELECT * FROM campaign_unlock_evaluations WHERE campaign_id=?", submitted["id"]
    )
    assert evaluation["gate_decision_id"] == record["id"]
    assert evaluation["approval_id"] == result["approval"]["id"]
    assert evaluation["standing_approval_id"] is None


def test_receipt_section_lists_what_admitted_the_campaign(sched):
    from tessera_control.campaign_gates import unlock_receipt_section

    named = finished_campaign(sched)
    decide(sched, named)
    gated = sched.create(unlock=go_requirement(named))
    sched.tick()
    (start,) = sched.starts(gated["id"])
    with sqlite3.connect(sched.env.state / "tessera.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        section = unlock_receipt_section(connection, start["campaign_id"])
        plain = unlock_receipt_section(connection, named)
    assert [r["basis_kind"] for r in section["requirements"]] == ["human_gate_go"]
    assert plain["requirements"] == []


def test_evaluate_unlock_is_tenant_scoped(sched):
    named = finished_campaign(sched)
    decide(sched, named)
    requirement = [UnlockRequirement(campaign_id=named, accept_human_go=True)]
    with sqlite3.connect(sched.env.state / "tessera.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        org = sched.env.owner["org_id"]
        project = sched.env.project["id"]
        assert evaluate_unlock(connection, org, project, requirement)[0] is None
        for other_org, other_project in (("org_other", project), (org, "project_other")):
            reason, _ = evaluate_unlock(connection, other_org, other_project, requirement)
            assert reason == "unlock_not_satisfied:0:campaign_not_found"


# ---- program budget ----


def write_budget(tmp_path, programs, mode=0o600):
    path = tmp_path / "program-budget.json"
    path.write_text(json.dumps({"schema": "tessera.program-budget.v1", "programs": programs}))
    os.chmod(path, mode)
    return path


def test_budget_file_is_owner_only_and_strict(tmp_path):
    good = write_budget(tmp_path, [{"id": "p1", "cap_cents": 20}])
    assert program_budget.load_program_budget_file(good).programs[0].cap_cents == 20
    for bad in (
        write_budget(tmp_path, [{"id": "p1", "cap_cents": 20}], mode=0o644),
        write_budget(tmp_path, [{"id": "p1", "cap_cents": 20}, {"id": "p1", "cap_cents": 1}]),
        write_budget(tmp_path, [{"id": "p1", "cap_cents": -1}]),
        write_budget(tmp_path, [{"id": "p1", "cap_cents": 1, "extra": True}]),
        tmp_path / "missing.json",
    ):
        with pytest.raises(ValueError, match="program budget"):
            program_budget.load_program_budget_file(bad)


def configure(tmp_path, cap, **extra):
    program_budget.configure(
        program_budget.load_program_budget_file(
            write_budget(tmp_path, [{"id": "main", "cap_cents": cap, **extra}])
        )
    )


def test_program_cap_counts_committed_money_across_standing_starts(sched, tmp_path):
    configure(tmp_path, 12)  # per-start cap is 5 cents
    standing = sched.create(max_starts=5, max_total_budget_cents=25, max_concurrent=4)
    sched.tick(5)
    assert len(sched.starts(standing["id"])) == 2  # 5 + 5 fits, a third 5 would make 15
    assert refusals(sched, standing["id"]) == ["program_budget_exhausted:main"]
    # A finished campaign counts what it actually held and settled, freeing the rest.
    sched.finish_all(standing["id"], "succeeded")
    sched.tick()
    assert len(sched.starts(standing["id"])) == 3


def test_program_cap_applies_to_first_approval_and_never_widens_anything(sched, tmp_path):
    configure(tmp_path, 15)  # the spec cap is 10 cents
    first = sched.env.submit(spec=sched.spec())
    assert approve(sched, first, approve_body(first))[0] == 200
    second = sched.env.submit(spec=sched.spec())
    with pytest.raises(Conflict, match="program_budget_exhausted:main"):
        approve(sched, second, approve_body(second))
    assert sched.world.status(second["id"]) == "submitted"


def test_program_cap_can_be_scoped_to_projects(sched, tmp_path):
    configure(tmp_path, 0, project_ids=["project_elsewhere"])
    standing = sched.create()
    sched.tick()
    assert len(sched.starts(standing["id"])) == 1  # this project is not covered


def test_unusable_program_budget_refuses_every_admission(sched):
    program_budget.mark_broken()
    standing = sched.create()
    sched.tick()
    assert sched.starts(standing["id"]) == []
    assert refusals(sched, standing["id"]) == ["program_budget_unavailable"]
    submitted = sched.env.submit(spec=sched.spec())
    with pytest.raises(Conflict, match="program_budget_unavailable"):
        approve(sched, submitted, approve_body(submitted))


def test_failed_campaign_with_a_human_go_unlocks(sched):
    named = finished_campaign(sched, "failed")
    gated = sched.create(unlock=go_requirement(named))
    sched.tick()
    assert sched.starts(gated["id"]) == []  # no decision yet
    decide(sched, named)
    sched.tick()
    assert len(sched.starts(gated["id"])) == 1


def test_automation_labelled_owner_go_never_unlocks(sched):
    named = finished_campaign(sched)
    labelled = {**sched.env.owner, "automation_label": "ops-script"}
    _, record = decide(sched, named, actor=labelled)
    assert record["decision_class"] == "owner_automation_labelled"
    gated = sched.create(unlock=go_requirement(named))
    sched.tick()
    assert sched.starts(gated["id"]) == []
    assert refusals(sched, gated["id"]) == ["unlock_not_satisfied:0:go_not_human_session"]
    submitted = sched.env.submit(spec=sched.spec())
    with pytest.raises(Conflict, match="go_not_human_session"):
        approve(sched, submitted, approve_body(submitted, unlock=go_requirement(named)))
    # A person's later go does unlock.
    decide(sched, named)
    sched.tick()
    assert len(sched.starts(gated["id"])) == 1


def test_unlock_on_a_campaign_that_is_not_submitted_is_refused_not_dropped(sched):
    named = finished_campaign(sched)
    decide(sched, named)
    submitted = sched.env.submit(spec=sched.spec())
    approved = approve(sched, submitted, approve_body(submitted))[1]
    sched.env.execute(
        "UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", submitted["id"]
    )
    row = sched.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0]
    body = {
        "expected_revision": row["revision"],
        "expected_spec_digest": row["spec_digest"],
        "unlock": go_requirement(named),
    }
    with pytest.raises(Conflict, match="first approval"):
        approve(sched, approved, body)
    assert (
        sched.env.query(
            "SELECT count(*) AS n FROM campaign_unlock_evaluations WHERE campaign_id=?",
            submitted["id"],
        )[0]["n"]
        == 0
    )
