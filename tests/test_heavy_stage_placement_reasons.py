# ruff: noqa: F811
"""Heavy-stage placement: ordering agrees with the guard, and local fallbacks carry an accurate
reason (ADR 0107 amendment 2026-11-23). Fakes only: no provider call, no network, no money."""

from __future__ import annotations

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_campaign_placement_default import (  # noqa: F401
    GIB,
    _cpu_world,
    _normal,
    local_providers,
)
from test_campaign_placement_salad import NEAR, _placed, _stage, near  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_placement as placement
from tessera_control import campaign_placement_preference as preference
from tessera_control.campaign_provider_salad import SaladRouteProvider, register_provider_instance

HEAVY = {"placement_tags": ["heavy"]}


@pytest.fixture(autouse=True)
def _no_preference():
    preference.configure(None, ())
    yield
    preference.configure(None, ())


def _events(world):
    return world.events(guard.EVENT)


# -- 2. ordering uses the guard's signals --------------------------------------------------------


def test_ordering_counts_the_guards_extra_signals(local_providers):
    stage = _normal()
    assert preference.preferred_order(stage)[0][0] == NEAR  # light: the spec's order
    order, record = preference.preferred_order(stage, ["large_inputs"])
    assert order[0] == "salad"
    assert record["class"] == "heavy" and record["signals"] == ["large_inputs"]
    # A signal never moves a light stage when the guard names none.
    assert preference.preferred_order(stage, [])[0][0] == NEAR


def test_a_stage_heavy_only_by_large_inputs_is_placed_on_salad_first(tmp_path, near, monkeypatch):
    monkeypatch.setattr(guard, "input_bytes", lambda connection, campaign, stage: 4 * GIB)
    world = _cpu_world(tmp_path, near, {})
    world.start()
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and placed["candidates"] == ["salad", NEAR]
    assert placed["preference"]["class"] == "heavy"
    assert placed["preference"]["signals"] == ["large_inputs"]
    assert "heavy_guard" not in placed


# -- 3. a real credential refusal ----------------------------------------------------------------


def test_a_real_credential_refusal_is_a_credential_reason(tmp_path, near, monkeypatch):
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    expired = {"state": "expired", "reference": "ref-1", "profile": "cpu-main", "expires_at": 1}
    monkeypatch.setattr(SaladRouteProvider, "credential_state", lambda s, p, now=None: expired)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    assert placed["skipped"][0]["reason"] == "credential_unavailable"  # from check_stage itself
    assert placed["heavy_guard"]["reasons"] == ["credential_unavailable"]
    assert placed["heavy_guard"]["detail"] == [
        {"provider_id": "salad", "reason": "credential_unavailable"}
    ]


# -- 4. a Salad infrastructure failure -----------------------------------------------------------


def test_a_local_retry_after_a_salad_infrastructure_failure_says_so(tmp_path, near, monkeypatch):
    real = placement.decide
    failed = [{"provider_id": "salad", "failure_class": "infra_container_killed"}]

    def after_failure(*args, **kwargs):
        return real(*args, **{**kwargs, "attempts": failed})

    monkeypatch.setattr(placement, "decide", after_failure)
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["candidates"] == [NEAR, "salad"] and placed["provider_id"] == NEAR
    block = placed["heavy_guard"]
    assert block["reasons"] == ["remote_failed_infra"]
    assert block["detail"] == [{"provider_id": "salad", "reason": "infra_container_killed"}]
    (event,) = _events(world)
    assert event["reasons"] == ["remote_failed_infra"]


# -- 5. no Salad profile registered --------------------------------------------------------------


def test_without_a_salad_profile_warn_mode_records_why(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    register_provider_instance(None)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    assert placed["heavy_guard"]["reasons"] == ["no_remote_profile"]
    assert placed["heavy_guard"]["mode"] == "warn"
    (event,) = _events(world)
    assert event["reasons"] == ["no_remote_profile"] and event["provider_id"] == NEAR


def test_without_a_salad_profile_refuse_mode_still_places_locally(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    preference.configure({"heavy_local_fallback": "refuse"}, [NEAR, "salad"])
    world.start()
    register_provider_instance(None)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    # Refuse mode never waits for capacity that cannot exist, but records what warn records.
    assert placed["heavy_guard"]["reasons"] == ["no_remote_profile"]
    assert placed["heavy_guard"]["mode"] == "warn"
    (event,) = _events(world)
    assert event["reasons"] == ["no_remote_profile"] and event["provider_id"] == NEAR
    assert not world.events(placement.WAITING_EVENT)


def test_a_light_stage_without_a_salad_profile_records_nothing(tmp_path, near):
    world = _cpu_world(tmp_path, near, {"max_duration_seconds": 60})
    world.start()
    register_provider_instance(None)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert "heavy_guard" not in placed and not _events(world)


# -- 6. a gpu requirement is a guard signal ------------------------------------------------------


def test_a_gpu_tag_is_a_guard_signal(local_providers):
    assert guard.heavy(_normal(placement_tags=["gpu"])) == (True, ["tag:gpu"])


def test_a_gpu_stage_placed_locally_while_salad_is_registered_warns(tmp_path, near):
    world = _cpu_world(tmp_path, near, {"placement_tags": ["gpu"]})
    # Amendment 2026-11-23b: the local fallback is ``remote_not_tried`` (unexplained); it is
    # placed and warned only with the operator's opt-out (by default Salad is tried first).
    preference.configure({"heavy_unexplained_local": "warn"}, [NEAR, "salad"])
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    assert placed["heavy_guard"]["signals"] == ["tag:gpu"]
    assert placed["heavy_guard"]["reasons"] == ["remote_not_tried"]
    assert placed["heavy_guard"]["unexplained_local"] == "warn"
    assert len(_events(world)) == 1
