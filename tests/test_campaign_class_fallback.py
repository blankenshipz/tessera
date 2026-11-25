"""Next-class fallback after a pre-start failure that spent nothing (ADR 0060 addendum).

Real stores and runner; the Salad executor, guardian and human are test doubles exactly as in
the Salad runner tests (no provider call, no network, no money). A pre-start failure is the
rows the executor writes for one: a route that recorded a ``create`` effect but no ``start``,
the provider's confirmed stop and a live receipt naming the step that failed.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    GPU,
    SaladWorld,
    profile_data,
    salad_provider_reset,
    salad_spec,
    salad_stage,
    salad_stage_v2,
)

from tessera_control import campaign_class_fallback as class_fallback
from tessera_control import campaign_placement as placement
from tessera_control.campaign_models import (
    RetryPolicy,
    SaladStageSpec,
    parse_spec,
    spec_digest,
)
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles
from tessera_control.salad_live_check import PRESTART_FAILURE_CLASSES

ALT_GPU = "650e8400-e29b-41d4-a716-446655440000"


def _world(tmp_path, *, alternates=("gpu-alt",), retry=None, **alt_over):
    world = SaladWorld(tmp_path)
    env = world.env
    documents = [profile_data(env)]
    for index, name in enumerate(alternates):
        documents.append(
            profile_data(env, name=name, gpu_class=ALT_GPU if index == 0 else GPU, **alt_over)
        )
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(schema="tessera.salad.campaign-profiles.v1", profiles=documents)
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    stage = salad_stage(env, "g1")
    if alternates:
        stage["salad"] = {**stage["salad"], "alternate_profiles": list(alternates)}
    stage["retry"] = {**stage["retry"], "max_class_fallbacks": 2, **(retry or {})}
    world.spec = salad_spec(env, [stage])
    return world


def _route_effects(world, *actions: str) -> None:
    state = world.route_state()
    state["effects"] = [
        {"id": f"e{i}", "action": action, "requested_at": "2026-01-01T00:00:00Z", "epoch": 1}
        for i, action in enumerate(actions)
    ]
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])


def _prestart_failure(
    world,
    *,
    code: str | None = "group_unsettled",
    rejected: bool = False,
    effects=("create",),
    confirmed: bool = True,
) -> None:
    """Executor double for a route that failed before start (``confirmed``: provider stop)."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    _route_effects(world, *effects)
    if confirmed:
        world.terminate_route()
        world.observe_exit()
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "status": "failed",
        "reason": "execution_or_output_unresolved",
        "stop_confirmed": confirmed,
        "prestart_failure": None if code is None else {"class": code, "exception": "Conflict"},
        "create_rejection": {"code": "capacity"} if rejected else None,
    }
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(route_id=row["route_id"], live_receipt=receipt),
    )


def _attempts(world):
    return world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? ORDER BY attempt",
        world.campaign_id,
    )


def _to_next_route(world) -> None:
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")


def _receipt(world):
    sql = "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?"
    row = world.row(sql, world.campaign_id)
    return json.loads(row["receipt_json"])


def _stop_reason(world):
    return world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)[
        "stop_reason"
    ]


# -- the runner path --------------------------------------------------------------------------


def test_a_prestart_failure_with_no_spend_moves_to_the_next_class_which_runs(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    assert world.route_state()["proposal"]["selection"]["resources"]["gpu_class"] == GPU
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    first, second = _attempts(world)
    assert first["status"] == "failed_transient" and first["settled_cents"] == 0
    assert first["failure_class"] == class_fallback.PRESTART_NO_SPEND
    assert first["failure_detail"] == "group_unsettled"
    (event,) = world.events(class_fallback.EVENT)
    assert event["reason"] == "group_unsettled" and event["path"] == "failed"
    assert event["class_index"] == 0 and event["provider_id"] == "salad"
    assert event["class_fallbacks_used"] == 1 and event["class_fallbacks_max"] == 2
    # max_attempts is 1: the move used no scientific retry.
    assert second["attempt"] == 2 and second["hold_cents"] == first["hold_cents"]
    (placed,) = world.events(placement.PLACED_EVENT)
    assert placed["attempt"] == 2 and placed["class_index"] == 1
    _to_next_route(world)
    # The second attempt's route runs on the alternate profile's GPU class.
    assert world.route_state()["proposal"]["selection"]["resources"]["gpu_class"] == ALT_GPU
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    world.record_charge(9)
    world.tick_until(lambda: world.status() == "succeeded")
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["settled_cents"] == 9 and campaign["held_cents"] == 0
    entries = _receipt(world)["stages"][0]["attempts"]
    assert entries[0]["failure_class"] == "prestart_no_spend"
    assert entries[0]["class_fallback"] == {
        "reason": "group_unsettled",
        "class_index": 0,
        "path": "failed",
        "is_evidence": False,
    }
    assert entries[1]["placement"]["class_index"] == 1 and entries[1]["status"] == "succeeded"
    # Closed codes only: nothing host-, path- or text-shaped reaches the fallback record.
    text = json.dumps(entries[0]["class_fallback"]) + json.dumps(event)
    assert "/" not in text and "example" not in text


def test_a_create_rejection_is_a_prestart_reason(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    _prestart_failure(world, code=None, rejected=True)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    assert _attempts(world)[0]["failure_detail"] == "create_rejected"


def test_an_exhausted_class_list_fails_closed_with_the_code(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    _to_next_route(world)
    _prestart_failure(world, code="input_delivery_unconfirmed")
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: world.status() == "failed")
    assert len(_attempts(world)) == 2
    assert _stop_reason(world) == f"stage_failed:g1:{class_fallback.EXHAUSTED}"
    # Never another destination: no grant went to any other worker.
    workers = {
        row["worker_principal_id"] for row in world.env.query("SELECT * FROM execution_grants")
    }
    assert workers == {world.worker["id"]}


def test_the_fallback_bound_fails_closed_before_the_list_ends(tmp_path):
    world = _world(tmp_path, alternates=("gpu-alt", "gpu-third"), retry={"max_class_fallbacks": 1})
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    _to_next_route(world)
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: world.status() == "failed")
    assert len(_attempts(world)) == 2
    assert _stop_reason(world) == f"stage_failed:g1:{class_fallback.EXHAUSTED}"


@pytest.mark.parametrize("case", ["start_effect", "spent", "unclassified_receipt"])
def test_no_fallback_after_a_possible_start_or_any_spend(tmp_path, case):
    world = _world(tmp_path)
    world.to_route_started()
    if case == "start_effect":
        _prestart_failure(world, effects=("create", "start"))
    elif case == "unclassified_receipt":
        _prestart_failure(world, code=None)
    else:
        _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(3 if case == "spent" else 0)
    world.tick_until(lambda: world.status() == "failed")
    (attempt,) = _attempts(world)
    assert attempt["status"] == "failed_permanent"
    assert attempt["failure_class"] == "salad_not_passed"
    assert world.events(class_fallback.EVENT) == []


def test_a_stage_without_alternates_is_unchanged(tmp_path):
    world = _world(tmp_path, alternates=())
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: world.status() == "failed")
    (attempt,) = _attempts(world)
    assert attempt["failure_class"] == "salad_not_passed"
    assert world.events(class_fallback.EVENT) == []
    assert world.events(placement.PLACED_EVENT) == []


def test_max_class_fallbacks_zero_with_alternates_is_refused_at_submit(tmp_path):
    # ADR 0169: alternates are reached only by pre-start moves, so a bound of 0 could never
    # reach them; submit refuses the stage instead of approving unusable profiles.
    from tessera_control.store import Conflict

    world = _world(tmp_path, retry={"max_class_fallbacks": 0})
    with pytest.raises(Conflict, match=r"retry\.max_class_fallbacks is 0"):
        world.env.submit(spec=world.spec)


def test_an_unknown_attempt_reconciled_at_zero_moves_on_and_restarts_safely(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    # The provider's stop was never confirmed: the attempt is unknown and the campaign blocked.
    _prestart_failure(world, confirmed=False)
    world.tick_until(lambda: world.status() == "blocked")
    assert world.attempt()["status"] == "unknown" and world.attempt()["settled_cents"] is None
    # A person reconciles the grant (the same double the Salad runner tests use).
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled', settlement_cents=0 WHERE id=?",
        world.attempt()["grant_id"],
    )
    world.tick(2)
    # Claimed: a zero reconcile alone never settles; a person's zero charge does.
    assert world.attempt()["settled_cents"] is None
    world.record_charge(0)
    world.tick_until(lambda: world.attempt()["failure_class"] == "prestart_no_spend")
    (event,) = world.events(class_fallback.EVENT)
    assert event["path"] == "reconciled_unknown"
    # A new runner (a restart) continues from the database: one event, one next attempt.
    world.env.execute("UPDATE campaign_runner_lease SET expires_at='2000-01-01T00:00:00Z'")
    world.runner = CampaignRunner(world.env.state)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    world.tick(3)
    assert len(world.events(class_fallback.EVENT)) == 1 and len(_attempts(world)) == 2
    assert world.status() == "running" and _stop_reason(world) is None
    first = _attempts(world)[0]
    assert first["settled_cents"] == 0 and first["status"] == "failed_transient"


def test_every_class_is_checked_at_submit_and_again_at_placement(tmp_path, monkeypatch):
    from tessera_control.campaign_provider_salad import SaladRouteProvider
    from tessera_control.store import Conflict

    world = _world(tmp_path, max_run_cents=5)
    # The alternate profile's per-run ceiling is below the hold: submit refuses the stage.
    with pytest.raises(Conflict, match="per-run ceiling"):
        world.env.submit(spec=world.spec)

    (tmp_path / "again").mkdir()
    world = _world(tmp_path / "again")
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    # After approval the alternate profile stops qualifying (its check refuses): placement
    # re-checks the next class, refuses it, and the stage fails instead of running unchecked.
    real = SaladRouteProvider.check_stage
    checked: list[str] = []

    def check(self, connection, project_id, stage):
        profile = (getattr(stage, "provider_config", None) or {}).get("profile")
        checked.append(profile)
        if profile == "gpu-alt":
            raise Conflict("Salad stage names an unknown operator profile.")
        return real(self, connection, project_id, stage)

    monkeypatch.setattr(SaladRouteProvider, "check_stage", check)
    world.record_charge(0)
    world.tick_until(lambda: world.status() == "failed")
    assert len(_attempts(world)) == 1
    (waiting,) = world.events(placement.WAITING_EVENT)
    reasons = {item["reason"] for item in waiting["skipped"]}
    assert reasons == {"not_enrolled"}
    assert _stop_reason(world) == "stage_failed:g1:no_eligible_provider"
    assert "gpu-alt" in checked


# -- placement, models and the provider hook (pure) ------------------------------------------


def test_candidate_order_skips_a_candidate_only_when_every_class_failed():
    from tessera_control.campaign_models import NormalStage

    base = NormalStage.__new__(NormalStage)  # only the fields candidate_order reads
    object.__setattr__(base, "provider", "a")
    object.__setattr__(base, "alternates", ())
    object.__setattr__(base, "retry", RetryPolicy(reroute_providers=[]))
    attempts = [{"provider_id": "a", "failure_class": "prestart_no_spend"}]
    ids, excluded = placement.candidate_order(base, attempts, ("a",), {"a": 2})
    assert ids == ("a",) and excluded == {}
    ids, excluded = placement.candidate_order(base, attempts, ("a",), {"a": 1})
    assert excluded["a"].reason == class_fallback.SKIP_REASON


def test_failure_reason_reports_budget_first_then_the_exhausted_fallback():
    skip = placement.Skip
    exhausted = placement.Decision(("a", "b"), None, (skip("a", "prestart_failed"),))
    assert placement.failure_reason(exhausted) == class_fallback.EXHAUSTED
    budget = placement.Decision(
        ("a", "b"), None, (skip("a", "prestart_failed"), skip("b", "over_run_cap"))
    )
    assert placement.failure_reason(budget) == "no_eligible_provider"
    assert not exhausted.waiting


def test_absent_fields_keep_every_existing_digest(tmp_path):
    assert "max_class_fallbacks" not in RetryPolicy().model_dump()
    assert RetryPolicy(max_class_fallbacks=3).model_dump()["max_class_fallbacks"] == 3
    world = SaladWorld(tmp_path)
    spec = world.spec
    explicit = json.loads(json.dumps(spec))
    explicit["stages"][0]["retry"]["max_class_fallbacks"] = 0
    config = SaladStageSpec.model_validate(spec["stages"][0]["salad"])
    assert "alternate_profiles" not in config.model_dump()
    assert spec_digest(parse_spec(spec)) == spec_digest(parse_spec(explicit))
    salad = parse_spec(spec).stages[0].salad
    assert "max_class_fallbacks" not in json.dumps(parse_spec(spec).model_dump(mode="json"))
    assert salad is not None and salad.alternate_profiles is None
    # One validator (ADR 0151's): the own profile is never an alternate, names are distinct.
    with pytest.raises(ValueError, match="must not repeat profile"):
        SaladStageSpec.model_validate(
            {**spec["stages"][0]["salad"], "alternate_profiles": ["gpu-main"]}
        )
    with pytest.raises(ValueError, match="must be distinct"):
        SaladStageSpec.model_validate(
            {**spec["stages"][0]["salad"], "alternate_profiles": ["gpu-b", "gpu-b"]}
        )
    with pytest.raises(ValueError):
        SaladStageSpec.model_validate(
            {**spec["stages"][0]["salad"], "alternate_profiles": ["a", "b", "c", "d", "e"]}
        )


def test_class_fallbacks_count_in_the_approved_worst_case(tmp_path):
    from tessera_control.store import Conflict

    assert RetryPolicy().worst_case_attempts == 1
    assert RetryPolicy(max_attempts=2, max_class_fallbacks=2).worst_case_attempts == 4
    # Hold 20 x (1 + 2 fallbacks) = 60 exceeds a per-task ceiling of 50: refused at submit.
    world = _world(tmp_path, max_run_cents=50, max_task_cents=50)
    with pytest.raises(Conflict, match="per-task ceiling"):
        world.env.submit(spec=world.spec)


def test_the_salad_prestart_classes_are_closed_reasons():
    assert PRESTART_FAILURE_CLASSES <= class_fallback.REASONS
    assert "create_rejected" in class_fallback.REASONS


def test_a_v2_stage_lists_alternate_profiles_in_provider_config(tmp_path):
    world = _world(tmp_path)
    stage = salad_stage_v2(world.env, "g1")
    stage["provider_config"] = {**stage["provider_config"], "alternate_profiles": ["gpu-alt"]}
    stage["retry"] = {**stage["retry"], "max_class_fallbacks": 1}
    from salad_campaign_fixture import salad_spec_v2

    world.spec = salad_spec_v2(world.env, [stage])
    world.to_route_started()
    _prestart_failure(world, code="create_failed")
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    _to_next_route(world)
    assert world.route_state()["proposal"]["selection"]["resources"]["gpu_class"] == ALT_GPU
