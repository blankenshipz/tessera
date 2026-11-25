"""ADR 0169: one alternate-profile list, pre-start moves only, every move a recorded switch.

Real stores and runner; the Salad executor, guardian and human are the test doubles of the Salad
campaign fixture (no provider call, no network, no money). A pre-start failure is the rows the
executor writes for one (a route with a ``create`` effect and no ``start``, the provider's
confirmed stop, a live receipt naming the step that failed); an unstable run is the route state
the control plane records for a reallocation. Nothing here proves provider behavior.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from salad_campaign_fixture import (  # noqa: F401
    GPU,
    SaladWorld,
    profile_data,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_campaign_class_fallback import _attempts, _prestart_failure, _to_next_route
from test_salad_profile_fallback import _ran_unstable

from tessera_control import campaign_allocation_instability as instability
from tessera_control import campaign_class_fallback as class_fallback
from tessera_control import campaign_heavy_guard as heavy_guard
from tessera_control import campaign_models
from tessera_control import campaign_salad_fallback as fallback
from tessera_control.agent_stage_status import stage_status
from tessera_control.campaign_models import SaladStageSpec
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_receipts import build_receipt
from tessera_control.gate_evidence_paired import PairedRefused, _pair_class
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

GPU_B = "650e8400-e29b-41d4-a716-446655440000"
GPU_C = "750e8400-e29b-41d4-a716-446655440000"


def _world(tmp_path, names=("gpu-b", "gpu-c"), *, fallbacks=None, **over) -> SaladWorld:
    """A Salad stage on ``gpu-main`` listing ``names`` (each its own GPU class)."""
    world = SaladWorld(tmp_path)
    env = world.env
    classes = [GPU_B, GPU_C]
    documents = [profile_data(env)] + [
        profile_data(env, name=name, gpu_class=classes[index], **over.get(name, {}))
        for index, name in enumerate(names)
    ]
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(schema="tessera.salad.campaign-profiles.v1", profiles=documents)
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    stage = salad_stage(env, "g1")
    stage["salad"] = {**stage["salad"], "alternate_profiles": list(names)}
    stage["retry"] = {**stage["retry"], "max_class_fallbacks": fallbacks or len(names)}
    world.spec = salad_spec(env, [stage])
    return world


def _prestart_moved(world: SaladWorld, count: int) -> None:
    """Fail the current attempt before start at zero, then wait for attempt ``count + 1``."""
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: len(_attempts(world)) == count + 1)


def _gpu_class(world: SaladWorld) -> str:
    return world.route_state()["proposal"]["selection"]["resources"]["gpu_class"]


def _campaign(world: SaladWorld) -> dict:
    return dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))


def _receipt(world: SaladWorld) -> dict:
    with world.env.store.runner_connection() as connection:
        receipt, _ = build_receipt(connection, _campaign(world), "running")
    return receipt


# -- one field, one cap, one selector ----------


def test_one_alternate_profiles_definition_and_one_length_cap(tmp_path):
    world = SaladWorld(tmp_path)
    base = world.spec["stages"][0]["salad"]
    assert campaign_models.MAX_CLASS_FALLBACKS == campaign_models.MAX_ALTERNATE_PROFILES == 4
    names = [f"gpu-{index}" for index in range(campaign_models.MAX_ALTERNATE_PROFILES)]
    assert SaladStageSpec.model_validate({**base, "alternate_profiles": names})
    with pytest.raises(ValidationError) as error:
        SaladStageSpec.model_validate({**base, "alternate_profiles": [*names, "gpu-x"]})
    # Refused once, by the one length bound (no second validator with another cap).
    assert len(error.value.errors()) == 1
    assert error.value.errors()[0]["type"] == "too_long"
    assert not hasattr(SaladStageSpec, "class_profiles")
    # The same field feeds the move bound: an over-large bound is refused by the retry model.
    with pytest.raises(ValidationError):
        campaign_models.RetryPolicy(max_class_fallbacks=campaign_models.MAX_CLASS_FALLBACKS + 1)


def test_the_selector_never_reselects_a_tried_profile():
    class Config:
        profile = "a"
        alternate_profiles = ["b", "c"]

    assert fallback.tried([], "a") == ("a",)
    recorded = [{"to_profile": "b", "to_attempt": 2}]
    assert fallback.tried(recorded, "a") == ("a", "b")
    from tessera_control.salad_alternate_profiles import next_profile

    assert next_profile(Config(), fallback.tried(recorded, "a")) == "c"
    assert next_profile(Config(), ("a", "b", "c")) is None


# -- the pre-start move is a recorded profile switch ----------


def test_a_prestart_move_records_a_switch_and_the_receipt_shows_the_profile_that_ran(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    assert _gpu_class(world) == GPU
    _prestart_moved(world, 1)
    first, _second = _attempts(world)
    assert first["failure_class"] == class_fallback.PRESTART_NO_SPEND
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert (switch["from_profile"], switch["to_profile"]) == ("gpu-main", "gpu-b")
    assert (switch["from_attempt"], switch["to_attempt"]) == (1, 2)
    assert switch["reason"] == fallback.REASON_PRESTART_NO_SPEND
    assert switch["prestart_reason"] == "group_unsettled"
    assert switch["started"] is False and switch["spent"] is False
    assert switch["settled_cents"] == 0 and switch["attempt_id"] == first["id"]
    assert switch["provider_id"] == "salad"
    # The move bound's own event is recorded with it (one transaction).
    assert len(world.events(class_fallback.EVENT)) == 1
    _to_next_route(world)
    assert _gpu_class(world) == GPU_B

    entry = _receipt(world)["stages"][0]
    assert [a["salad_profile"] for a in entry["attempts"]] == ["gpu-main", "gpu-b"]
    [recorded] = entry["salad_profiles"]["switches"]
    assert recorded["reason"] == "prestart_no_spend" and recorded["started"] is False
    assert recorded["spent"] is False and recorded["attempt_id"] == first["id"]
    with world.env.store.runner_connection() as connection:
        view = stage_status(connection, _campaign(world))["stages"][0]["salad_profiles"]
    assert view["latest_attempt_profile"] == "gpu-b"
    assert view["switches"][0]["started"] is False


def test_a_and_b_then_b_unstable_never_reselects_b_or_reaches_c(tmp_path):
    """[A, B, C]: A fails before start (move to B); B starts and goes unstable: the strict rule
    refuses any move (C is never used), B is never selected again, and the stop names both."""
    world = _world(tmp_path)
    world.to_route_started()
    _prestart_moved(world, 1)
    _to_next_route(world)
    assert _gpu_class(world) == GPU_B
    _ran_unstable(world, within=False)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(4)
    world.tick_until(lambda: world.status() == "blocked")

    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["to_profile"] == "gpu-b"
    [stop] = world.events(fallback.STOP_EVENT)
    assert stop["outcome"] == fallback.POST_START_REFUSED and stop["attempt"] == 2
    assert stop["tried"] == ["gpu-main", "gpu-b"] and stop["from_profile"] == "gpu-b"
    assert stop["started"] is True and stop["spent"] is True
    assert len(_attempts(world)) == 2
    world.tick(3)
    assert len(_attempts(world)) == 2 and len(world.events(fallback.STOP_EVENT)) == 1
    used = [a["salad_profile"] for a in _receipt(world)["stages"][0]["attempts"]]
    assert used == ["gpu-main", "gpu-b"]


def test_a_prestart_move_to_an_uncovered_profile_holds_instead(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    # After approval the operator lowered the next profile's run cap below the stage's hold.
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(
            schema="tessera.salad.campaign-profiles.v1",
            profiles=[
                profile_data(world.env),
                profile_data(
                    world.env, name="gpu-b", gpu_class=GPU_B, max_run_cents=5, max_task_cents=5
                ),
                profile_data(world.env, name="gpu-c", gpu_class=GPU_C),
            ],
        )
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: world.status() == "blocked")
    assert world.events(fallback.SWITCH_EVENT) == []
    [stop] = world.events(fallback.STOP_EVENT)
    assert (stop["outcome"], stop["detail"]) == ("profile_not_approved", "over_run_cap")
    assert stop["to_profile"] == "gpu-b" and stop["started"] is False
    # Nothing moved, nothing skipped ahead to gpu-c, no new attempt.
    assert len(_attempts(world)) == 1
    assert _attempts(world)[0]["failure_class"] != class_fallback.PRESTART_NO_SPEND


def test_the_paired_comparison_sees_the_class_each_attempt_ran_on(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    _prestart_moved(world, 1)
    _to_next_route(world)
    first, second = _attempts(world)
    assert first["admitted_attempt_id"] and second["admitted_attempt_id"]
    with world.env.store.runner_connection() as connection:
        assert fallback.attempt_class(connection, first["admitted_attempt_id"]) == "gpu-main"
        assert fallback.attempt_class(connection, second["admitted_attempt_id"]) == "gpu-b"
        assert fallback.attempt_class(connection, "not-a-campaign-attempt") is None
        # A pair whose two arms ran on these two attempts crosses classes: refused.
        mixed = [
            (1, {"attempt_id": first["admitted_attempt_id"]}, {"attempt_id": "x"}),
        ]
        with pytest.raises(PairedRefused) as error:
            _pair_class(connection, mixed)
        assert error.value.code == "class_mismatch"


# -- paired arms stay on one class ----------


def test_paired_arms_stay_on_one_class(monkeypatch):
    classes = {"a1": "gpu-b", "b1": "gpu-b", "a2": "gpu-b", "b2": "gpu-b", "c1": "gpu-c"}
    monkeypatch.setattr(fallback, "attempt_class", lambda connection, attempt: classes.get(attempt))
    same = [
        (1, {"attempt_id": "a1"}, {"attempt_id": "b1"}),
        (2, {"attempt_id": "a2"}, {"attempt_id": "b2"}),
    ]
    assert _pair_class(None, same) == "gpu-b"
    crossed = [(1, {"attempt_id": "a1"}, {"attempt_id": "c1"})]
    with pytest.raises(PairedRefused) as error:
        _pair_class(None, crossed)
    assert error.value.code == "class_mismatch"
    # Each pair on one class but the comparison spans two: also refused.
    split = [
        (1, {"attempt_id": "a1"}, {"attempt_id": "b1"}),
        (2, {"attempt_id": "c1"}, {"attempt_id": "c1"}),
    ]
    with pytest.raises(PairedRefused):
        _pair_class(None, split)
    # Off Salad (no class known for either arm): unchanged, no class on the result.
    assert _pair_class(None, [(1, {"attempt_id": "n1"}, {"attempt_id": "n2"})]) is None


# -- placement: no class change after a possible start; heavy never silently local ----------


def test_the_instability_reorder_only_applies_before_any_possible_start():
    assert instability.may_reorder([])
    assert instability.may_reorder(
        [{"failure_class": "not_started"}, {"failure_class": "prestart_no_spend"}]
    )
    for failure in ("salad_not_passed", "infra_container_killed", None):
        assert not instability.may_reorder([{"failure_class": failure}])


def test_a_heavy_stage_reaches_local_on_instability_only_with_the_recorded_reason():
    class Guard:
        registered = True

        @staticmethod
        def is_remote(pid):
            return pid == "salad"

    class Base:
        candidate_ids = ("salad", "local")
        provider = "salad"

    record = {
        "outcome": instability.FALLBACK,
        "from_provider": "salad",
        "eligible": ["local"],
    }
    codes, detail = heavy_guard.reasons(
        Guard(), Base(), "local", [], None, None, (), instability=record
    )
    assert codes == ["allocation_unstable"]
    assert {"provider_id": "salad", "reason": "allocation_unstable"} in detail
    assert heavy_guard.explained(codes, detail)
    # Without the operator's instability record the same local placement is unexplained.
    codes, detail = heavy_guard.reasons(Guard(), Base(), "local", [], None, None, ())
    assert "allocation_unstable" not in codes and not heavy_guard.explained(codes, detail)
    # A record for another candidate explains nothing here.
    other = {**record, "eligible": ["elsewhere"]}
    codes, _ = heavy_guard.reasons(Guard(), Base(), "local", [], None, None, (), instability=other)
    assert "allocation_unstable" not in codes


def test_existing_specs_keep_their_digests(tmp_path):
    world = SaladWorld(tmp_path)
    spec = world.spec
    text = json.dumps(campaign_models.parse_spec(spec).model_dump(mode="json"))
    assert "alternate_profiles" not in text and "max_class_fallbacks" not in text
    explicit = json.loads(json.dumps(spec))
    explicit["stages"][0]["retry"]["max_class_fallbacks"] = 0
    assert campaign_models.spec_digest(campaign_models.parse_spec(spec)) == (
        campaign_models.spec_digest(campaign_models.parse_spec(explicit))
    )
