"""The route window covers provisioning plus the declared run; a deadline cut says so."""

from __future__ import annotations

import sqlite3

import pytest
from salad_campaign_fixture import (  # noqa: F401
    IMAGE,
    SaladWorld,
    contract_data,
    profile_data,
    salad_provider_reset,
)
from test_salad_guardian import case  # noqa: F401, F811

from tessera_control import campaign_providers as providers
from tessera_control.campaign_models import CampaignSpec
from tessera_control.campaign_provider_salad import SaladRouteProvider, register_provider_instance
from tessera_control.remote_execution_models import (
    SALAD_ALLOCATION_ALLOWANCE_SECONDS,
    SALAD_IMAGE_PULL_INPUT_ALLOWANCE_SECONDS,
    SALAD_PROVISIONING_ALLOWANCE_SECONDS,
    salad_route_window_refusal,
)
from tessera_control.salad_campaign_profiles import (
    WorkloadContractV1,
    derive_selection,
    route_window_source,
)
from tessera_control.store import Conflict


def _stage_with_run(world, seconds):
    stage = CampaignSpec.model_validate(world.spec).stages[0]
    workload = stage.salad.workload.model_copy(update={"workload_seconds": seconds})
    salad = stage.salad.model_copy(update={"workload": workload})
    return stage.model_copy(update={"max_duration_seconds": seconds, "salad": salad})


def _check(world, stage):
    register_provider_instance(world.profiles)
    db = sqlite3.connect(world.env.state / "tessera.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        providers.get_provider("salad").check_stage(db, project, stage)
    finally:
        db.close()


def test_allowance_is_named_and_justified():
    assert SALAD_PROVISIONING_ALLOWANCE_SECONDS == (
        SALAD_ALLOCATION_ALLOWANCE_SECONDS + SALAD_IMAGE_PULL_INPUT_ALLOWANCE_SECONDS
    )
    assert SALAD_PROVISIONING_ALLOWANCE_SECONDS != 3600


def test_run_that_leaves_no_room_for_provisioning_is_refused_naming_the_numbers(tmp_path):
    world = SaladWorld(tmp_path, route_max_seconds=3600)
    with pytest.raises(Conflict) as refused:
        _check(world, _stage_with_run(world, 3600))
    text = str(refused.value)
    assert "declared run 3600s" in text
    assert f"{SALAD_PROVISIONING_ALLOWANCE_SECONDS}s provisioning allowance" in text
    assert "3600s ceiling" in text


def test_a_longer_profile_ceiling_admits_the_same_run(tmp_path):
    world = SaladWorld(tmp_path, route_max_seconds=7200)
    _check(world, _stage_with_run(world, 3600))


def test_a_run_that_fits_under_the_ceiling_is_admitted_unchanged(tmp_path):
    world = SaladWorld(tmp_path, route_max_seconds=3600)
    fits = 3600 - SALAD_PROVISIONING_ALLOWANCE_SECONDS
    _check(world, _stage_with_run(world, fits))
    with pytest.raises(Conflict):
        _check(world, _stage_with_run(world, fits + 1))


def test_derive_selection_refuses_instead_of_shortening(tmp_path):
    world = SaladWorld(tmp_path, route_max_seconds=3600)
    profile = world.profiles.profiles[0]
    contract = WorkloadContractV1.model_validate(contract_data(workload_seconds=3600))
    with pytest.raises(ValueError, match="route window"):
        derive_selection(
            profile,
            contract,
            operation_id="op",
            epoch=1,
            snapshot_digest="a" * 64,
            image=IMAGE,
            max_cost_cents=10,
            plan_max_seconds=3600,
        )
    wide = profile.model_copy(update={"route_max_seconds": 7200})
    chosen = derive_selection(
        wide,
        contract,
        operation_id="op",
        epoch=1,
        snapshot_digest="a" * 64,
        image=IMAGE,
        max_cost_cents=10,
        plan_max_seconds=3600,
    )
    assert chosen.resources.max_seconds == 7200
    assert route_window_source(wide, 7200) == "profile_route_max_seconds"
    assert route_window_source(wide, 5000) == "plan_bound"


def test_refusal_text_is_none_when_it_fits():
    assert salad_route_window_refusal(100, 3600) is None


def _plan_of(world):
    from tessera_control.campaign_models import normalize, parse_spec_json
    from tessera_control.campaign_runner import _Ctx

    submitted = world.env.submit(spec=world.spec)
    row = world.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0]
    campaign = dict(row)
    spec = normalize(parse_spec_json(campaign["spec_json"]))
    ctx = _Ctx(campaign, spec, None, 0)
    stage = spec.stage("g1")
    placeholder = {
        "study_id": "s",
        "source_id": "s",
        "study_variant_id": "v",
        "split_lock_id": "l",
    }
    plan = world.driver._plan_scope(
        ctx,
        {"stage_key": "g1", "input_snapshot_digest": None, "variant_id": "v"},
        stage,
        {"operation_id": "operation-preview", "epoch": 1},
        {"basis_digest": "0" * 64, "reviews": []},
        placeholder,
        preview=True,
    )
    return plan, world.driver.preview_review_digests(ctx, "g1")


def test_plan_and_digest_show_window_allowance_and_source(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir(mode=0o700)
    # The provider registry is process-wide: build and read each world in turn.
    plan, digests = _plan_of(SaladWorld(tmp_path / "a", route_max_seconds=3600))
    resources = plan["brief"]["compute"]["resources"]
    assert "route window 3600s" in resources
    assert f"provisioning allowance {SALAD_PROVISIONING_ALLOWANCE_SECONDS}s" in resources
    assert "profile route_max_seconds 3600" in resources
    # The window is part of what the reviewer digests: another window, another plan digest.
    assert (
        _plan_of(SaladWorld(tmp_path / "b", route_max_seconds=7200))[1]["plan_digest"]
        != digests["plan_digest"]
    )


DEADLINE_FINAL = dict(
    status="stopped",
    reason="deadline",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
    observed_at="2026-11-19T00:00:00Z",
    route_deadline={"running_seconds": 2400, "limit": "route_deadline"},
)


def test_a_deadline_cut_after_an_instance_was_seen_has_its_own_class(tmp_path):
    from test_campaign_salad_settlement import run

    world = SaladWorld(tmp_path)
    run(world, DEADLINE_FINAL, passed=False)
    world.tick_until(lambda: world.attempt()["status"].startswith("failed"))
    attempt = world.attempt()
    assert attempt["failure_class"] == "route_deadline_reached"
    assert attempt["status"] == "failed_permanent"  # retry policy is decided elsewhere
    assert "2400s running" in attempt["failure_detail"]


def test_without_deadline_evidence_the_label_stays_not_passed(tmp_path):
    provider = SaladRouteProvider(SaladWorld(tmp_path).profiles)
    base = {"salad_evidence_digest": "e" * 64, "manifest_id": "m1", "salad_verdict": "failed"}
    forged = provider.observe(None, {**base, "failure_detail": "live receipt not passed: deadline"})
    assert forged.failure_class == "salad_not_passed"


def test_guardian_status_reports_running_seconds_only_after_an_instance(case):  # noqa: F811
    from test_salad_guardian import _advance, _running_window_assignment

    g, _, _, clock, state, stops, _, _ = case
    a = _running_window_assignment(case, "op-cut", running_seconds=40)
    state[0] = "running"
    clock[0] += 5
    g.heartbeat(a.binding.operation)
    g.tick(a.binding.operation)
    status = _advance(g, a, clock, 60)
    assert (status["reason"], status["stop_cause"]) == ("deadline", "deadline")
    assert 35 <= status["route_deadline"]["running_seconds"] <= 50
    # running_seconds 40 << deadline 1000: the declared-run cap fired, not the route window.
    assert status["route_deadline"]["limit"] == "running_cap"
    assert "allocation_timeout" not in status

    never = _running_window_assignment(case, "op-never", running_seconds=40, deadline=160)
    state[0] = "pending"
    status = _advance(g, never, clock, 200)
    assert status["stop_cause"] == "deadline" and "route_deadline" not in status


def test_a_declared_run_cap_overrun_is_not_labelled_a_route_deadline(case):  # noqa: F811
    from test_salad_guardian import _advance, _running_window_assignment

    g, _, _, clock, state, stops, _, _ = case
    # Route deadline 140 s away, run cap 500 s: the route window runs out first.
    a = _running_window_assignment(case, "op-route", running_seconds=500, deadline=140)
    state[0] = "running"
    status = _advance(g, a, clock, 200)
    assert status["stop_cause"] == "deadline"
    assert status["route_deadline"]["limit"] == "route_deadline"


def test_running_cap_final_keeps_not_passed_and_route_final_gets_the_new_class(tmp_path):
    provider = SaladRouteProvider(SaladWorld(tmp_path).profiles)
    assert provider._route_deadline(None, {"id": None}) is None


def test_running_cap_deadline_final_stays_salad_not_passed(tmp_path):
    from test_campaign_salad_settlement import run

    world = SaladWorld(tmp_path)
    final = dict(DEADLINE_FINAL, route_deadline={"running_seconds": 20, "limit": "running_cap"})
    run(world, final, passed=False)
    world.tick_until(lambda: world.attempt()["status"].startswith("failed"))
    assert world.attempt()["failure_class"] == "salad_not_passed"
