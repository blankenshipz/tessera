"""A standing-approval start never resolves Salad reviews; a person still does."""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld


def test_a_standing_approval_start_stops_at_the_human_source_assessment(tmp_path):
    world = SaladWorld(tmp_path)
    world.start()
    world.env.execute(
        "UPDATE campaign_approvals SET decision_class='standing_approval', "
        "automation_label='standing_approval:standing_test' WHERE campaign_id=?",
        world.campaign_id,
    )
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    for _ in range(5):
        world.runner.tick()
    # Nothing assessed the source or resolved a review on a person's behalf.
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"
    assert world.row("SELECT count(*) AS n FROM decision_requests")["n"] == 0
    reviews = world.env.query(
        "SELECT step FROM campaign_step_intents WHERE campaign_id=? AND "
        "(step LIKE '%assess%' OR step LIKE '%resolve_decision%')",
        world.campaign_id,
    )
    assert reviews == []
    assert world.status() == "running"


def test_the_human_review_stops_remain_after_a_standing_start(tmp_path):
    world = SaladWorld(tmp_path)
    world.start()
    world.env.execute(
        "UPDATE campaign_approvals SET decision_class='standing_approval' WHERE campaign_id=?",
        world.campaign_id,
    )
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    world.assess_source()  # the person's review, done by the test as the human
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    pending = world.env.query("SELECT status FROM decision_requests")
    assert pending and all(row["status"] == "pending" for row in pending)
