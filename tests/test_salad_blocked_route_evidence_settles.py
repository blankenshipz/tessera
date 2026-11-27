"""A block stops new spend, never the record of a run already made (q-2705 F2, F3, F5).

Field shapes replayed on synthetic state (no provider, no network): a Salad route that ran
and stopped with provider-confirmed termination while its campaign was blocked; the same run
after the runner had already called it ``unknown`` because the executor was silent (the
sanctioned late import); and an unblock after a long block with one healthy route running.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld

from tessera_control.campaign_salad_commands import SILENT_EXECUTOR
from tessera_control.store import Conflict

CONTENT = b'{"score": 1}'


def _receipt() -> dict:
    return {
        "schema": "tessera.salad.live-check.v1",
        "status": "passed",
        "reason": "verified_journey",
        "stop_confirmed": True,
        "outputs": [
            {
                "name": "output.json",
                "sha256": hashlib.sha256(CONTENT).hexdigest(),
                "bytes": len(CONTENT),
            }
        ],
    }


def _ran_and_stopped(world: SaladWorld, *, terminated: bool = True) -> dict:
    """The executor double for a route that ran (claims, upload, confirmed stop, exited
    observation) but whose outcome was not yet recorded; returns the manifest."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    manifest = world.upload_manifest(CONTENT)
    if terminated:
        world.terminate_route()
        world.observe_exit()
    return manifest


def _record_outcome(world: SaladWorld, manifest: dict) -> None:
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=_receipt(),
            manifest_id=manifest["id"],
            provider_id="provider-group-1",
            estimated_exposure={"estimated_exposure_cents": 7},
        ),
    )


def _block(world: SaladWorld) -> None:
    world.env.execute("UPDATE campaigns SET status='blocked' WHERE id=?", world.campaign_id)


def _later(world: SaladWorld, hours: int) -> None:
    world.runner.now = lambda: datetime.now(UTC) + timedelta(hours=hours)


def _silent_unknown(world: SaladWorld) -> dict:
    """The attempt goes unknown for the silent reason and blocks the campaign, as the v0.103.0
    runner did after a long block (the field case)."""
    world.tick()
    attempt = world.attempt()
    world.driver._unknown(attempt, SILENT_EXECUTOR)
    ctx = world.runner._load(world.campaign_id)
    world.runner._stage_unknown(ctx, "g1")
    assert world.status() == "blocked"
    return world.attempt()


def _listed(world: SaladWorld) -> dict:
    return {item["attempt_id"]: item for item in world.env.store.list_salad_work(world.worker)}


def _settles_by_charge(world: SaladWorld, attempt_id: str) -> None:
    world.tick_until_phase("awaiting_charge")
    now = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt_id)
    assert now["status"] != "failed_permanent"
    assert now["settlement_basis"] != "unclaimed_zero"
    assert world.salad()["phase"] == "awaiting_charge"
    world.record_charge(3)
    world.tick_until(
        lambda: (
            world.row("SELECT settled_cents FROM campaign_stage_attempts WHERE id=?", attempt_id)[
                "settled_cents"
            ]
            is not None
        )
    )
    settled = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt_id)
    assert settled["settled_cents"] == 3
    assert settled["settlement_basis"] != "unclaimed_zero"
    assert world.events("execution.grant_reconciled_unclaimed") == []


# -- F2: the worker gate split by direction -------------------------------------------------


def test_blocked_campaign_keeps_started_routes_listed_and_refuses_forward_commands(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    attempt_id = world.salad()["attempt_id"]
    _block(world)

    item = _listed(world)[attempt_id]
    assert item["phase"] == "route_started" and item["campaign_status"] == "blocked"
    with pytest.raises(Conflict):
        world.env.store.mark_salad_executing(world.worker, attempt_id, world.key("executing"))
    assert world.salad()["phase"] == "route_started"


def test_paused_campaign_lists_pre_route_work_for_keepalive_only(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.env.execute(
        "UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", world.campaign_id
    )
    item = _listed(world)[world.salad()["attempt_id"]]
    assert (
        item["phase"] == "awaiting_review" and item["campaign_status"] == "paused_delegation_lapsed"
    )


def test_outcome_recorded_during_a_block_verifies_and_settles_by_the_charge_path(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    manifest = _ran_and_stopped(world)
    attempt_id = world.salad()["attempt_id"]
    _block(world)
    assert _listed(world)[attempt_id]["campaign_status"] == "blocked"

    # Evidence is accepted while blocked (it was a Conflict before v0.104.0).
    _record_outcome(world, manifest)
    assert world.salad()["phase"] == "outcome_recorded"

    world.tick()  # blocked: the evidence pass verifies; no forward step is taken
    verified = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt_id)
    assert verified["status"] == "exited" and verified["salad_verdict"] == "ok"
    assert not [
        row
        for row in world.env.query(
            "SELECT status FROM campaign_step_intents WHERE campaign_id=? AND status='failed'",
            world.campaign_id,
        )
    ]
    _settles_by_charge(world, attempt_id)


# -- F3: the silent clock and the sanctioned late import ------------------------------------


def test_termination_confirmed_route_waits_for_the_import_not_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _ran_and_stopped(world)
    _later(world, 3)

    world.tick(2)

    assert world.attempt()["status"] != "unknown"
    assert world.status() == "running"
    assert len(world.events("campaign.salad_import_awaited")) == 1


def test_late_import_of_a_silent_unknown_finished_run_settles_ordinarily(tmp_path):
    """s17-20e64973: stopped and termination-confirmed, the outcome refused while blocked,
    then called silent. The restarted executor records it; the runner verifies it."""
    world = SaladWorld(tmp_path)
    world.to_route_started()
    manifest = _ran_and_stopped(world)
    attempt = _silent_unknown(world)
    item = _listed(world)[attempt["id"]]
    assert item["phase"] == "unknown" and item["campaign_status"] == "blocked"

    _record_outcome(world, manifest)
    (event,) = [
        e
        for e in world.events("campaign.salad_outcome_recorded")
        if e["attempt_id"] == attempt["id"]
    ]
    assert event["late_import"] is True

    world.tick()
    now = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert now["status"] == "exited" and now["failure_class"] is None
    assert world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )["status"] in {"running", "succeeded"}
    assert len(world.events("campaign.salad_late_import_verified")) == 1
    _settles_by_charge(world, attempt["id"])
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert not (stop["stop_reason"] or "").startswith("stage_failed")


def test_late_outcome_after_a_reconciled_grant_is_refused(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    manifest = _ran_and_stopped(world)
    attempt = _silent_unknown(world)
    # A person reconciled the grant (only the field the gate reads is set here).
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled' WHERE id=?", attempt["grant_id"]
    )
    assert attempt["id"] not in _listed(world)
    with pytest.raises(Conflict):
        _record_outcome(world, manifest)
    assert world.salad()["phase"] == "unknown"


def test_late_outcome_of_a_settled_attempt_is_refused(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    manifest = _ran_and_stopped(world)
    attempt = _silent_unknown(world)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET settled_cents=0 WHERE id=?", attempt["id"]
    )
    with pytest.raises(Conflict):
        _record_outcome(world, manifest)


def test_other_unknowns_are_not_late_importable(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    manifest = _ran_and_stopped(world)
    world.tick()
    attempt = world.attempt()
    world.driver._unknown(attempt, "claimed provider effect without confirmed termination")
    world.runner._stage_unknown(world.runner._load(world.campaign_id), "g1")
    assert attempt["id"] not in _listed(world)
    with pytest.raises(Conflict):
        _record_outcome(world, manifest)


def test_late_outcome_without_confirmed_termination_stays_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    manifest = _ran_and_stopped(world, terminated=False)
    attempt = _silent_unknown(world)
    _record_outcome(world, manifest)

    world.tick(2)

    now = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert now["status"] == "unknown" and now["settled_cents"] is None
    assert world.status() == "blocked"
    assert world.salad()["phase"] == "unknown"
    # No longer the silent reason: not listed again, the human path stays.
    assert attempt["id"] not in _listed(world)
    assert world.events("campaign.salad_late_import_verified") == []


# -- F5: unblocking does not manufacture unknowns from the downtime -------------------------


def test_unblock_after_a_long_block_does_not_reblock_a_healthy_route(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    _block(world)
    _later(world, 3)  # the block lasted longer than the route window plus grace

    world.tick()  # blocked, nothing unknown: the runner unblocks
    assert world.status() == "running"
    (unblocked,) = world.events("campaign.unblocked")
    assert "at" in unblocked
    blocks_before = len(world.events("campaign.blocked"))

    world.tick(3)

    assert world.attempt()["status"] == "running"
    assert world.salad()["phase"] == "executing"
    assert world.status() == "running"
    assert len(world.events("campaign.blocked")) == blocks_before
