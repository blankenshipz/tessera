"""ADR 0151: the stage-status and campaign-logs reads show a Salad stage's profile list.

Keys and codes only: the ordered profile keys, the profile the latest attempt ran on, each
pre-start switch with its reason code, attempt numbers and started/spent flags, and the
not-approved or post-start-refused outcome (ADR 0169).
Rows and events are seeded directly; nothing here runs a provider.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control.agent_stage_status import salad_profile_views, stage_status
from tessera_control.campaign_salad_fallback import STOP_EVENT, SWITCH_EVENT

SECRET_TEXT = "free text with host 203.0.113.9 and secret"


@pytest.fixture
def world(tmp_path):
    world = SaladWorld(tmp_path)
    world.start()
    # The runner writes this row when it first reaches the stage; seed it as it would.
    world.env.execute(
        "INSERT INTO campaign_salad_stages (campaign_id, stage_key, profile, revision, "
        "updated_at) VALUES (?, 'g1', 'gpu-main', 1, '2026-11-23T00:00:00Z')",
        world.campaign_id,
    )
    return world


def _alternates(world, names):
    """Rewrite the stored spec so the Salad stage lists these alternate profiles."""
    row = world.row("SELECT spec_json FROM campaigns WHERE id=?", world.campaign_id)
    spec = json.loads(row["spec_json"])
    stage = spec["stages"][0]
    stage["salad"]["alternate_profiles"] = list(names)
    world.env.execute(
        "UPDATE campaigns SET spec_json=? WHERE id=?", json.dumps(spec), world.campaign_id
    )


def _switch(world, **body):
    _event(world, SWITCH_EVENT, **body)


def _event(world, event_type, **body):
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    payload = {"campaign_id": world.campaign_id, "stage_key": "g1", **body}
    world.env.execute(
        "INSERT INTO events (org_id, project_id, event_type, resource_revision, "
        "actor_principal_id, payload_json, created_at) "
        "VALUES (?, ?, ?, 1, ?, ?, '2026-11-23T00:00:00Z')",
        campaign["org_id"],
        campaign["project_id"],
        event_type,
        world.env.worker["id"],
        json.dumps(payload),
    )


def _view(world):
    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    with world.env.store.runner_connection() as connection:
        return stage_status(connection, campaign)["stages"][0]


def test_single_profile_stage_is_backward_compatible(world):
    stage = _view(world)
    assert stage["profile"] == "gpu-main"
    assert stage["salad_profiles"] == {
        "profiles": ["gpu-main"],
        "latest_attempt_profile": "gpu-main",
        "switches": [],
        "outcome": None,
    }
    assert {"stage_key", "status", "provider_id", "placement", "latest_attempt"} <= set(stage)


def test_profile_list_is_ordered_with_the_primary_first(world):
    _alternates(world, ["gpu-alt-a", "gpu-alt-b"])
    view = _view(world)["salad_profiles"]
    assert view["profiles"] == ["gpu-main", "gpu-alt-a", "gpu-alt-b"]
    assert view["latest_attempt_profile"] == "gpu-main"
    assert view["switches"] == []


def test_switches_and_the_latest_profile_are_listed_with_reason_codes(world):
    _alternates(world, ["gpu-alt-a", "gpu-alt-b"])
    _switch(
        world,
        from_profile="gpu-main",
        to_profile="gpu-alt-a",
        reason="prestart_no_spend",
        from_attempt=1,
        to_attempt=2,
    )
    _switch(
        world,
        from_profile="gpu-alt-a",
        to_profile="gpu-alt-b",
        reason=SECRET_TEXT,
        from_attempt=2,
        to_attempt=3,
    )
    view = _view(world)["salad_profiles"]
    assert view["latest_attempt_profile"] == "gpu-alt-b"
    first, second = view["switches"]
    assert (first["from_profile"], first["to_profile"]) == ("gpu-main", "gpu-alt-a")
    assert first["reason"] == "prestart_no_spend"
    assert (first["from_attempt"], first["to_attempt"]) == (1, 2)
    assert second["reason"] == "detail_withheld"
    assert SECRET_TEXT not in json.dumps(view)


@pytest.mark.parametrize(
    ("code", "detail", "started"),
    [("profile_not_approved", "over_run_cap", False), ("post_start_switch_refused", None, True)],
)
def test_the_stop_outcome_is_shown(world, code, detail, started):
    _alternates(world, ["gpu-alt-a"])
    _event(
        world,
        STOP_EVENT,
        attempt=2,
        outcome=code,
        detail=detail,
        to_profile="gpu-alt-a",
        started=started,
        evidence={"free": SECRET_TEXT},
    )
    outcome = _view(world)["salad_profiles"]["outcome"]
    assert outcome == {
        "outcome": code,
        "detail": detail,
        "attempt": 2,
        "next_profile": "gpu-alt-a",
        "started": started,
    }
    assert SECRET_TEXT not in json.dumps(outcome)


def test_a_removed_outcome_code_is_withheld(world):
    # ADR 0169: the post-start switch codes of the first ADR 0151 draft are not shown as codes.
    _event(world, STOP_EVENT, attempt=1, outcome="profiles_exhausted", detail=None)
    assert _view(world)["salad_profiles"]["outcome"]["outcome"] == "detail_withheld"


def test_a_switch_shows_that_nothing_started_or_was_spent(world):
    _alternates(world, ["gpu-alt-a"])
    _switch(
        world,
        from_profile="gpu-main",
        to_profile="gpu-alt-a",
        reason="prestart_no_spend",
        from_attempt=1,
        to_attempt=2,
        started=False,
        spent=False,
    )
    [switch] = _view(world)["salad_profiles"]["switches"]
    assert (switch["started"], switch["spent"]) == (False, False)


def test_an_unknown_outcome_or_detail_is_withheld(world):
    _event(world, STOP_EVENT, attempt=1, outcome=SECRET_TEXT, detail=SECRET_TEXT)
    outcome = _view(world)["salad_profiles"]["outcome"]
    assert outcome["outcome"] == "detail_withheld" and outcome["detail"] == "detail_withheld"


def test_a_hostile_profile_key_is_withheld(world):
    _alternates(world, ["gpu-alt-a"])
    _switch(
        world,
        from_profile="198.51.100.7",
        to_profile="worker.internal.example.com",
        reason="prestart_no_spend",
        from_attempt=1,
        to_attempt=2,
    )
    world.env.execute(
        "UPDATE campaign_salad_stages SET profile='10.0.0.1' WHERE campaign_id=?",
        world.campaign_id,
    )
    stage = _view(world)
    assert stage["profile"] == "detail_withheld"
    view = stage["salad_profiles"]
    assert view["switches"][0]["from_profile"] == "detail_withheld"
    assert view["switches"][0]["to_profile"] == "detail_withheld"
    assert view["latest_attempt_profile"] == "detail_withheld"
    assert view["profiles"] == ["gpu-main", "gpu-alt-a"]
    text = json.dumps(stage)
    assert "198.51.100.7" not in text and "internal.example.com" not in text
    assert "10.0.0.1" not in text


def test_the_campaign_logs_read_shows_the_same_view(world):
    _alternates(world, ["gpu-alt-a"])
    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    with world.env.store.runner_connection() as connection:
        shown = salad_profile_views(connection, campaign)
    assert shown["g1"]["profiles"] == ["gpu-main", "gpu-alt-a"]


def test_the_view_holds_only_keys_codes_and_counts(world):
    _alternates(world, ["gpu-alt-a"])
    _switch(
        world,
        from_profile="gpu-main",
        to_profile="gpu-alt-a",
        reason="prestart_no_spend",
        from_attempt=1,
        to_attempt=2,
        detail=SECRET_TEXT,
        host="203.0.113.9",
    )
    text = json.dumps(_view(world)["salad_profiles"])
    assert "203.0.113.9" not in text and "secret" not in text
    assert set(_view(world)["salad_profiles"]["switches"][0]) == {
        "from_profile",
        "to_profile",
        "reason",
        "from_attempt",
        "to_attempt",
        "started",
        "spent",
        "source",  # closed: spec_alternate | operator_fallback (a legacy switch: spec_alternate)
        "at",
    }
