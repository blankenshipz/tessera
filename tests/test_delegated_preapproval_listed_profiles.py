"""A review pre-approval a delegation writes (ADR 0150) covers every profile the approved spec
lists for the stage, the primary included, each with its own plan digest (ADR 0151); a profile
the spec does not list stays ``wrong_profile``. Synthetic fixtures only; no provider call."""

from __future__ import annotations

from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_delegated_preapproval_nested_transaction import world  # noqa: F401

from tessera_control import campaign_preapproval


def _recorded(world):  # noqa: F811
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    for _ in range(30):
        world.runner.tick()
        if world.events(campaign_preapproval.DELEGATED_PREAPPROVAL_EVENT):
            break
    (event,) = world.events(campaign_preapproval.DELEGATED_PREAPPROVAL_EVENT)
    with world.runner.store.runner_connection() as connection:
        pre = dict(
            connection.execute(
                "SELECT * FROM campaign_review_preapprovals WHERE id=?", (event["preapproval_id"],)
            ).fetchone()
        )
        campaign = connection.execute("SELECT * FROM campaigns").fetchone()
        return event, pre, dict(campaign), "attempt_synthetic"


def _digest_on(world, pre, campaign, attempt_id, profile, monkeypatch):  # noqa: F811
    # The profile the attempt runs on, as the execution plane records it.
    monkeypatch.setattr(
        "tessera_control.salad_alternate_profiles.attempt_profile", lambda *a, **k: profile
    )
    with world.runner.store.runner_connection() as connection:
        return campaign_preapproval._profile_plan_digest(
            connection, campaign, pre, pre["stage_key"], "salad", attempt_id
        )


def test_every_listed_profile_including_the_primary_is_covered(world, monkeypatch):  # noqa: F811
    event, pre, campaign, attempt_id = _recorded(world)
    profiles = {item["profile"]: item["plan_digest"] for item in event["profiles"]}
    assert list(profiles) == ["gpu-main", "gpu-alt1"]
    assert profiles["gpu-main"] == pre["plan_digest"]
    for name, digest in profiles.items():
        assert _digest_on(world, pre, campaign, attempt_id, name, monkeypatch) == digest


def test_an_unlisted_profile_stays_wrong_profile(world, monkeypatch):  # noqa: F811
    _event, pre, campaign, attempt_id = _recorded(world)
    assert _digest_on(world, pre, campaign, attempt_id, "gpu-unlisted", monkeypatch) is None
