"""A Salad attempt whose recorded outcome shows no create was sent is released automatically.

Real stores and runner (``SaladWorld``); the executor is played by the test. "Never sent" is
proven only from control's own records (no sent create/start effect, no claim, no provider
reference); a create that was sent, or a passed receipt, is never released here. Also covers
the receipt rule of the automatic unknown-attempt reconcile (``create_claim_state``)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control.campaign_providers import get_provider

PRE_CREATE_RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": "create_not_attempted",
    "stop_confirmed": False,
    "outputs": [],
}


def _money(world: SaladWorld):
    campaign = world.row(
        "SELECT held_cents, settled_cents FROM campaigns WHERE id=?", world.campaign_id
    )
    limits = world.row("SELECT held_cents FROM execution_project_limits")
    return dict(campaign), dict(limits)


def _record(world: SaladWorld, receipt: dict[str, Any], provider_id: str | None = None) -> None:
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=receipt,
            manifest_id=None,
            provider_id=provider_id,
            estimated_exposure={"estimated_exposure_cents": 0},
        ),
    )


def _executing(world: SaladWorld) -> None:
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))


def _sent_create_effect(world: SaladWorld) -> None:
    state = world.route_state()
    state["effects"] = [*state["effects"], {"id": "eff_create", "action": "create"}]
    state["receipts"] = [
        *(state.get("receipts") or []),
        {
            "effect_id": "eff_create",
            "provenance": "provider",
            "state": "unknown",
            "provider_id": None,
            "allocation_id": None,
            "proxy_reason": "provider_readback",
        },
    ]
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])


def test_prestart_read_failure_with_receipt_and_no_effect_ends_unclaimed_with_hold_released(
    tmp_path,
):
    world = SaladWorld(tmp_path)
    _executing(world)
    (campaign, limits) = _money(world)
    assert campaign["held_cents"] > 0 and limits["held_cents"] > 0
    _record(world, PRE_CREATE_RECEIPT)
    world.tick_until(lambda: world.status() in {"failed", "stopped"})
    attempt = world.attempt()
    assert attempt["status"] == "failed_transient"
    assert attempt["failure_class"] == "t1_salad_unclaimed"
    assert attempt["failure_detail"] == "live_check_create_not_sent"
    assert attempt["settled_cents"] == 0
    assert world.phase() == "unclaimed"
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    event = world.events("execution.grant_reconciled_unclaimed")[-1]
    assert event["proof"] == "no_create_or_start_claim"
    # Released without a person: the operator route never ran, no claim, nothing charged.
    assert not world.events("campaign.attempt_unclaimed_reconciled")
    (after, after_limits) = _money(world)
    assert after == {**campaign, "held_cents": 0}
    assert after_limits == {"held_cents": 0}


def _make_sent(world: SaladWorld) -> None:
    world.claim("create")
    _sent_create_effect(world)


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(_make_sent, id="sent_create_effect_with_claim"),
        pytest.param(lambda world: world.claim("create"), id="claim_without_effect"),
    ],
)
def test_a_create_that_may_have_been_sent_is_not_released(tmp_path, setup):
    world = SaladWorld(tmp_path)
    _executing(world)
    setup(world)
    _record(world, PRE_CREATE_RECEIPT)
    money = _money(world)
    world.tick(6)
    attempt = world.attempt()
    assert attempt["status"] in {"running", "unknown"}
    assert attempt["failure_class"] != "t1_salad_unclaimed"
    assert attempt["settled_cents"] is None
    assert world.phase() != "unclaimed"
    assert _money(world) == money
    assert not world.events("execution.grant_reconciled_unclaimed")


def test_a_passed_receipt_is_not_released(tmp_path):
    world = SaladWorld(tmp_path)
    _executing(world)
    _record(world, {**PRE_CREATE_RECEIPT, "status": "passed", "reason": "verified_journey"})
    money = _money(world)
    world.tick(6)
    attempt = world.attempt()
    assert attempt["failure_class"] != "t1_salad_unclaimed"
    assert attempt["failure_detail"] != "live_check_create_not_sent"
    assert world.phase() != "unclaimed"
    assert _money(world) == money
    assert not world.events("execution.grant_reconciled_unclaimed")


# -- create_claim_state (automatic reconcile of unknown attempts) -----------------------------


def _claim_state(world: SaladWorld, receipt: dict[str, Any] | None):
    world.to_awaiting_review()
    attempt = world.attempt()
    row = world.salad()
    digest = "d" * 64
    world.env.execute(
        "UPDATE campaign_salad_attempts SET phase='unknown', live_receipt_json=?, "
        "live_receipt_digest=? WHERE attempt_id=?",
        None if receipt is None else json.dumps(receipt),
        None if receipt is None else digest,
        row["attempt_id"],
    )
    with world.env.store.runner_connection() as connection:
        return get_provider("salad").create_claim_state(connection, attempt)


def test_a_not_passed_receipt_with_no_provider_id_defers_to_the_claim_proof(tmp_path):
    claim = _claim_state(SaladWorld(tmp_path), PRE_CREATE_RECEIPT)
    assert claim.state == "none" and claim.proof == "no_create_or_start_claim"


@pytest.mark.parametrize(
    "receipt",
    [
        {**PRE_CREATE_RECEIPT, "status": "passed", "reason": "verified_journey"},
        {**PRE_CREATE_RECEIPT, "provider_id": "provider-group-1"},
        {**PRE_CREATE_RECEIPT, "create_rejection": {"code": "x"}},
    ],
    ids=["passed", "provider_id", "create_rejection"],
)
def test_any_other_receipt_is_evidence_and_stays_unknown(tmp_path, receipt):
    claim = _claim_state(SaladWorld(tmp_path), receipt)
    assert claim.state == "unknown" and claim.reason == "salad_record_has_evidence"
