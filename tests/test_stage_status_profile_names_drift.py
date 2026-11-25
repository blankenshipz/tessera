"""The stage-status read and the fallback module name the same events and outcomes.

A rename in ``campaign_salad_fallback`` must not leave the stage-status read silently showing
no switches. The second test runs a real pre-start switch, then reads ``stage_status``.
"""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_class_fallback import _attempts
from test_salad_profile_strict_rule import _campaign, _prestart_moved, _world

from tessera_control import agent_stage_status as status
from tessera_control import campaign_salad_fallback as fallback
from tessera_control.agent_stage_status import stage_status


def test_stage_status_names_equal_the_fallback_modules():
    assert status.SWITCH_EVENT == fallback.SWITCH_EVENT == "campaign.salad_profile_switched"
    assert status.STOP_EVENT == fallback.STOP_EVENT == "campaign.salad_profile_fallback_stopped"
    assert status._PROFILE_OUTCOMES == fallback.OUTCOMES


def test_a_real_switch_shows_in_stage_status(tmp_path):
    world: SaladWorld = _world(tmp_path)
    world.to_route_started()
    _prestart_moved(world, 1)
    first, _second = _attempts(world)
    [recorded] = world.events(fallback.SWITCH_EVENT)
    with world.env.store.runner_connection() as connection:
        view = stage_status(connection, _campaign(world))["stages"][0]["salad_profiles"]
    [shown] = view["switches"]
    assert (shown["from_profile"], shown["to_profile"]) == ("gpu-main", "gpu-b")
    assert (shown["from_attempt"], shown["to_attempt"]) == (1, 2)
    assert shown["reason"] == recorded["reason"] == fallback.REASON_PRESTART_NO_SPEND
    assert shown["started"] is False and shown["spent"] is False
    assert view["latest_attempt_profile"] == "gpu-b" and first["attempt"] == 1
