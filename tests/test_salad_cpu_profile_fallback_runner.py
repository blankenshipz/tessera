"""Operator CPU capacity fallback through the real runner (no spec change, strict rule).

Real stores and runner; the Salad executor, guardian and human are the doubles of the Salad
campaign fixture (no provider call, no network, no money). A pre-start failure is the rows the
executor writes for one (a route with a ``create`` effect and no ``start``, a confirmed stop, a
live receipt naming the step that failed). Nothing here proves provider behavior.
"""

from __future__ import annotations

from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    profile_data,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_campaign_class_fallback import (
    _attempts,
    _prestart_failure,
    _stop_reason,
    _to_next_route,
)

from tessera_control import campaign_class_fallback as class_fallback
from tessera_control import campaign_salad_fallback as fallback
from tessera_control.agent_stage_status import stage_status
from tessera_control.campaign_models import normalize, parse_spec_json, stage_provider
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_receipts import build_receipt
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

RATE = dict(vcpu_hour_usd="0.04", memory_gb_hour_usd="0.01")
# Far above the stage's 20-cent hold over its 3000 s duration.
COSTLY = dict(vcpu_hour_usd="9.99", memory_gb_hour_usd="9.99")
COUNTRY = {"gpu-main": "US", "cpu-b": "CA", "cpu-c": "DE"}


def _cpu(env, name, **over):
    base = dict(
        name=name,
        gpu_class=None,
        cpu_rate=RATE,
        country=COUNTRY[name],
        capability_environment_id="salad-gpu-campaign",
        max_task_cents=200,
    )
    return profile_data(env, **(base | over))


def _world(tmp_path, chain=("cpu-b", "cpu-c"), *, fallbacks=2, over=None) -> SaladWorld:
    """A CPU stage on ``gpu-main`` (a CPU profile; the fixture's name) with no spec alternates;
    the operator file chains ``chain`` after it."""
    over = over or {}
    world = SaladWorld(tmp_path, gpu_class=None, cpu_rate=RATE)
    env = world.env
    main = {"fallback_profiles": list(chain)} if chain else {}
    documents = [_cpu(env, "gpu-main", **main, **over.get("gpu-main", {}))] + [
        _cpu(env, name, **over.get(name, {})) for name in ("cpu-b", "cpu-c")
    ]
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(schema="tessera.salad.campaign-profiles.v1", profiles=documents)
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    stage = salad_stage(env, "g1")
    stage["salad"] = {**stage["salad"], "gpu": False}
    if fallbacks:
        stage["retry"] = {**stage["retry"], "max_class_fallbacks": fallbacks}
    world.spec = salad_spec(env, [stage])
    return world


def _country(world) -> str:
    return world.route_state()["proposal"]["selection"]["salad"]["country"]


def _fail_prestart(world, **kw) -> None:
    _prestart_failure(world, **kw)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)


def _campaign(world) -> dict:
    return dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))


def _receipt(world) -> dict:
    with world.env.store.runner_connection() as connection:
        receipt, _ = build_receipt(connection, _campaign(world), "running")
    return receipt


def _spec_row(world):
    row = _campaign(world)
    return row["spec_json"], row.get("spec_digest")


# -- the move ----------


def test_a_no_spend_prestart_failure_moves_to_the_operator_fallback(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    approved = _spec_row(world)
    assert _country(world) == "US"
    _fail_prestart(world, code=None, rejected=True)  # refused for capacity at create
    world.tick_until(lambda: len(_attempts(world)) == 2)
    first, _second = _attempts(world)
    assert first["failure_class"] == class_fallback.PRESTART_NO_SPEND
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["source"] == fallback.SOURCE_OPERATOR
    assert (switch["from_profile"], switch["to_profile"]) == ("gpu-main", "cpu-b")
    assert switch["operator_order"] == ["cpu-b", "cpu-c"] and switch["skipped"] == []
    assert switch["order"] == ["gpu-main"]  # the approved order is unchanged
    assert switch["started"] is False and switch["spent"] is False
    assert switch["settled_cents"] == 0 and switch["prestart_reason"] == "create_rejected"
    _to_next_route(world)
    assert _country(world) == "CA"
    # The approved spec (and its digest) never changed.
    assert _spec_row(world) == approved

    entry = _receipt(world)["stages"][0]
    assert [a["salad_profile"] for a in entry["attempts"]] == ["gpu-main", "cpu-b"]
    [recorded] = entry["salad_profiles"]["switches"]
    assert recorded["source"] == "operator_fallback" and recorded["to_profile"] == "cpu-b"
    with world.env.store.runner_connection() as connection:
        view = stage_status(connection, _campaign(world))["stages"][0]["salad_profiles"]
    assert view["switches"][0]["source"] == "operator_fallback"
    assert view["profiles"] == ["gpu-main", "cpu-b"]
    assert view["latest_attempt_profile"] == "cpu-b"


def test_a_fallback_over_the_approved_hold_is_skipped_and_the_next_one_used(tmp_path):
    world = _world(tmp_path, over={"cpu-b": {"cpu_rate": COSTLY}})
    world.to_route_started()
    _fail_prestart(world)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["to_profile"] == "cpu-c" and switch["source"] == fallback.SOURCE_OPERATOR
    assert switch["skipped"] == [{"profile": "cpu-b", "detail": "fallback_over_hold"}]
    _to_next_route(world)
    assert _country(world) == "DE"


def test_no_covered_fallback_holds_the_stage_with_a_closed_reason_and_no_spend(tmp_path):
    world = _world(
        tmp_path,
        over={"cpu-b": {"cpu_rate": COSTLY}, "cpu-c": {"max_run_cents": 5, "max_task_cents": 5}},
    )
    world.to_route_started()
    _fail_prestart(world)
    world.tick_until(lambda: world.status() == "blocked")
    assert world.events(fallback.SWITCH_EVENT) == []
    [stop] = world.events(fallback.STOP_EVENT)
    assert stop["outcome"] == fallback.PROFILE_NOT_APPROVED
    assert stop["detail"] == "fallback_over_hold" and stop["to_profile"] == "cpu-b"
    assert stop["skipped"] == [
        {"profile": "cpu-b", "detail": "fallback_over_hold"},
        {"profile": "cpu-c", "detail": "over_run_cap"},
    ]
    assert stop["started"] is False and stop["spent"] is False
    world.tick(3)
    assert len(_attempts(world)) == 1  # nothing new held or run


def test_a_possible_start_never_moves(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    # The route carries a start effect: it may have started, whatever the receipt says.
    _fail_prestart(world, effects=("create", "start"))
    world.tick(5)
    assert world.events(fallback.SWITCH_EVENT) == []
    assert all(a["failure_class"] != class_fallback.PRESTART_NO_SPEND for a in _attempts(world))


def test_a_nonzero_settlement_never_moves(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(3)
    world.tick(5)
    assert world.events(fallback.SWITCH_EVENT) == []
    assert all(a["failure_class"] != class_fallback.PRESTART_NO_SPEND for a in _attempts(world))


# -- order, exhaustion and the bound ----------


def test_the_chain_is_tried_in_order_then_fails_closed_when_exhausted(tmp_path):
    world = _world(tmp_path, fallbacks=3)
    world.to_route_started()
    _fail_prestart(world)
    _to_next_route(world)
    assert _country(world) == "CA"
    _fail_prestart(world)
    _to_next_route(world)
    assert _country(world) == "DE"
    _fail_prestart(world)
    world.tick_until(lambda: world.status() == "failed")
    moves = [(s["from_profile"], s["to_profile"]) for s in world.events(fallback.SWITCH_EVENT)]
    assert moves == [("gpu-main", "cpu-b"), ("cpu-b", "cpu-c")]
    assert len(_attempts(world)) == 3
    assert _stop_reason(world) == f"stage_failed:g1:{class_fallback.EXHAUSTED}"


def test_operator_moves_count_against_max_class_fallbacks(tmp_path):
    world = _world(tmp_path, fallbacks=1)
    world.to_route_started()
    _fail_prestart(world)
    _to_next_route(world)
    _fail_prestart(world)
    world.tick_until(lambda: world.status() == "failed")
    assert [s["to_profile"] for s in world.events(fallback.SWITCH_EVENT)] == ["cpu-b"]
    assert len(_attempts(world)) == 2
    assert _stop_reason(world) == f"stage_failed:g1:{class_fallback.EXHAUSTED}"


def test_without_a_class_fallback_bound_the_chain_is_never_used(tmp_path):
    world = _world(tmp_path, fallbacks=0)
    world.to_route_started()
    _fail_prestart(world)
    world.tick(5)
    assert world.events(fallback.SWITCH_EVENT) == []
    assert world.events(class_fallback.EVENT) == []


def test_without_fallback_profiles_nothing_changes(tmp_path):
    world = _world(tmp_path, chain=())
    world.to_route_started()
    approved = normalize(parse_spec_json(_campaign(world)["spec_json"])).stage("g1")
    assert stage_provider("salad").fallback_classes(approved) == 1
    assert fallback.operator_order(approved) == () and not fallback.has_fallbacks(approved)
    entry = _receipt(world)["stages"][0]
    assert "salad_profiles" not in entry or entry["salad_profiles"] is None
    assert all("salad_profile" not in a for a in entry["attempts"])
