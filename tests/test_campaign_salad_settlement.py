"""Automatic Salad settlement at the guardian-bounded hold (ADR 0038 amendment).

Fakes only: the executor's outcome is recorded through the control store with a guardian
final status as the executor would project it. No provider, network or credential.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

CLEAN = dict(
    status="stopped",
    reason="deadline",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
    observed_at="2026-11-19T00:00:00Z",
)


def two_stages(env):
    return [salad_stage(env, "g1"), salad_stage(env, "g2")]


def run(world, final=CLEAN, *, passed=True):
    world.to_route_started()
    world.run_to_outcome(passed=passed, guardian_final=final)


def campaign(world):
    return world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)


def entries(world) -> int:
    return world.row("SELECT count(*) AS n FROM experiment_budget_entries")["n"]


def test_ok_verdict_with_a_clean_final_settles_at_the_hold_without_a_ledger_entry(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    attempt = world.attempt()
    assert (
        attempt["settled_cents"] == HOLD and attempt["settlement_basis"] == "guardian_bounded_hold"
    )
    assert (campaign(world)["held_cents"], campaign(world)["settled_cents"]) == (0, HOLD)
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == HOLD
    assert entries(world) == 0
    reservation = world.row("SELECT * FROM experiment_budget_reservations")
    assert reservation["held_cents"] == HOLD
    assert world.phase() == "settled"


def test_the_next_positive_hold_stage_reserves_after_a_bounded_settlement(tmp_path):
    world = SaladWorld(tmp_path, stages=two_stages, spec_over={"budget_cap_cents": 60})
    run(world)
    world.tick_until(lambda: world.phase() == "settled")
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_salad_stages WHERE stage_key='g2' AND source_id IS NOT NULL"
            )
            is not None
        )
    )
    world.assess_source("g2")
    world.tick_until(lambda: world.attempt("g2") is not None)
    assert world.events("campaign.stage_waiting_exposure") == []


def test_a_failed_verdict_with_a_clean_final_is_bounded_too(tmp_path):
    world = SaladWorld(tmp_path)
    run(world, passed=False)
    world.tick_until(lambda: world.attempt()["settlement_basis"] == "guardian_bounded_hold")
    assert world.attempt()["salad_verdict"] == "failed"
    assert world.attempt()["settled_cents"] == HOLD


@pytest.mark.parametrize(
    "change",
    [
        {"orphan": True},
        {"allocation_uncertain": True},
        {"escalation_pending": True},
        {"budget_fenced": True},
        {"rate_unknown": True},
        {"status": "reconciled"},
        {"published_hourly_rate_usd": None},
        {"actual_billed_cost_cents": 5},
        {"estimated_exposure_cents": None},
        "none",
    ],
)
def test_anything_short_of_a_clean_final_waits_for_a_human_charge(tmp_path, change):
    world = SaladWorld(tmp_path)
    final = None if change == "none" else dict(CLEAN, **change)
    run(world, final)
    world.tick_until_phase("awaiting_charge")
    world.tick(3)
    assert world.phase() == "awaiting_charge" and world.attempt()["settled_cents"] is None
    assert campaign(world)["held_cents"] == HOLD


def test_a_malformed_stored_estimate_is_never_trusted(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.env.execute(
        "UPDATE campaign_salad_attempts SET guardian_final_json=? WHERE attempt_id=?",
        json.dumps(dict(CLEAN, estimated_exposure_cents="NaN")),
        world.salad()["attempt_id"],
    )
    world.tick_until_phase("awaiting_charge")
    world.tick(2)
    assert world.attempt()["settled_cents"] is None


def test_an_estimate_above_the_hold_marks_the_attempt_unknown_and_blocks(tmp_path):
    world = SaladWorld(tmp_path)
    run(world, dict(CLEAN, estimated_exposure_cents=str(HOLD) + ".5"))
    world.tick_until(lambda: world.status() == "blocked")
    assert world.phase() == "unknown" and world.attempt()["status"] == "unknown"
    assert world.attempt()["settled_cents"] is None
    assert campaign(world)["stop_reason"] == "estimated_overrun"
    world.tick(3)
    assert len(world.events("campaign.blocked")) == 1


def test_an_unknown_verdict_is_never_settled(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.run_to_outcome(guardian_final=CLEAN, outputs=False)
    world.env.execute(
        "UPDATE campaign_salad_attempts SET live_receipt_json=NULL WHERE attempt_id=?",
        world.salad()["attempt_id"],
    )
    world.tick_until(lambda: world.phase() == "unknown")
    world.tick(3)
    assert world.attempt()["settled_cents"] is None


def test_a_later_charge_at_or_below_the_hold_confirms_once_and_keeps_the_amounts(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    world.record_charge(13)
    world.tick_until(lambda: world.events("campaign.settlement_confirmed") != [])
    confirmed = world.events("campaign.settlement_confirmed")
    assert [(e["provisional_cents"], e["charged_cents"]) for e in confirmed] == [(HOLD, 13)]
    # A partial charge confirms the hold but the settled amount is still the hold.
    assert world.attempt()["settlement_basis"] == "guardian_bounded_hold"
    assert world.salad()["settlement_confirmed_at"] is not None
    assert world.attempt()["settled_cents"] == HOLD and campaign(world)["settled_cents"] == HOLD
    world.tick(3)
    assert len(world.events("campaign.settlement_confirmed")) == 1
    assert world.events("campaign.late_charge") == []
    # Charges that total the settled amount make it a human charge.
    world.record_charge(HOLD - 13)
    world.tick_until(lambda: world.attempt()["settlement_basis"] == "human_charge")
    assert world.attempt()["settled_cents"] == HOLD
    assert len(world.events("campaign.settlement_confirmed")) == 1


def test_a_charge_above_the_hold_adds_the_excess_once(tmp_path):
    world = SaladWorld(tmp_path, spec_over={"budget_cap_cents": 200})
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    world.record_charge(HOLD + 7)
    world.tick_until(lambda: world.events("campaign.late_charge") != [])
    assert world.events("campaign.late_charge")[0]["delta_cents"] == 7
    world.tick(3)
    assert len(world.events("campaign.late_charge")) == 1
    assert campaign(world)["settled_cents"] == HOLD + 7


def test_a_charge_above_the_hold_over_the_cap_blocks_a_running_campaign(tmp_path):
    world = SaladWorld(tmp_path, stages=two_stages, spec_over={"budget_cap_cents": 2 * HOLD})
    run(world)
    world.tick_until(lambda: world.phase() == "settled")
    world.record_charge(HOLD + 30)
    world.tick_until(lambda: world.status() == "blocked")
    assert campaign(world)["stop_reason"] == "budget_overrun"
    assert [e["reason"] for e in world.events("campaign.blocked")] == ["budget_overrun"]


def test_a_charge_after_the_campaign_is_terminal_emits_without_touching_the_receipt(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    before = world.row("SELECT receipt_digest FROM campaign_receipts")["receipt_digest"]
    world.record_charge(13)
    world.tick_until(lambda: world.events("campaign.settlement_confirmed") != [])
    assert world.row("SELECT receipt_digest FROM campaign_receipts")["receipt_digest"] == before


def test_a_human_charge_before_settlement_has_the_human_charge_basis(tmp_path):
    world = SaladWorld(tmp_path)
    run(world, final=None)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(lambda: world.status() == "succeeded")
    assert world.attempt()["settlement_basis"] == "human_charge"
    assert world.attempt()["settled_cents"] == 5


def test_a_human_charge_settlement_without_a_recorded_covered_amount_is_not_re_added(tmp_path):
    world = SaladWorld(tmp_path, spec_over={"budget_cap_cents": HOLD})
    run(world, final=None)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(lambda: world.status() == "succeeded")
    world.env.execute(
        "UPDATE campaign_salad_attempts SET charged_cents=NULL WHERE attempt_id=?",
        world.salad()["attempt_id"],
    )
    world.runner.tick()
    world.runner.tick()
    world.driver.sweep_unconfirmed()
    assert world.attempt()["settled_cents"] == 5 and campaign(world)["settled_cents"] == 5
    assert world.events("campaign.late_charge") == []
    assert world.events("campaign.late_overrun") == []


def test_a_cancel_before_any_claim_is_unclaimed_zero(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settlement_basis"] == "unclaimed_zero"


def test_the_receipt_carries_basis_finality_and_the_labelled_estimate(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    attempt = receipt["stages"][0]["attempts"][0]
    assert attempt["settlement_basis"] == "guardian_bounded_hold"
    assert attempt["settlement_final"] is False
    assert attempt["guardian_estimate"] == {
        "estimated_exposure_cents": "3.5",
        "published_hourly_rate_usd": "0.5",
        "label": "estimate_not_invoice",
    }


def test_a_restart_between_the_grant_reconcile_and_the_settle_settles_once(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    run(world)
    from tessera_control.campaign_runner import CampaignRunner

    real = CampaignRunner._settle
    state = {"armed": True}

    def crashing(self, connection, attempt_id, settlement, basis):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("synthetic crash")
        return real(self, connection, attempt_id, settlement, basis)

    monkeypatch.setattr(CampaignRunner, "_settle", crashing)
    world.tick_until(lambda: not state["armed"])
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", world.attempt()["grant_id"])
    assert grant["status"] == "reconciled" and world.attempt()["settled_cents"] is None
    world.tick_until(lambda: world.status() == "succeeded")
    assert world.attempt()["settled_cents"] == HOLD
    assert (campaign(world)["held_cents"], campaign(world)["settled_cents"]) == (0, HOLD)


def settled_charged(world):
    return (
        world.attempt()["settled_cents"],
        world.salad()["charged_cents"],
        world.attempt()["settlement_basis"],
    )


def test_an_invoice_split_into_charges_below_the_hold_never_adds_a_late_charge(tmp_path):
    world = SaladWorld(tmp_path, stages=two_stages, spec_over={"budget_cap_cents": 2 * HOLD})
    run(world)
    world.tick_until(lambda: world.phase() == "settled")
    world.record_charge(3)
    world.tick_until(lambda: world.events("campaign.settlement_confirmed") != [])
    assert settled_charged(world) == (HOLD, HOLD, "guardian_bounded_hold")
    world.record_charge(5)
    world.tick(4)
    # 3 + 5 = 8 <= 20: the settlement already covers it; nothing is added or blocked.
    assert settled_charged(world) == (HOLD, HOLD, "guardian_bounded_hold")
    assert world.events("campaign.late_charge") == [] and world.events("campaign.blocked") == []
    assert world.status() == "running" or world.status() == "succeeded"
    assert campaign(world)["settled_cents"] >= HOLD
    # A third charge that takes the total past the hold adds only the excess.
    world.record_charge(15)
    world.tick_until(lambda: world.events("campaign.late_charge") != [])
    assert world.events("campaign.late_charge")[0]["delta_cents"] == 3
    assert world.attempt()["settled_cents"] == HOLD + 3
    assert world.attempt()["settlement_basis"] == "human_charge"


def test_a_late_overrun_on_a_terminal_campaign_is_an_event_not_a_block(tmp_path):
    world = SaladWorld(tmp_path, spec_over={"budget_cap_cents": HOLD})
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    world.record_charge(HOLD + 7)
    world.tick_until(lambda: world.events("campaign.late_charge") != [])
    assert world.status() == "succeeded" and world.events("campaign.blocked") == []
    assert len(world.events("campaign.late_overrun")) == 1
    assert world.events("campaign.late_charge")[0]["campaign_terminal"] is True
    # The campaign and attempt totals follow the charge; the stored receipt does not.
    assert campaign(world)["settled_cents"] == HOLD + 7
    world.tick(3)
    assert len(world.events("campaign.late_overrun")) == 1


def test_the_sweep_picks_attempts_with_a_charge_not_the_lowest_campaign_ids(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    real = world.campaign_id
    real_attempt = world.attempt()["id"]
    db = sqlite3.connect(world.env.state / "tessera.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=OFF")

    def copy(table, where, **over):
        value = real_attempt if table == "campaign_salad_attempts" else real
        row = dict(db.execute(f"SELECT * FROM {table} WHERE {where}", (value,)).fetchone())
        row.update(over)
        names = ",".join(row)
        db.execute(
            f"INSERT INTO {table} ({names}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )

    # Decoy terminal campaigns whose ids sort first, each with an unconfirmed bounded
    # attempt that has no recorded charge: they must not occupy the sweep's slots.
    for n in range(3):
        decoy, attempt = f"campaign_00000{n}", f"attempt_00000{n}"
        copy("campaigns", "id=?", id=decoy)
        copy("campaign_stages", "campaign_id=?", campaign_id=decoy, experiment_id=f"x{n}")
        copy(
            "campaign_stage_attempts",
            "campaign_id=?",
            id=attempt,
            campaign_id=decoy,
            admitted_attempt_id=f"y{n}",
            grant_id=None,
        )
        copy(
            "campaign_salad_attempts",
            "attempt_id=?",
            attempt_id=attempt,
            route_id=None,
            operation_id=None,
        )
    db.commit()
    db.close()
    world.record_charge(13)
    from tessera_control.campaign_providers import get_provider

    driver = get_provider("salad").driver(world.runner)
    driver.sweep_unconfirmed(limit=3)
    assert len(world.events("campaign.settlement_confirmed")) == 1


def test_the_unknown_transition_and_the_block_are_one_transaction(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    run(world, dict(CLEAN, estimated_exposure_cents=str(HOLD) + ".5"))
    store = world.runner.store
    real_record = store.record_runner_event

    def failing(connection, campaign, event_type, payload):
        if event_type == "campaign.blocked":
            raise RuntimeError("crash between the writes")
        return real_record(connection, campaign, event_type, payload)

    monkeypatch.setattr(store, "record_runner_event", failing)
    world.tick(4)
    # Nothing was half-applied: the attempt is not unknown and the campaign still runs.
    assert world.phase() != "unknown" and world.status() == "running"
    monkeypatch.setattr(store, "record_runner_event", real_record)
    world.tick_until(lambda: world.status() == "blocked")
    assert world.phase() == "unknown"
    world.tick(4)
    assert world.phase() == "unknown" and world.attempt()["status"] == "unknown"


def test_a_foreign_reconcile_cannot_hide_an_estimated_overrun(tmp_path):
    world = SaladWorld(tmp_path)
    run(world, dict(CLEAN, estimated_exposure_cents=str(HOLD) + ".5"))
    world.tick_until(lambda: world.attempt()["salad_verdict"] is not None)
    assert world.status() == "running" and world.attempt()["status"] != "unknown"
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled', settlement_cents=0 WHERE id=?",
        world.attempt()["grant_id"],
    )
    world.cancel()
    world.tick(6)
    assert world.phase() == "unknown" and world.attempt()["settled_cents"] is None


def test_an_unclaimed_zero_settlement_is_never_labelled_human_reconcile(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled', settlement_cents=0 WHERE id=?",
        world.attempt()["grant_id"],
    )
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settled_cents"] == 0
    assert world.attempt()["settlement_basis"] == "unclaimed_zero"
