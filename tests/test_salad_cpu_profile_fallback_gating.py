"""Which failures the profile fallback rules read once an operator chains CPU fallbacks.

An operator ``fallback_profiles`` chain is operator config: it must not change how an approved
spec's failures are handled unless the stage's approved ``retry.max_class_fallbacks`` lets it
use a fallback. A single-profile stage with a zero bound behaves exactly as with no chain.

Real stores and runner; the Salad executor, guardian and human are the doubles of the Salad
campaign fixture (no provider call, no network, no money). An unstable run is the route state
the control plane records for a reallocation. Nothing here proves provider behavior.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    profile_data,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_campaign_class_fallback import _attempts
from test_salad_cpu_profile_fallback_runner import RATE, _cpu, _fail_prestart
from test_salad_profile_fallback import _ran_unstable

from tessera_control import campaign_class_fallback as class_fallback
from tessera_control import campaign_salad_fallback as fallback
from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles


def _world(tmp_path, *, chain=(), fallbacks=0, alternates=()) -> SaladWorld:
    """A CPU stage on ``gpu-main`` (the fixture's name) listing ``alternates``; the operator
    file chains ``chain`` after ``gpu-main``; ``fallbacks`` is the stage's approved bound."""
    tmp_path.mkdir(mode=0o700, exist_ok=True)
    world = SaladWorld(tmp_path, gpu_class=None, cpu_rate=RATE)
    env = world.env
    main = {"fallback_profiles": list(chain)} if chain else {}
    documents = [_cpu(env, "gpu-main", **main)] + [_cpu(env, n) for n in ("cpu-b", "cpu-c")]
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(schema="tessera.salad.campaign-profiles.v1", profiles=documents)
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    stage = salad_stage(env, "g1")
    stage["salad"] = {**stage["salad"], "gpu": False}
    if alternates:
        stage["salad"]["alternate_profiles"] = list(alternates)
    stage["retry"] = {**stage["retry"], "max_class_fallbacks": fallbacks}
    world.spec = salad_spec(env, [stage])
    return world


def _approved(world):
    row = world.row("SELECT spec_json FROM campaigns WHERE id=?", world.campaign_id)
    return normalize(parse_spec_json(row["spec_json"])).stage("g1")


def _unstable_after_start(world, *, terminal: str) -> None:
    """Attempt 1 (route started) is reallocated (a possible start), stops and is charged."""
    _ran_unstable(world, within=True)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(lambda: world.status() == terminal)


def _outcome(world) -> dict:
    stage = world.row("SELECT status FROM campaign_stages WHERE campaign_id=?", world.campaign_id)
    attempt = world.attempt()
    return {
        "campaign": world.status(),
        "stage": stage["status"],
        "attempt": attempt["attempt"],
        "attempt_status": attempt["status"],
        "failure_class": attempt["failure_class"],
        "settled": attempt["settled_cents"],
        "switches": world.events(fallback.SWITCH_EVENT),
        "stops": world.events(fallback.STOP_EVENT),
    }


# (a) a stage that can never use a fallback: the chain changes nothing after a start


def test_a_zero_bound_single_profile_stage_fails_exactly_as_without_a_chain(tmp_path):
    plain = _world(tmp_path / "plain")
    plain.to_route_started()
    _unstable_after_start(plain, terminal="failed")
    expected = _outcome(plain)

    chained = _world(tmp_path / "chained", chain=("cpu-b", "cpu-c"))
    chained.to_route_started()
    approved = _approved(chained)
    assert fallback.operator_order(approved) == ("cpu-b", "cpu-c")
    assert not fallback.reads_fallback(approved)
    _unstable_after_start(chained, terminal="failed")
    found = _outcome(chained)

    assert found == expected
    # The provider's own confirmed move is infrastructure; no infra retry is enabled here.
    assert found["failure_class"] == "infra_salad_reallocated" and found["attempt"] == 1
    assert found["switches"] == [] and found["stops"] == []
    assert all(
        e["reason"] != fallback.POST_START_REFUSED for e in chained.events("campaign.blocked")
    )
    chained.tick(3)
    assert chained.events(fallback.STOP_EVENT) == [] and len(_attempts(chained)) == 1


# (b) a stage that could use an operator fallback: the strict rule holds it after a start


def test_a_bounded_stage_with_a_chain_is_held_after_a_possible_start(tmp_path):
    world = _world(tmp_path, chain=("cpu-b", "cpu-c"), fallbacks=1)
    world.to_route_started()
    assert fallback.reads_fallback(_approved(world))
    _unstable_after_start(world, terminal="blocked")
    assert world.events(fallback.SWITCH_EVENT) == []
    [stop] = world.events(fallback.STOP_EVENT)
    assert stop["outcome"] == fallback.POST_START_REFUSED and stop["attempt"] == 1
    assert stop["started"] is True and stop["to_profile"] is None
    assert world.events("campaign.blocked")[-1]["reason"] == fallback.POST_START_REFUSED
    assert len(_attempts(world)) == 1


# (c) spec alternates: held after a start, whatever the operator file says


@pytest.mark.parametrize("chain", [(), ("cpu-c",)])
# Submit refuses spec alternates with a zero bound, so the bound here is at least 1.
@pytest.mark.parametrize("fallbacks", [1, 2])
def test_spec_alternates_are_held_after_a_possible_start_regardless_of_a_chain(
    tmp_path, chain, fallbacks
):
    world = _world(tmp_path, chain=chain, fallbacks=fallbacks, alternates=("cpu-b",))
    world.to_route_started()
    assert fallback.reads_fallback(_approved(world))
    _unstable_after_start(world, terminal="blocked")
    assert world.events(fallback.SWITCH_EVENT) == []
    [stop] = world.events(fallback.STOP_EVENT)
    assert stop["outcome"] == fallback.POST_START_REFUSED
    assert stop["order"] == ["gpu-main", "cpu-b"] and stop["tried"] == ["gpu-main"]


# (d) the pre-start move: bounded by max_class_fallbacks as before


def test_a_zero_bound_prestart_failure_never_moves_with_or_without_a_chain(tmp_path):
    for name, chain in (("plain", ()), ("chained", ("cpu-b", "cpu-c"))):
        world = _world(tmp_path / name, chain=chain)
        world.to_route_started()
        _fail_prestart(world)
        world.tick(5)
        assert world.events(fallback.SWITCH_EVENT) == []
        assert world.events(fallback.STOP_EVENT) == []
        assert world.events(class_fallback.EVENT) == []


def test_a_bounded_prestart_failure_still_moves_to_the_operator_fallback(tmp_path):
    world = _world(tmp_path, chain=("cpu-b", "cpu-c"), fallbacks=1)
    world.to_route_started()
    _fail_prestart(world)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["source"] == fallback.SOURCE_OPERATOR and switch["to_profile"] == "cpu-b"
