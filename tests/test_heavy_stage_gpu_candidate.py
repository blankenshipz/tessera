# ruff: noqa: F811
"""A GPU-required heavy stage gets the operator's Salad GPU candidate at submit (ADR 0141,
amendment 2026-11-23).

Fakes only: the Salad executor, guardian and human are ``SaladWorld`` test doubles, the CPU host
is a worker-grant provider with a fixed number of free slots, and no Salad call is made.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    profile_data,
    salad_provider_reset,
    salad_spec_v2,
)
from test_salad_stage_adapter import (  # noqa: F401
    NEAR,
    adapter_section,
    cpu_world,
    near,
    spec_of,
    stage,
)

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_placement_preference as preference
from tessera_control import program_budget
from tessera_control.campaign_models import HEAVY_REMOTE_ADDED
from tessera_control.campaign_provider_salad import SaladRouteProvider, register_provider_instance
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles
from tessera_control.store import canonical_digest

GIB = 1024**3
GPU_HEAVY = {"alternate_providers": None, "memory_bytes": 2 * GIB, "placement_tags": ["gpu"]}
CPU_HEAVY = {"alternate_providers": None, "memory_bytes": 2 * GIB}
SETTING = {"provider": "salad", "max_exposure_cents": 10}
# 600 s at 0.30 USD/hour is exactly 5 cents.
GPU_RATE = "0.30"
GPU_ESTIMATE = 5


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


def _configure(**over):
    preference.configure({"heavy_remote_candidate": {**SETTING, **over}}, [NEAR, "salad"])


def gpu_world(tmp_path, near, slots=0, **adapter_over):
    world = SaladWorld(
        tmp_path, stage_adapter=adapter_section(gpu_hour_usd=GPU_RATE, **adapter_over)
    )
    near(world, slots)
    return world


def both_world(tmp_path, near, slots=0):
    """A CPU and a GPU profile, both opened to adapted stages, in that order."""
    world = cpu_world(tmp_path, near, slots)
    gpu = profile_data(
        world.env,
        stage_adapter=adapter_section(gpu_hour_usd=GPU_RATE),
    )
    cpu = world.profiles.profiles[0].model_dump(mode="json")
    world.profiles = SaladCampaignProfiles.model_validate(
        {"schema": "tessera.salad.campaign-profiles.v1", "profiles": [cpu, gpu]}
    )
    register_provider_instance(world.profiles)
    return world


def _stored(world) -> dict:
    return json.loads(world.env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])


def _submit(world, spec=None, **over) -> dict:
    world.spec = spec or spec_of(world, **over)
    submitted = world.env.submit(spec=world.spec)
    return _stored(world)["stages"][0] | {"_digest": submitted["spec_digest"]}


def _placed(world):
    return world.events("campaign.stage_placed")


def test_a_gpu_heavy_stage_gets_the_gpu_profile_candidate_first_and_is_placed_there(tmp_path, near):
    _configure()
    world = both_world(tmp_path, near, slots=1)  # the local host is idle
    world.spec = spec_of(world, **GPU_HEAVY)
    world.start()
    stage = _stored(world)["stages"][0]
    first = stage["alternate_providers"][0]
    config = first["provider_config"]
    # The GPU profile, never the CPU one listed before it; ``gpu`` is omitted when True.
    assert first["provider"] == "salad" and config["profile"] == "gpu-main"
    assert config["adapted"] is True and "gpu" not in config
    assert first["max_exposure_cents"] == GPU_ESTIMATE
    record = stage["heavy_remote_candidate"]
    assert record == {
        "added": True,
        "reason": HEAVY_REMOTE_ADDED,
        "provider": "salad",
        "signals": record["signals"],
        "profile": "gpu-main",
        "max_exposure_cents": GPU_ESTIMATE,
    }
    assert "tag:gpu" in record["signals"]
    campaign = world.row("SELECT spec_digest FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["spec_digest"] == canonical_digest(_stored(world))
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and placed["candidates"][0] == "salad"
    assert placed["preference"]["class"] == "gpu"
    assert placed["preference"]["kinds"]["salad"] == "remote_gpu"
    assert not world.events(guard.EVENT)


def test_a_gpu_stage_with_only_a_cpu_profile_gets_nothing_and_says_why(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    stage = _submit(world, **GPU_HEAVY)
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    assert record["added"] is False and record["reason"] == "no_gpu_profile"
    assert "profile" not in record


def test_a_gpu_profile_not_opened_to_adapted_stages_is_not_used(tmp_path, near):
    _configure()
    world = SaladWorld(tmp_path)  # a GPU profile without a stage_adapter section
    near(world, 0)
    record = _submit(world, **GPU_HEAVY)["heavy_remote_candidate"]
    assert record["reason"] == "no_gpu_profile"


def test_a_named_gpu_profile_is_the_only_one_considered(tmp_path, near):
    _configure(gpu_profile="nope")
    world = gpu_world(tmp_path, near)
    record = _submit(world, **GPU_HEAVY)["heavy_remote_candidate"]
    assert record["reason"] == "no_gpu_profile"


@pytest.mark.parametrize(
    ("setting", "stage_over", "reason"),
    [
        ({"max_exposure_cents": GPU_ESTIMATE - 1}, {}, "over_operator_cap"),
        ({}, {"max_exposure_cents": GPU_ESTIMATE - 1}, "over_stage_cap"),
    ],
)
def test_a_gpu_estimate_over_a_cap_is_not_added(tmp_path, near, setting, stage_over, reason):
    _configure(**setting)
    world = gpu_world(tmp_path, near)
    stage = _submit(world, **GPU_HEAVY, **stage_over)
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    assert record["added"] is False and record["reason"] == reason
    assert record["profile"] == "gpu-main" and f"estimate {GPU_ESTIMATE}" in record["detail"]


def test_a_gpu_estimate_over_the_campaign_budget_is_not_added(tmp_path, near):
    _configure()
    world = gpu_world(tmp_path, near)
    env = world.env
    low = GPU_ESTIMATE - 1
    spec = salad_spec_v2(
        env, [stage(env, **GPU_HEAVY, max_exposure_cents=low)], budget_cap_cents=low
    )
    stage_record = _submit(world, spec=spec)
    # A stage's hold may not exceed the campaign budget (spec validation), so a budget below the
    # estimate always comes with a stage cap below it too: the first bound named is the stage's.
    assert "alternate_providers" not in stage_record
    record = stage_record["heavy_remote_candidate"]
    assert record["added"] is False and record["reason"] == "over_stage_cap"


def test_a_gpu_estimate_a_program_budget_cannot_admit_is_not_added(tmp_path, near):
    _configure()
    world = gpu_world(tmp_path, near)
    program_budget.configure(
        program_budget.ProgramBudgetConfig.model_validate(
            {
                "schema": program_budget.PROGRAM_BUDGET_SCHEMA,
                "programs": [{"id": "tiny", "cap_cents": 1}],
            }
        )
    )
    record = _submit(world, **GPU_HEAVY)["heavy_remote_candidate"]
    assert record["reason"] == "program_budget" and record["profile"] == "gpu-main"


def test_a_gpu_profile_the_adapter_refuses_is_recorded(tmp_path, near):
    _configure()
    world = gpu_world(tmp_path, near)
    stage = _submit(world, **GPU_HEAVY, max_duration_seconds=3601, grant_ttl_seconds=4300)
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    assert record["reason"] == "gpu_profile_unfit" and "3600" in record["detail"]


def test_a_skipped_gpu_candidate_falls_back_locally_with_its_reason(tmp_path, near, monkeypatch):
    _configure()
    world = gpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, **GPU_HEAVY)
    world.start()
    assert _stored(world)["stages"][0]["heavy_remote_candidate"]["added"] is True
    expired = {"state": "expired", "reference": "ref-1", "profile": "gpu-main", "expires_at": 1}
    monkeypatch.setattr(SaladRouteProvider, "credential_state", lambda s, p, now=None: expired)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    assert placed["candidates"][0] == "salad"  # the GPU candidate was tried first
    assert placed["skipped"][0]["provider_id"] == "salad"
    assert placed["heavy_guard"]["reasons"] == ["credential_unavailable"]
    (event,) = world.events(guard.EVENT)
    assert event["provider_id"] == NEAR and event["reasons"] == ["credential_unavailable"]


def test_a_cpu_heavy_stage_still_gets_the_cpu_profile_when_a_gpu_one_exists(tmp_path, near):
    _configure()
    world = both_world(tmp_path, near)
    stage = _submit(world, **CPU_HEAVY)
    first = stage["alternate_providers"][0]
    assert first["provider_config"]["profile"] == "cpu-main"
    assert first["provider_config"]["gpu"] is False
    assert stage["heavy_remote_candidate"]["profile"] == "cpu-main"


def test_a_cpu_heavy_stage_never_gets_a_gpu_profile(tmp_path, near):
    _configure()
    world = gpu_world(tmp_path, near)
    stage = _submit(world, **CPU_HEAVY)
    assert "alternate_providers" not in stage
    assert stage["heavy_remote_candidate"]["reason"] == "no_fitting_profile"


def test_a_gpu_adapter_section_needs_the_operator_gpu_rate(tmp_path, near):
    from tessera_control.salad_campaign_profiles import SaladCampaignProfile

    world = cpu_world(tmp_path, near)
    cpu = world.profiles.profiles[0].model_dump(mode="json")
    gpu = profile_data(world.env)
    for bad, match in (
        ({**gpu, "stage_adapter": adapter_section()}, "gpu_hour_usd"),
        ({**gpu, "stage_adapter": adapter_section(gpu_hour_usd="0")}, "positive"),
        ({**cpu, "stage_adapter": adapter_section(gpu_hour_usd=GPU_RATE)}, "gpu_hour_usd"),
    ):
        with pytest.raises(ValueError, match=match):
            SaladCampaignProfile.model_validate(bad)


def test_an_explicit_adapter_request_still_refuses_a_gpu_profile(tmp_path, near):
    from tessera_control.campaign_models import parse_spec
    from tessera_control.campaign_salad_adapter import expand_spec
    from tessera_control.store import Conflict

    world = gpu_world(tmp_path, near)
    request = {
        "provider": "salad",
        "runtime": "salad-workload-v1",
        "provider_config": {"adapt": {"profile": "gpu-main"}},
    }
    with pytest.raises(Conflict, match="not a CPU-only profile"):
        expand_spec(
            parse_spec(spec_of(world, alternate_providers=[request])),
            world.profiles,
            tenant=None,
        )


def test_the_gpu_profile_setting_is_strict():
    for bad in ({"gpu_profile": "Bad Name"}, {"gpu_profile": 1}):
        with pytest.raises(ValueError):
            preference.configure({"heavy_remote_candidate": {**SETTING, **bad}}, ())
