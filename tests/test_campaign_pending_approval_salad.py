"""The campaign view lists a real Salad review awaiting a person, and a pre-approval clears it."""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_preapproval import PreWorld, _short


def _listed(world: SaladWorld) -> list[dict]:
    view = world.env.store.get_campaign(world.env.owner, world.campaign_id)
    return view["pending_human_approval"]


def test_a_real_attempt_awaiting_review_is_listed_until_a_person_decides(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    salad = world.salad()
    items = _listed(world)
    assert [(i["kind"], i["review"], i["stage_key"], i["attempt"]) for i in items] == [
        ("salad_review", "plan", "g1", 1),
        ("salad_review", "source_use", "g1", 1),
    ]
    assert [i["decision_request_id"] for i in items] == [
        salad["plan_decision_id"],
        salad["source_decision_id"],
    ]
    world.resolve_decisions()
    assert _listed(world) == []


def test_a_digest_bound_preapproval_resolves_the_review_so_nothing_is_listed(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    world.to_leased()
    world.tick_until_phase("route_started")
    assert world.events("campaign.stage_awaiting_human") == []
    assert _listed(world) == []
