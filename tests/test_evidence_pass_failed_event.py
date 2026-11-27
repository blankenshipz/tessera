"""A refusal that escapes a provider evidence hook is visible on the campaign.

One ``campaign.evidence_pass_failed`` event per (campaign, provider, step, class), beside the
runner's ``evidence_failed:`` action; repeats of the same refusal on later passes write no more.
Real stores and runner (``SaladWorld``) on synthetic state; no provider, no network.
"""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control.campaign_runner import EVIDENCE_PASS_FAILED_EVENT
from tessera_control.campaign_steps import StepFailed


def test_an_escaping_hook_refusal_records_one_event(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    driver_type = type(world.driver)

    def refusing(self, ctx):
        raise StepFailed("cancel_route", "step_refused", "sticky")

    monkeypatch.setattr(driver_type, "evidence_pass", refusing)

    for _ in range(3):
        report = world.runner.tick()
        assert any(action.startswith("evidence_failed:") for action in report.actions)

    events = world.events(EVIDENCE_PASS_FAILED_EVENT)
    assert len(events) == 1
    (event,) = events
    assert event["campaign_id"] == world.campaign_id
    assert event["provider"] == "salad"
    assert (event["step"], event["error_class"]) == ("cancel_route", "step_refused")
    assert world.status() == "running"


def test_a_different_refusal_class_records_its_own_event(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    driver_type = type(world.driver)
    classes = iter(["step_refused", "Conflict", "Conflict"])

    def refusing(self, ctx):
        raise StepFailed("revoke_grant", next(classes), "sticky")

    monkeypatch.setattr(driver_type, "evidence_pass", refusing)
    world.tick(3)
    found = sorted(e["error_class"] for e in world.events(EVIDENCE_PASS_FAILED_EVENT))
    assert found == ["Conflict", "step_refused"]


def test_no_event_once_the_lease_holder_changed_before_the_refusal_escapes(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    driver_type = type(world.driver)

    def refusing(self, ctx):
        # Another runner takes the lease while the hook runs; then the refusal escapes.
        world.env.execute("UPDATE campaign_runner_lease SET holder_id='rival'")
        raise StepFailed("cancel_route", "step_refused", "sticky")

    monkeypatch.setattr(driver_type, "evidence_pass", refusing)
    world.runner.tick()
    assert world.events(EVIDENCE_PASS_FAILED_EVENT) == []
