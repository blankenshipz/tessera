"""One stage's error never starves the stages after it (ADR 0210).

Real stores, runner and a fake worker on temporary state; the failing stage is simulated by
patching one runner method for one stage key.
"""

from __future__ import annotations

from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import STAGE_RUNNER_ERROR_EVENT, CampaignRunner
from tessera_control.campaign_steps import StepFailed


def _two_stage(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "a"), stage_spec(world.env, "b")]
    return world.campaign(spec)


def test_a_raising_stage_does_not_stop_the_later_stage_and_the_campaign_completes(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    campaign = _two_stage(world)
    cid = campaign["id"]
    original = CampaignRunner._advance_stage
    raised = []

    def flaky(self, ctx, key):
        if key == "a" and len(raised) < 8:
            raised.append(key)
            raise KeyError("synthetic")
        return original(self, ctx, key)

    monkeypatch.setattr(CampaignRunner, "_advance_stage", flaky)
    report = world.runner.tick()
    assert f"error:{cid}:a:KeyError" in report.actions
    # Stage b (later in topological order) still moves while a raises on every pass.
    world.tick_until(lambda: bool(world.attempts(cid)), worker=False)
    assert len(raised) < 8
    assert [row["stage_key"] for row in world.attempts(cid)] == ["b"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert world.stages(cid) == {"a": "succeeded", "b": "succeeded"}
    events = [
        e["payload"]
        for e in world.events_of(STAGE_RUNNER_ERROR_EVENT)
        if e["payload"]["campaign_id"] == cid
    ]
    # Once per (campaign, stage, class), however many passes raised it; class name only.
    assert events == [{"campaign_id": cid, "stage_key": "a", "error_class": "KeyError"}]


def test_a_step_refusal_before_any_attempt_fails_only_that_stage(tmp_path, monkeypatch):
    world = World(tmp_path)
    campaign = _two_stage(world)
    cid = campaign["id"]
    original = CampaignRunner._place_and_reserve

    def refusing(self, ctx, key, **kwargs):
        if key == "b":
            raise StepFailed("issue_grant", "step_refused", "synthetic")
        return original(self, ctx, key, **kwargs)

    monkeypatch.setattr(CampaignRunner, "_place_and_reserve", refusing)
    report = world.runner.tick()
    assert any(action.startswith("stage_failed:b:step_failed:") for action in report.actions)
    assert not any(action.startswith(f"error:{cid}") for action in report.actions)
    world.tick_all(cid)
    stages = world.stages(cid)
    assert stages["b"] == "failed"
    # The earlier stage's in-flight attempt ran to its end; it was not failed with b.
    assert stages["a"] == "succeeded", dump(world, cid)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:b:step_failed:issue_grant:step_refused"
    assert [row["stage_key"] for row in world.attempts(cid)] == ["a"]


def test_a_step_refusal_with_a_live_attempt_is_recorded_not_failed(tmp_path, monkeypatch):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "a")]
    cid = world.campaign(spec)["id"]
    world.tick_until(lambda: bool(world.attempts(cid)), worker=False)
    original = CampaignRunner._unwind_failed
    calls = []

    def refusing(self, ctx, key):
        if len(calls) < 2:
            calls.append(key)
            raise StepFailed("revoke_grant", "step_refused", "synthetic")
        return original(self, ctx, key)

    monkeypatch.setattr(CampaignRunner, "_unwind_failed", refusing)
    report = world.runner.tick()
    assert f"error:{cid}:a:StepFailed" in report.actions
    assert world.stages(cid)["a"] != "failed"
    events = [e["payload"] for e in world.events_of(STAGE_RUNNER_ERROR_EVENT)]
    assert events == [
        {
            "campaign_id": cid,
            "stage_key": "a",
            "error_class": "StepFailed",
            "step": "revoke_grant",
            "step_error_class": "step_refused",
        }
    ]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
