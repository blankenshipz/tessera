"""The approval view states the pre-custody reallocation bound (ADR 0179) at every value."""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, stage_spec

from tessera_control.campaign_models import CampaignSpec, spec_digest

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _spec(world, **retry):
    spec = world.env.spec()
    extra = {"retry": retry} if retry else {}
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
    return spec


@pytest.mark.parametrize(
    ("retry", "reallocation", "worst"),
    [
        ({}, 2, 3),
        ({"max_reallocation_retries": 0}, 0, 1),
        ({"max_reallocation_retries": 3}, 3, 4),
        ({"max_attempts": 2, "infra_retries": 1, "max_reallocation_retries": 3}, 3, 6),
    ],
)
def test_the_policy_view_projects_the_reallocation_bound(tmp_path, retry, reallocation, worst):
    world = World(tmp_path)
    submitted = world.env.submit(spec=_spec(world, **retry))
    (entry,) = submitted["infra_retry_policy"]
    assert entry["max_reallocation_retries"] == reallocation
    assert entry["worst_case_attempts"] == worst
    read = world.env.store.get_campaign(world.env.human(), submitted["id"])
    assert read["infra_retry_policy"] == submitted["infra_retry_policy"]


def test_the_default_stays_out_of_the_stored_spec_and_digest(tmp_path):
    world = World(tmp_path)
    submitted = world.env.submit(spec=_spec(world))
    assert "max_reallocation_retries" not in json.dumps(submitted["spec"]["stages"][0]["retry"])

    def digest(**retry):
        return spec_digest(CampaignSpec.model_validate(_spec(world, **retry)))

    assert digest() == digest(max_reallocation_retries=2)
    assert digest(max_reallocation_retries=0) != digest()
    assert digest(max_reallocation_retries=3) != digest()
