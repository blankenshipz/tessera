# ruff: noqa: F811
"""A program budget that is already used up stops a billable placement (README: placement
checks "the remaining campaign and program budget"). Fakes only: no network, no money."""

from __future__ import annotations

import pytest
from test_campaign_models_v2 import _spec2
from test_campaign_placement import (
    FAR,
    NEAR,
    _placed,
    _stage,
    _world,
    providers,  # noqa: F401 - pytest fixture
)

from tessera_control import program_budget


@pytest.fixture(autouse=True)
def _clean_program_budget():
    program_budget.reset()
    yield
    program_budget.reset()


def _program(cap_cents: int) -> program_budget.ProgramBudgetConfig:
    return program_budget.ProgramBudgetConfig.model_validate(
        {
            "schema": program_budget.PROGRAM_BUDGET_SCHEMA,
            "programs": [{"id": "main", "cap_cents": cap_cents}],
        }
    )


def _tick(world, times=4):
    for _ in range(times):
        world.runner.tick()


def _waiting(world, campaign):
    return world.events_of("campaign.stage_waiting_placement")


def _ledger(world, campaign):
    row = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", campaign["id"])
    return row["held_cents"], row["settled_cents"]


def test_an_exhausted_program_budget_holds_nothing_and_the_stage_waits_then_runs(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    env = world.env
    alternate = {"provider": FAR, "runtime": "docker-cpu-v1", "max_exposure_cents": 10}
    campaign = world.campaign(
        _spec2(_stage(env, "a", alternates=(alternate,), exposure=30), budget_cap_cents=60)
    )
    env.execute("UPDATE execution_project_limits SET monetary_limit_cents=1000")
    # The campaign is admitted (it counts its full 60 cents); the operator cap is then 5.
    program_budget.configure(_program(5))
    _tick(world)
    assert world.attempts(campaign["id"]) == [] and not _placed(world, campaign["id"])
    assert _ledger(world, campaign) == (0, 0)
    waiting = _waiting(world, campaign)
    assert waiting and {
        (s["provider_id"], s["reason"]) for s in waiting[-1]["payload"]["skipped"]
    } == {
        (NEAR, "program_budget"),
        (FAR, "program_budget"),
    }
    assert world.status(campaign["id"]) not in {"failed", "succeeded"}
    # A person raising the operator cap lets the same stage place and finish.
    program_budget.configure(_program(1000))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded"


def test_an_unusable_program_budget_refuses_the_placement_too(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    campaign = world.campaign(_spec2(_stage(env, "a", exposure=30), budget_cap_cents=60))
    env.execute("UPDATE execution_project_limits SET monetary_limit_cents=1000")
    program_budget.mark_broken()
    _tick(world)
    assert world.attempts(campaign["id"]) == [] and _ledger(world, campaign) == (0, 0)
    waiting = _waiting(world, campaign)
    assert waiting and {
        (s["provider_id"], s["reason"], s["detail"]) for s in waiting[-1]["payload"]["skipped"]
    } == {
        (NEAR, "program_budget", "program_budget_unavailable"),
        (FAR, "program_budget", "program_budget_unavailable"),
    }
