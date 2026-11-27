"""A campaign blocked mid-pass does not cascade step refusals onto later reserved stages.

Field replay: stage s1 goes unknown during a pass while s2 and s3 hold reserved attempts that
still wait for ``issue_grant``. Delegation refuses forward steps on a blocked campaign, but the
refusal is about the campaign: later stages keep their reservation and hold, nothing is failed,
and no stop reason is stamped (ADR 0038 addendum).
"""

from __future__ import annotations

import dataclasses

import pytest
from campaign_runner_harness import World, stage_spec
from test_campaign_runner import METRIC, _spec

from tessera_control import campaign_steps
from tessera_control.campaign_runner import CampaignRunner

LATER = ("s2", "s3")


def _block(world: World, cid: str) -> None:
    """What ``_mark_stage_unknown`` does: s1 blocked_unknown and the campaign blocked."""
    world.env.execute(
        "UPDATE campaign_stages SET status='blocked_unknown' WHERE campaign_id=? "
        "AND stage_key='s1'",
        cid,
    )
    world.env.execute("UPDATE campaigns SET status='blocked' WHERE id=?", cid)


def _reserved_world(tmp_path) -> tuple[World, str]:
    world = World(tmp_path)
    stages = [
        stage_spec(world.env, k, metrics=METRIC, max_exposure_cents=2) for k in ("s1", "s2", "s3")
    ]
    cid = world.campaign(_spec(world, stages, budget_cap_cents=10))["id"]
    # Prepare passes reserve all three stages and run each one's task/experiment/budget steps;
    # the grant steps (release_snapshot, then issue_grant) are what is still to come.
    for _ in range(12):
        world.runner.tick()
        budgets = [i for i in world.env.intents(cid) if i["step"] == "authorize_budget"]
        if len(budgets) == 3 and all(i["status"] == "done" for i in budgets):
            break
    assert [a["status"] for a in world.attempts(cid)] == ["reserved"] * 3
    assert world.status(cid) == "running"
    assert [i for i in world.env.intents(cid) if i["step"] == "issue_grant"] == []
    return world, cid


def _snapshot(world: World, cid: str) -> dict:
    campaign = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)
    return {
        "holds": {a["stage_key"]: (a["status"], a["hold_cents"]) for a in world.attempts(cid)},
        "held": campaign["held_cents"],
        "settled": campaign["settled_cents"],
    }


def _assert_no_cascade(world: World, cid: str, before: dict) -> None:
    assert world.status(cid) == "blocked"
    after = _snapshot(world, cid)
    for key in LATER:
        assert after["holds"][key] == before["holds"][key] == ("reserved", 2)
    assert after["held"] == before["held"] and after["settled"] == before["settled"]
    assert world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"] is None
    for attempt in world.attempts(cid):
        assert attempt["status"] != "failed_permanent"
        assert attempt["failure_class"] != "step_refused"
    assert [i for i in world.env.intents(cid) if i["status"] == "failed"] == []
    assert world.events_of("campaign.step_failed") == []
    for stage in LATER:
        grants = [
            i
            for i in world.env.intents(cid)
            if i["step"] == "issue_grant" and i["stage_key"] == stage
        ]
        assert all(i["status"] == "pending" for i in grants)


def test_stage_blocking_the_campaign_ends_the_pass_for_later_reserved_stages(tmp_path, monkeypatch):
    world, cid = _reserved_world(tmp_path)
    before = _snapshot(world, cid)
    original = CampaignRunner._advance_stage

    def advance(self, ctx, key):
        progressed = original(self, ctx, key)
        if key == "s1":
            _block(world, cid)  # s1's own advance leaves the campaign blocked
        return progressed

    monkeypatch.setattr(CampaignRunner, "_advance_stage", advance)
    world.runner.tick()
    for stage in LATER:  # the pre-check ends the pass: no later forward step is even written
        assert [
            i
            for i in world.env.intents(cid)
            if i["stage_key"] == stage and i["step"] in {"issue_grant", "release_snapshot"}
        ] == []
    _assert_no_cascade(world, cid, before)
    # Later ticks keep a blocked campaign untouched as well.
    world.runner.tick()
    world.runner.tick()
    _assert_no_cascade(world, cid, before)


def test_block_raced_inside_the_step_call_is_transient_via_the_executor(tmp_path, monkeypatch):
    world, cid = _reserved_world(tmp_path)
    before = _snapshot(world, cid)
    real_prepare = campaign_steps.prepare
    raced: list[str] = []

    def racing(stores, step, ids, body, route_policy=None):
        prepared = real_prepare(stores, step, ids, body, route_policy)
        if step != "issue_grant":
            return prepared

        def call(principal, key):
            if not raced:
                raced.append(key)
                _block(world, cid)  # after the _step pre-check, before delegation admits
            return prepared.call(principal, key)

        return dataclasses.replace(prepared, call=call)

    monkeypatch.setattr(campaign_steps, "prepare", racing)
    for _ in range(4):
        world.runner.tick()
        if raced:
            break
    assert raced, "no issue_grant was attempted"
    (grant,) = [i for i in world.env.intents(cid) if i["step"] == "issue_grant"]
    assert grant["status"] == "pending" and grant["idempotency_key"] == raced[0]
    # No further tick: a synthetic block with no unknown attempt behind it is lifted by the
    # runner's own recovery, which is not what this test is about.
    _assert_no_cascade(world, cid, before)


@pytest.mark.parametrize("status", ["blocked", "cancel_requested"])
def test_step_precheck_refuses_forward_steps_before_writing_anything(tmp_path, status):
    from tessera_control.campaign_steps import StepTransient

    world, cid = _reserved_world(tmp_path)
    ctx = world.runner._load(cid)
    world.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, cid)
    before = len(world.env.intents(cid))
    with pytest.raises(StepTransient):
        world.runner._step(ctx, "s2", 1, "issue_grant", 0, lambda: ({}, {}))
    assert len(world.env.intents(cid)) == before
