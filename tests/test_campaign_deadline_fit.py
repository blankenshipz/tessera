"""Deadline fit: a window shorter than the stage plus settle margin is refused (fakes only)."""

from __future__ import annotations

import json

from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import SETTLE_MARGIN_SECONDS

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _campaign(world: World, deadline_seconds: int) -> dict:
    spec = world.env.spec(deadline_seconds=deadline_seconds)
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return world.campaign(spec)


def test_window_shorter_than_duration_plus_margin_fails_closed_without_authority(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world, 60 + SETTLE_MARGIN_SECONDS - 120)
    cid = campaign["id"]
    world.tick_all(cid)
    (attempt,) = world.attempts(cid)
    assert attempt["status"] == "failed_permanent"
    assert attempt["failure_class"] == "deadline_too_short"
    assert attempt["grant_id"] is None and attempt["release_id"] is None
    assert "remaining window" in attempt["failure_detail"]
    assert world.stages(cid)["s1"] == "failed"
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 0
    assert world.row("SELECT count(*) AS n FROM snapshot_releases WHERE status='active'")["n"] == 0
    ledger = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)
    assert (ledger["held_cents"], ledger["settled_cents"]) == (0, 0)
    intents = world.env.intents(cid)
    assert not [row for row in intents if row["step"] == "issue_grant"], intents
    (event,) = world.events_of("campaign.stage_deadline_too_short")
    assert event["payload"]["required_seconds"] == 60 + SETTLE_MARGIN_SECONDS
    assert 0 < event["payload"]["remaining_seconds"] < event["payload"]["required_seconds"]
    campaign_row = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)
    assert "deadline_too_short" in campaign_row["stop_reason"]
    # Retry-safe: further ticks neither loop nor add attempts.
    world.tick_all(cid)
    assert len(world.attempts(cid)) == 1
    assert len(world.events_of("campaign.stage_deadline_too_short")) == 1


def test_window_just_long_enough_proceeds_as_before(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world, 60 + SETTLE_MARGIN_SECONDS + 60)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["grant_id"] and attempt["failure_class"] is None
    assert world.events_of("campaign.stage_deadline_too_short") == []
    assert json.loads(attempt["measurements_json"])[0]["value"] == 3


def _expires(world: World, campaign_id: str):
    from datetime import datetime

    row = world.row(
        "SELECT expires_at FROM campaign_approvals WHERE campaign_id=? AND status='active'",
        campaign_id,
    )
    return datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))


def _at_remaining(world: World, campaign_id: str, seconds: int) -> None:
    from datetime import timedelta

    fixed = _expires(world, campaign_id) - timedelta(seconds=seconds)
    world.runner.now = lambda: fixed


def test_the_equality_boundary_passes_and_one_second_less_is_refused(tmp_path):
    required = 60 + SETTLE_MARGIN_SECONDS
    # remaining == required: allowed ("shorter than" is the refusal).
    (tmp_path / "exact").mkdir()
    (tmp_path / "short").mkdir()
    exact = World(tmp_path / "exact")
    campaign = _campaign(exact, 400)
    _at_remaining(exact, campaign["id"], required)
    exact.tick_all(campaign["id"])
    assert exact.events_of("campaign.stage_deadline_too_short") == []
    (attempt,) = exact.attempts(campaign["id"])
    assert attempt["grant_id"] and attempt["failure_class"] != "deadline_too_short"
    # remaining == required - 1: refused before any grant.
    short = World(tmp_path / "short")
    campaign = _campaign(short, 400)
    _at_remaining(short, campaign["id"], required - 1)
    short.tick_all(campaign["id"])
    (attempt,) = short.attempts(campaign["id"])
    assert attempt["failure_class"] == "deadline_too_short" and attempt["grant_id"] is None


def test_a_retry_attempt_is_rechecked_against_the_window_left_at_redispatch(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, retry={"max_attempts": 2})]
    campaign = world.campaign(spec)
    cid = campaign["id"]
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    (first,) = world.attempts(cid)
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        first["grant_id"],
    )
    # By the time the retry is dispatched the approval can no longer hold the stage.
    _at_remaining(world, cid, 60 + SETTLE_MARGIN_SECONDS - 1)
    world.tick_all(cid)
    one, two = world.attempts(cid)
    assert one["failure_class"] == "t1_grant_expired_unleased"
    assert two["failure_class"] == "deadline_too_short" and two["grant_id"] is None


def test_a_3600_second_stage_runs_with_a_window_of_at_least_65_minutes(tmp_path):
    """The model's maximum stage duration needs duration + settle margin = 65 minutes."""
    from datetime import UTC, datetime, timedelta

    world = World(tmp_path)
    # The fixture credentials live one hour; the window is the smallest of approval and
    # credential expiries, so the operator's credentials must outlast duration + margin.
    later = (datetime.now(UTC) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    world.env.execute("UPDATE credentials SET expires_at=? WHERE expires_at IS NOT NULL", later)
    spec = world.env.spec(deadline_seconds=3600 + SETTLE_MARGIN_SECONDS + 600)
    spec["stages"] = [
        stage_spec(
            world.env, "s1", metrics=METRIC, max_duration_seconds=3600, grant_ttl_seconds=3900
        )
    ]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert world.events_of("campaign.stage_deadline_too_short") == []
