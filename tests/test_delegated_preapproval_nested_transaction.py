"""The delegated review pre-approval (ADR 0150) of a Salad stage that lists alternate profiles
records its event without opening a second runner transaction inside its own write one.

``runner_connection()`` always begins IMMEDIATE, and SQLite reserved locks are per connection,
so a nested one on the same thread waits on itself until ``busy_timeout`` and the runner defers
every tick. A reentrancy detector refuses the nested entry at once instead of waiting.

Synthetic fixtures only: the real stores and runner, a test-driven executor; no provider call.
"""

from __future__ import annotations

import threading
import traceback
from contextlib import contextmanager

import pytest
from salad_campaign_fixture import GPU, profile_data, salad_provider_reset  # noqa: F401
from test_owner_bound_review_delegation import Direct

from tessera_control import campaign_preapproval
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_store import CampaignStore
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

GPU2 = "550e8400-e29b-41d4-a716-446655440002"


class NestedRunnerTransaction(AssertionError):
    pass


@pytest.fixture
def nested(monkeypatch):
    """Every nested ``runner_connection()`` entry on one thread, refused before BEGIN IMMEDIATE."""
    original = CampaignStore.runner_connection
    depth = threading.local()
    seen: list[str] = []

    @contextmanager
    def guarded(self):
        if getattr(depth, "value", 0):
            seen.append("".join(traceback.format_stack(limit=8)))
            raise NestedRunnerTransaction("runner_connection() opened inside another one")
        depth.value = 1
        try:
            with original(self) as connection:
                yield connection
        finally:
            depth.value = 0

    monkeypatch.setattr(CampaignStore, "runner_connection", guarded)
    return seen


@pytest.fixture
def world(tmp_path):
    world = Direct(tmp_path, deferred=False)
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(
            schema="tessera.salad.campaign-profiles.v1",
            profiles=[
                profile_data(world.env, gpu_class=GPU, preapprove_reviews=True),
                profile_data(world.env, name="gpu-alt1", gpu_class=GPU2, preapprove_reviews=True),
            ],
        )
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    (stage,) = world.spec["stages"]
    stage["provider_config"] = {**stage["provider_config"], "alternate_profiles": ["gpu-alt1"]}
    stage["retry"] = {**stage["retry"], "max_class_fallbacks": 1}
    return world


def test_a_stage_with_alternate_profiles_records_its_delegated_preapproval_in_one_transaction(
    world, nested
):
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    for _ in range(30):  # the review stop is reached before the stage is leased
        world.runner.tick()
        if world.events(campaign_preapproval.DELEGATED_PREAPPROVAL_EVENT) or nested:
            break
    assert nested == [], nested[0]
    assert world.declined() == []
    assert world.events("campaign.stage_awaiting_human") == []
    (event,) = world.events(campaign_preapproval.DELEGATED_PREAPPROVAL_EVENT)
    assert event["provider_id"] == "salad"  # the stage's own candidate (ADR 0066)
    assert event["delegation_id"] == delegation["id"]
