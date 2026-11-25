"""The owner-bound review delegation (ADR 0150) clears the reviews of a stage that was placed on
its Salad alternate, and a stage the driver truly cannot preview still stops for a person with
the error recorded. Synthetic fixtures only: the real stores and runner, test doubles for the
CPU host, executor and guardian; no provider call."""

from __future__ import annotations

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_campaign_placement_salad import NEAR, _stage, near  # noqa: F401
from test_owner_bound_review_delegation import Direct

from tessera_control.campaign_salad import StepFailed


@pytest.fixture
def world(tmp_path, near):  # noqa: F811
    world = Direct(tmp_path, deferred=False)
    near(world, 0)  # the CPU host is busy, so the stage is placed on its Salad alternate
    stage = _stage(world.env)
    stage["max_duration_seconds"] = 600  # inside the test credentials' window
    stage["grant_ttl_seconds"] = 1200
    world.spec = salad_spec_v2(world.env, [stage])
    return world


def _to_review_stop(world: Direct) -> None:
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")


def test_a_stage_placed_on_its_salad_alternate_is_cleared_by_the_bound_delegation(world):
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    _to_review_stop(world)
    (placed,) = world.events("campaign.stage_placed")
    assert placed["provider_id"] == "salad"
    assert world.declined() == []
    assert world.events("campaign.stage_awaiting_human") == []
    uses = [dict(r) for r in world.env.query("SELECT * FROM campaign_review_preapproval_uses")]
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]
    assert {u["decision_class"] for u in uses} == {"campaign_delegated"}
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "supported"


def test_a_stage_that_cannot_be_previewed_stops_for_a_person_and_records_why(world, monkeypatch):
    def refuse(self, ctx, key, provider_id=None):
        raise StepFailed("salad", "step_refused", "salad_config_missing")

    monkeypatch.setattr(type(world.driver), "preview_review_digests", refuse)
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    for _ in range(8):
        world.runner.tick()
    (declined,) = world.events("campaign.preapproval_declined")
    assert declined["kind"] == "delegated_review"
    assert declined["reason"] == "review_preview_failed:StepFailed:salad_config_missing"
    assert world.events("campaign.review_preapproval_delegated") == []
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.events("campaign.stage_awaiting_human")
