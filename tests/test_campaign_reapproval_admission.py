"""A forward re-approval of a lapsed campaign re-checks what it was admitted under (ADR 0045).

The program cap is re-checked without counting the campaign twice, and the first
approval's unlock requirements are re-evaluated; a refusal writes nothing. Fakes only.
"""

from __future__ import annotations

import sqlite3

import pytest
from test_campaign_gates import (
    approve,
    approve_body,
    configure,
    decide,
    finished_campaign,
    go_requirement,
)
from test_campaign_standing import Sched

from tessera_control import program_budget
from tessera_control.campaign_gates import unlock_receipt_section
from tessera_control.store import Conflict


@pytest.fixture
def sched(tmp_path):
    return Sched(tmp_path)


@pytest.fixture(autouse=True)
def _reset_budget():
    program_budget.reset()
    yield
    program_budget.reset()


def lapse(sched, campaign_id, status="paused_delegation_lapsed") -> dict:
    sched.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, campaign_id)
    return sched.env.query("SELECT * FROM campaigns WHERE id=?", campaign_id)[0]


def reapprove(sched, row, **extra):
    return approve(sched, row, approve_body(row, **extra))


def snapshot(sched, campaign_id) -> tuple:
    env = sched.env
    (row,) = env.query("SELECT status, revision FROM campaigns WHERE id=?", campaign_id)
    return (
        row["status"],
        row["revision"],
        env.query("SELECT count(*) AS n FROM campaign_approvals WHERE campaign_id=?", campaign_id)[
            0
        ]["n"],
        env.query(
            "SELECT count(*) AS n FROM campaign_unlock_evaluations WHERE campaign_id=?",
            campaign_id,
        )[0]["n"],
        env.query("SELECT count(*) AS n FROM events")[0]["n"],
    )


def evaluations(sched, campaign_id) -> list:
    return sched.env.query(
        "SELECT * FROM campaign_unlock_evaluations WHERE campaign_id=? ORDER BY rowid",
        campaign_id,
    )


def gated_running(sched):
    """A campaign first admitted under a human-go unlock, now lapsed."""
    named = finished_campaign(sched)
    decide(sched, named)
    submitted = sched.env.submit(spec=sched.spec())
    code, _ = approve(sched, submitted, approve_body(submitted, unlock=go_requirement(named)))
    assert code == 200
    return named, lapse(sched, submitted["id"])


# ---- program budget ----


def test_reapproval_refused_when_the_program_cap_no_longer_admits_it(sched, tmp_path):
    configure(tmp_path, 10)  # the spec cap is 10 cents
    submitted = sched.env.submit(spec=sched.spec())
    assert approve(sched, submitted, approve_body(submitted))[0] == 200
    row = lapse(sched, submitted["id"])
    configure(tmp_path, 5)  # the operator narrowed the program after admission
    before = snapshot(sched, row["id"])
    with pytest.raises(Conflict, match="Admission refused: program_budget_exhausted:main"):
        reapprove(sched, row)
    assert snapshot(sched, row["id"]) == before


def test_reapproval_refused_when_the_program_budget_is_unusable(sched, tmp_path):
    submitted = sched.env.submit(spec=sched.spec())
    approve(sched, submitted, approve_body(submitted))
    row = lapse(sched, submitted["id"])
    program_budget.mark_broken()
    with pytest.raises(Conflict, match="program_budget_unavailable"):
        reapprove(sched, row)
    assert sched.world.status(row["id"]) == "paused_delegation_lapsed"


def test_reapproval_does_not_count_the_campaign_twice(sched, tmp_path):
    configure(tmp_path, 10)  # exactly this campaign's cap: counting it twice would refuse
    submitted = sched.env.submit(spec=sched.spec())
    approve(sched, submitted, approve_body(submitted))
    row = lapse(sched, submitted["id"])
    code, result = reapprove(sched, row)
    assert code == 200 and result["status"] == "running"
    # Another campaign still does not fit: the re-approval added no commitment of its own.
    other = sched.env.submit(spec=sched.spec())
    with pytest.raises(Conflict, match="program_budget_exhausted:main"):
        approve(sched, other, approve_body(other))


def test_cleanup_reapproval_skips_admission_checks(sched, tmp_path):
    named, row = gated_running(sched)
    decide(sched, named, decision="no_go")
    program_budget.mark_broken()
    row = lapse(sched, row["id"], status="cancel_requested")
    code, result = reapprove(sched, row)
    assert code == 200 and result["status"] == "cancel_requested"
    assert len(evaluations(sched, row["id"])) == 1  # only the first admission's


# ---- unlock ----


def test_reapproval_refused_when_the_gate_turned_no_go(sched):
    named, row = gated_running(sched)
    decide(sched, named, decision="no_go")
    before = snapshot(sched, row["id"])
    with pytest.raises(Conflict, match="Admission refused: unlock_not_satisfied:0:human_no_go"):
        reapprove(sched, row, unlock=go_requirement(named))
    assert snapshot(sched, row["id"]) == before


def test_reapproval_with_the_gate_still_go_is_admitted_and_recorded(sched):
    named, row = gated_running(sched)
    code, result = reapprove(sched, row, unlock=go_requirement(named))
    assert code == 200 and result["status"] == "running"
    first, second = evaluations(sched, row["id"])
    assert second["approval_id"] == result["approval"]["id"] != first["approval_id"]
    assert second["requirement_digest"] == first["requirement_digest"]
    assert second["standing_approval_id"] is None
    with sqlite3.connect(sched.env.state / "tessera.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        section = unlock_receipt_section(connection, row["id"])
    # Every admission's evaluation is on the receipt, oldest first.
    assert [r["evaluated_at"] for r in section["requirements"]] == [
        first["evaluated_at"],
        second["evaluated_at"],
    ]


def test_reapproval_must_restate_the_first_unlock_exactly(sched):
    named, row = gated_running(sched)
    other = finished_campaign(sched)
    decide(sched, other)
    before = snapshot(sched, row["id"])
    for extra in ({}, {"unlock": go_requirement(other)}):
        with pytest.raises(Conflict, match="restate the first approval's unlock"):
            reapprove(sched, row, **extra)
    stricter = go_requirement(named) + go_requirement(other)
    with pytest.raises(Conflict, match="restate the first approval's unlock"):
        reapprove(sched, row, unlock=stricter)
    assert snapshot(sched, row["id"]) == before


def test_unlock_is_refused_on_reapproval_of_an_unconditional_campaign(sched):
    named = finished_campaign(sched)
    decide(sched, named)
    submitted = sched.env.submit(spec=sched.spec())
    approve(sched, submitted, approve_body(submitted))
    row = lapse(sched, submitted["id"])
    with pytest.raises(Conflict, match="first approval"):
        reapprove(sched, row, unlock=go_requirement(named))
    assert evaluations(sched, row["id"]) == []
    # Without one it is admitted as before.
    assert reapprove(sched, row)[0] == 200


def test_standing_started_campaign_reuses_its_standing_unlock(sched):
    named = finished_campaign(sched)
    decide(sched, named)
    gated = sched.create(unlock=go_requirement(named))
    sched.tick()
    (start,) = sched.starts(gated["id"])
    row = lapse(sched, start["campaign_id"])
    decide(sched, named, decision="no_go")
    with pytest.raises(Conflict, match="unlock_not_satisfied:0:human_no_go"):
        reapprove(sched, row)
    decide(sched, named)
    code, result = reapprove(sched, row)
    assert code == 200 and result["status"] == "running"
    first, second = evaluations(sched, row["id"])
    assert second["approval_id"] == result["approval"]["id"]
    assert second["requirement_digest"] == first["requirement_digest"]
