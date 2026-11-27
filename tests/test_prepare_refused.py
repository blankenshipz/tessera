"""A provider preparation step that is refused fails the stage closed, once.

Salad scaffolding runs ``create_study`` before any reservation, grant or hold. A recorded
failed intent replays the same refusal on every later call, so the runner ends the stage with
``prepare_refused:<step>:<class>`` instead of raising on every tick. Real stores and runner
(``SaladWorld``) on synthetic state; no provider, no network.
"""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control.research import ResearchStore
from tessera_control.store import Conflict


def test_a_refused_prepare_step_fails_the_stage_without_a_loop(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.start()

    def refusing(self, *args, **kwargs):
        raise Conflict("synthetic refusal")

    monkeypatch.setattr(ResearchStore, "create_study", refusing)

    reports = [world.runner.tick() for _ in range(5)]

    stage = world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )
    assert stage["status"] == "failed", world.dump()
    campaign = world.row(
        "SELECT status, stop_reason, held_cents FROM campaigns WHERE id=?", world.campaign_id
    )
    assert campaign["stop_reason"].startswith(
        "stage_failed:g1:prepare_refused:create_study:Conflict"
    )
    assert campaign["held_cents"] == 0
    assert (
        world.row("SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=?", world.campaign_id)
        is None
    )
    failed = [
        e for e in world.events("campaign.step_failed") if e.get("campaign_id") == world.campaign_id
    ]
    assert len(failed) == 1, failed
    assert failed[0]["step"] == "create_study"
    mine = f"error:{world.campaign_id}"
    for report in reports:
        assert not any(action.startswith(mine) for action in report.actions), report.actions
    assert any(
        action.startswith("stage_failed:g1:prepare_refused:create_study:Conflict")
        for report in reports
        for action in report.actions
    )
