"""Salad stage driver: real stores and runner, executor and human played by the test."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    cpu_stage,
    salad_provider_reset,
    salad_spec,
    salad_spec_v2,
    salad_stage,
    salad_stage_v2,
)


def test_scaffold_pauses_for_the_source_assessment(tmp_path):
    world = SaladWorld(tmp_path)
    world.start()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    paused = world.events("campaign.stage_awaiting_human")[0]
    assert paused["stage_key"] == "g1" and len(paused["records"]) == 1
    # No hold is taken and nothing is reserved while a human has not assessed the source.
    world.tick(3)
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]
        == 0
    )
    assert world.attempt() is None
    assert len(world.events("campaign.stage_awaiting_human")) == 1


def test_attempt_reaches_review_then_route(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    paused = world.events("campaign.stage_awaiting_human")[-1]
    row = world.salad()
    assert paused["records"] == [row["plan_decision_id"], row["source_decision_id"]]
    assert "ADR 0032" in paused["reason"]
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    route = world.route_state()
    assert route["state"] == "queued" and route["desired"] == "run"
    assert route["approved_by"] == world.human["id"]


def test_happy_path_settles_at_the_human_charge(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_charge()
    attempt = world.attempt()
    assert attempt["status"] == "resolved" or attempt["status"] == "measured"
    row = world.salad()
    assert row["salad_evidence_digest"] and row["live_receipt_digest"]
    assert attempt["exit_code"] is None and attempt["run_receipt_digest"] is None
    assert attempt["salad_verdict"] == "ok"
    assert attempt["salad_evidence_digest"] == row["salad_evidence_digest"]
    # Unknown cost never settles: the hold stays held and a human is told what to record.
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["held_cents"] == 20 and campaign["settled_cents"] == 0
    paused = world.events("campaign.stage_awaiting_human")[-1]
    assert "compute charge" in paused["reason"]
    world.tick(3)
    assert world.status() == "running" and world.phase() == "awaiting_charge"
    world.record_charge(13)
    world.tick_until(lambda: world.status() == "succeeded")
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["settled_cents"] == 13 and campaign["held_cents"] == 0
    assert world.phase() == "settled" and world.salad()["charged_cents"] == 13
    limits = world.row("SELECT settled_cents FROM execution_project_limits")
    assert limits["settled_cents"] == 13


def two_stages(env):
    return [salad_stage(env, "g1"), salad_stage(env, "g2")]


def test_unknown_cost_blocks_positive_holds_until_settled(tmp_path):
    world = SaladWorld(tmp_path, stages=two_stages)
    world.start()
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_salad_stages WHERE stage_key='g1' AND source_id IS NOT NULL"
            )
            is not None
        )
    )
    world.assess_source("g1")
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    # g2 is ready to reserve (its source is assessed) but the open exposure blocks it.
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_salad_stages WHERE stage_key='g2' AND source_id IS NOT NULL"
            )
            is not None
        )
    )
    world.assess_source("g2")
    world.tick(6)
    assert world.attempt("g2") is None
    assert world.events("campaign.stage_waiting_exposure")[0]["stage_key"] == "g2"
    world.record_charge(9)
    world.tick_until(lambda: world.attempt("g2") is not None)
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 9
    )


def test_hold_zero_stage_is_not_blocked_by_open_exposure(tmp_path):
    def stages(env):
        return [salad_stage(env, "g1"), cpu_stage(env, "z1", hold=0), cpu_stage(env, "z2", hold=6)]

    world = SaladWorld(tmp_path, stages=stages)
    gate = {"open": False}
    original = world.runner._advance_stage

    def gated(ctx, key):
        if key in {"z1", "z2"} and not gate["open"]:
            return False
        return original(ctx, key)

    world.runner._advance_stage = gated
    world.to_awaiting_charge()
    gate["open"] = True
    world.tick(4)
    assert world.attempt("z1") is not None
    assert world.attempt("z2") is None
    waiting = world.events("campaign.stage_waiting_exposure")
    assert [event["stage_key"] for event in waiting] == ["z2"]


def test_late_charge_is_added_once_and_overrun_blocks(tmp_path):
    world = SaladWorld(tmp_path, stages=two_stages, spec_over={"budget_cap_cents": 30})
    world.to_awaiting_charge()
    world.record_charge(13)
    world.tick_until(lambda: world.phase() == "settled")
    world.record_charge(5, delivery="late-1")
    world.tick_until(lambda: world.events("campaign.late_charge") != [])
    assert world.events("campaign.late_charge")[0]["delta_cents"] == 5
    world.tick(3)
    assert len(world.events("campaign.late_charge")) == 1
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 18
    )
    assert world.salad()["charged_cents"] == 18
    # 18 settled plus the next stage's 20 hold would exceed 30; the overrun itself blocks.
    world.record_charge(14, delivery="late-2")
    world.tick_until(lambda: world.status() == "blocked")
    assert world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)[
        "stop_reason"
    ].startswith("budget_overrun")
    world.tick(3)
    assert world.status() == "blocked"


def test_synthetic_receipts_are_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], "x1")
    world.claim("create")
    world.upload_manifest()
    world.terminate_route(provenance="synthetic")
    world.observe_exit()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        "x2",
        dict(
            route_id=row["route_id"],
            live_receipt={"status": "passed", "reason": "verified_journey", "stop_confirmed": True},
        ),
    )
    world.tick_until(lambda: world.status() == "blocked")
    assert world.phase() == "unknown"
    assert world.attempt()["status"] == "unknown"
    assert (
        world.row("SELECT status FROM campaign_stages WHERE stage_key='g1'")["status"]
        == "blocked_unknown"
    )


def test_unconfirmed_stop_is_blocked_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], "x1")
    world.claim("create")
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        "x2",
        dict(route_id=row["route_id"], live_receipt={"status": "inconclusive"}),
    )
    world.tick_until(lambda: world.status() == "blocked")
    assert world.phase() == "unknown"


def test_failed_run_with_confirmed_stop_waits_for_a_charge_then_fails(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.run_to_outcome(passed=False, outputs=False)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(4)
    world.tick_until(lambda: world.status() == "failed")
    assert world.attempt()["status"] == "failed_permanent"
    failed = world.attempt()
    # No exit code or run receipt exists for Salad: only the verdict and evidence digest.
    assert failed["exit_code"] is None and failed["run_receipt_digest"] is None
    assert failed["salad_verdict"] == "failed" and failed["salad_evidence_digest"]
    assert failed["failure_class"] == "salad_not_passed"
    assert failed["failure_detail"].startswith("salad:")
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    published = receipt["stages"][0]["attempts"][0]
    assert published["exit_code"] is None and published["run_receipt_digest"] is None
    # A program-bound stage leaves the scientific verdict to a human.
    assert receipt["verdict"]["scientific_verdict"] == "pending_human_approval"
    assert published["salad_verdict"] == "failed"
    assert published["salad_evidence_digest"] == failed["salad_evidence_digest"]
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 4
    )


def test_mismatched_manifest_is_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], "x1")
    world.claim("create")
    manifest = world.upload_manifest(b'{"score": 2}')
    world.terminate_route()
    world.observe_exit()
    receipt = {
        "status": "passed",
        "reason": "verified_journey",
        "stop_confirmed": True,
        "outputs": [{"name": "output.json", "sha256": "0" * 64, "bytes": 12}],
    }
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        "x2",
        dict(route_id=row["route_id"], live_receipt=receipt, manifest_id=manifest["id"]),
    )
    world.tick_until(lambda: world.status() == "blocked")
    assert "manifest" in world.attempt()["failure_detail"]


def retrying(env):
    return [salad_stage(env, "g1", attempts=2)]


def test_unresolved_reviews_expire_into_the_unclaimed_path_and_retry(tmp_path):
    world = SaladWorld(tmp_path, stages=retrying)
    world.to_awaiting_review()
    first = world.attempt()
    world.grant_expiry_in_past()
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    done = world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )
    assert done["id"] == first["id"] and done["status"] == "failed_transient"
    assert done["failure_class"] == "t1_salad_unclaimed" and done["settled_cents"] == 0
    assert world.salad(attempt=1)["phase"] == "unclaimed"
    reconciled = world.row("SELECT * FROM execution_grants WHERE id=?", done["grant_id"])
    assert reconciled["status"] == "reconciled" and reconciled["settlement_cents"] == 0
    assert world.events("execution.grant_reconciled_unclaimed")[0]["proof"] == (
        "no_create_or_start_claim"
    )
    # The retry needs its own lease and fresh reviews bound to the new admission.
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    second = world.salad()
    assert second["plan_decision_id"] != world.salad(attempt=1)["plan_decision_id"]
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]
        == 20
    )


def test_rejected_review_is_unclaimed_and_exhausts_attempts(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.resolve_decisions("reject")
    world.tick_until(lambda: world.status() in {"failed", "stopped"})
    assert world.salad()["phase"] == "unclaimed"
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 0
    )


def test_claim_present_refuses_the_unclaimed_reconcile(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.claim("create")
    row = world.salad()
    attempt = world.attempt()
    from tessera_control.salad_unclaimed import SaladUnclaimedStore
    from tessera_control.store import Conflict, canonical_digest

    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    from tessera_control.execution import ExecutionStore

    execution = ExecutionStore(world.env.state)
    execution.revoke_execution_grant(
        world.human,
        grant["id"],
        "revoke",
        canonical_digest({"expected_revision": 1}),
        grant["revision"],
    )
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", grant["id"])
    body = {"expected_revision": grant["revision"]}
    with pytest.raises(Conflict, match="claimed"):
        SaladUnclaimedStore(world.env.state).reconcile(
            world.human, grant["id"], "unclaimed", canonical_digest(body), body
        )
    assert row["route_id"]


def test_cancel_during_execution_cancels_route_and_revokes(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], "x1")
    world.claim("create")
    world.claim("start")
    world.cancel()
    world.tick_until(lambda: world.route_state()["desired"] == "stop")
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", world.attempt()["grant_id"])
    assert grant["status"] == "cancel_requested"
    # A claimed run is never zero-settled: the campaign stays open until evidence and a charge.
    world.tick(4)
    assert world.status() == "cancel_requested"
    world.terminate_route()
    world.observe_exit()
    world.tick(3)
    assert world.status() == "cancel_requested"
    world.record_charge(6)
    world.tick_until(lambda: world.status() == "cancelled")
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 6
    )


def test_cancel_before_any_claim_settles_zero(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.phase() == "unclaimed"
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 0
    )


def test_a_salad_grant_is_leasable_only_while_it_awaits_a_lease(tmp_path):
    from test_execution import _worker

    from tessera_control.execution import ExecutionStore
    from tessera_control.store import Conflict, canonical_digest
    from tessera_control.worker_grants import WorkerGrantStore

    world = SaladWorld(tmp_path)
    cpu_worker = _worker(world.env.state, world.env.owner, world.env.project["id"], "cpu-worker")
    world.start()
    world.tick_until(
        lambda: world.row("SELECT 1 FROM campaign_salad_stages WHERE source_id IS NOT NULL")
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    grants = WorkerGrantStore(world.env.state)
    grant_id = world.attempt()["grant_id"]
    # A CPU worker principal never sees the grant issued to the Salad worker.
    assert grants.list_pending(cpu_worker)["grants"] == []
    assert [g["id"] for g in grants.list_pending(world.worker)["grants"]] == [grant_id]
    # Once the attempt leaves awaiting_lease the grant is neither listed nor leasable.
    world.env.execute(
        "UPDATE campaign_salad_attempts SET phase='leased' WHERE attempt_id=?",
        world.attempt()["id"],
    )
    assert grants.list_pending(world.worker)["grants"] == []
    with pytest.raises(Conflict, match="not running"):
        ExecutionStore(world.env.state).acquire_execution_lease(
            world.worker, grant_id, "late-lease", canonical_digest("late")
        )


def test_submit_enforces_the_operator_profile_ceilings(tmp_path):
    from tessera_control.campaign_provider_salad import register_provider_instance
    from tessera_control.store import Conflict, canonical_digest

    def submit(world, spec):
        body = {"spec": spec}
        return world.env.store.submit_campaign(
            world.env.owner,
            world.env.project["id"],
            "k" + str(world.counter),
            canonical_digest(body),
            body,
        )

    world = SaladWorld(tmp_path, max_run_cents=25, max_task_cents=30)
    env = world.env
    ok = submit(world, salad_spec(env, [salad_stage(env, "g1", hold=25)]))
    assert ok[0] == 201
    world.counter += 1
    with pytest.raises(Conflict, match="per-run ceiling"):
        submit(world, salad_spec(env, [salad_stage(env, "g1", hold=26)]))
    world.counter += 1
    with pytest.raises(Conflict, match="per-task ceiling"):
        submit(world, salad_spec(env, [salad_stage(env, "g1", hold=20, attempts=2)]))
    world.counter += 1
    unknown = salad_stage(env, "g1")
    unknown["salad"] = {**unknown["salad"], "profile": "other"}
    with pytest.raises(Conflict, match="unknown operator profile"):
        submit(world, salad_spec(env, [unknown]))
    world.counter += 1
    foreign = salad_stage(env, "g1")
    foreign["salad"] = {**foreign["salad"], "worker_principal_id": "someone-else"}
    with pytest.raises(Conflict, match="profile's worker"):
        submit(world, salad_spec(env, [foreign]))
    world.counter += 1
    register_provider_instance(None)
    with pytest.raises(Conflict, match="not enabled"):
        submit(world, salad_spec(env, [salad_stage(env, "g1")]))


def test_approval_rechecks_the_profile(tmp_path):
    from tessera_control.campaign_provider_salad import register_provider_instance
    from tessera_control.store import Conflict, canonical_digest

    world = SaladWorld(tmp_path)
    submitted = world.env.submit(spec=world.spec)
    register_provider_instance(None)
    body = {
        "expected_revision": submitted["revision"],
        "expected_spec_digest": submitted["spec_digest"],
    }
    with pytest.raises(Conflict, match="not enabled"):
        world.env.store.approve_campaign(
            world.human, submitted["id"], "a1", canonical_digest(body), body
        )


def test_the_route_fence_follows_the_approver_and_the_exact_selection(tmp_path):
    from tessera_control.remote_execution import RemoteExecutionStore
    from tessera_control.remote_execution_models import ExecutionSelection, TargetPolicy
    from tessera_control.store import Forbidden, _connect

    world = SaladWorld(tmp_path)
    world.to_route_started()
    state = world.route_state()
    selection = ExecutionSelection.model_validate(state["proposal"]["selection"])
    # The runner derived exactly the profile selection: same inputs, same bytes.
    from tessera_control.campaign_models import CampaignSpec
    from tessera_control.salad_campaign_profiles import derive_selection

    stage = CampaignSpec.model_validate(world.spec).stages[0]
    again = derive_selection(
        world.profiles.get("gpu-main"),
        stage.salad.workload,
        operation_id=world.salad()["operation_id"],
        epoch=world.salad()["epoch"],
        snapshot_digest=world.env.snapshot_digest,
        image=stage.image,
        max_cost_cents=stage.max_exposure_cents,
        plan_max_seconds=stage.max_duration_seconds,
    )
    assert again.model_dump_json() == selection.model_dump_json()
    routes = RemoteExecutionStore(world.env.state, policies=(TargetPolicy(selection=selection),))
    with _connect(world.env.state / "tessera.sqlite3") as db:
        routes._current(db, world.worker, state)  # the heartbeat check passes while live
    world.env.execute(
        "UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE principal_id=?",
        world.human["id"],
    )
    with _connect(world.env.state / "tessera.sqlite3") as db, pytest.raises(Forbidden):
        routes._current(db, world.worker, state)


def test_a_human_reconcile_of_the_grant_supplies_the_settlement(tmp_path):
    from tessera_control.execution import ExecutionStore
    from tessera_control.store import canonical_digest

    world = SaladWorld(tmp_path)
    world.to_awaiting_charge()
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", world.attempt()["grant_id"])
    body = {
        "expected_revision": grant["revision"],
        "settlement_cents": 11,
        "provider_terminated": True,
    }
    ExecutionStore(world.env.state).reconcile_execution_grant(
        world.human, grant["id"], "human-reconcile", canonical_digest(body), body
    )
    world.tick_until(lambda: world.status() == "succeeded")
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)[
            "settled_cents"
        ]
        == 11
    )


def test_an_executor_silent_past_the_route_window_is_unknown(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.tick(2)
    assert world.phase() == "route_started"
    world.env.execute(
        "UPDATE campaign_salad_attempts SET updated_at='2000-01-01T00:00:00Z' WHERE attempt_id=?",
        world.attempt()["id"],
    )
    world.tick_until(lambda: world.status() == "blocked")
    assert world.phase() == "unknown"
    assert "silent" in world.attempt()["failure_detail"]
    assert world.attempt()["settled_cents"] is None  # the hold stays held


def test_submit_and_approve_refuse_a_salad_input_not_recorded_public(tmp_path):
    from campaign_fixture import record_snapshot_private

    from tessera_control.store import Conflict, canonical_digest

    world = SaladWorld(tmp_path)
    env = world.env
    spec = salad_spec(env, [salad_stage(env, "g1")])
    body = {"spec": spec}
    _, campaign = env.store.submit_campaign(
        env.owner, env.project["id"], "pub", canonical_digest(body), body
    )
    # The record changes after submit (a hand-edited or re-recorded snapshot): approve refuses.
    record_snapshot_private(env.state, env.snapshot_id, "default")
    approve = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    # The Salad stage check (fetch hosts with a non-public input) or the core data-class
    # admission refuses first; either way the stage is not approved.
    with pytest.raises(Conflict, match="not recorded public|input is not public"):
        env.store.approve_campaign(
            env.human(), campaign["id"], "ap", canonical_digest(approve), approve
        )
    # A spec that still declares public is refused at submit, too.
    with pytest.raises(Conflict, match="not recorded public|input is not public"):
        env.store.submit_campaign(
            env.owner, env.project["id"], "again", canonical_digest(body), body
        )


def test_the_shared_guard_refuses_a_salad_input_that_stopped_being_public(tmp_path):
    from campaign_fixture import record_snapshot_private

    from tessera_control.campaign_models import CampaignSpec, normalize
    from tessera_control.campaign_providers import get_provider
    from tessera_control.campaign_store import CampaignStore
    from tessera_control.store import Conflict, _connect

    world = SaladWorld(tmp_path)
    env = world.env
    record_snapshot_private(env.state, env.snapshot_id, "derived")
    parsed = normalize(
        CampaignSpec.model_validate(salad_spec(env, [salad_stage(env, "g1")]))
    ).stages[0]
    with (
        _connect(env.state / "tessera.sqlite3") as connection,
        pytest.raises(Conflict, match="not trusted to receive"),
    ):
        CampaignStore.check_stage_inputs(connection, parsed, get_provider("salad"))


def test_salad_stage_output_inputs_follow_the_provider_trust(tmp_path, trust_policy):
    import dataclasses

    from tessera_control.campaign_models import CampaignSpec, NormalInput, normalize
    from tessera_control.campaign_providers import get_provider
    from tessera_control.campaign_store import CampaignStore
    from tessera_control.store import Conflict, _connect

    world = SaladWorld(tmp_path)
    env = world.env
    stage = normalize(
        CampaignSpec.model_validate(salad_spec(env, [salad_stage(env, "g1")]))
    ).stages[0]
    source = NormalInput(kind="stage", name="prior", stage="up", paths=("out.json",))
    stage = dataclasses.replace(stage, inputs=(*stage.inputs, source))
    provider = get_provider("salad")
    with _connect(env.state / "tessera.sqlite3") as connection:
        # The default trust setting is public-only: derived data is refused, naming both.
        with pytest.raises(Conflict, match="private_derived") as refused:
            CampaignStore.check_stage_inputs(connection, stage, provider)
        assert "salad" in str(refused.value)
        trust_policy({"salad": (["private_derived"], [env.worker["id"]])})
        CampaignStore.check_stage_inputs(connection, stage, provider)


def test_an_image_outside_the_profile_allowlist_is_refused(tmp_path):
    from tessera_control.campaign_steps import StepFailed
    from tessera_control.store import Conflict, canonical_digest

    world = SaladWorld(tmp_path)
    env = world.env
    other = salad_stage(env, "g1", image="ghcr.io/example/other@sha256:" + "2" * 64)
    body = {"spec": salad_spec(env, [other])}
    with pytest.raises(Conflict, match="not allowlisted"):
        env.store.submit_campaign(env.owner, env.project["id"], "img", canonical_digest(body), body)
    # The driver re-checks, so a profile edited after approval still fences the stage.
    from tessera_control.campaign_models import CampaignSpec

    spec = CampaignSpec.model_validate(salad_spec(env, [other]))
    with pytest.raises(StepFailed, match="salad_image_not_allowlisted"):
        world.driver.profile(spec.stages[0])


def _reconcile_grant_elsewhere(world, settlement=None):
    """Someone else reconciles the grant, recording `settlement` (NULL by default)."""
    grant_id = world.attempt()["grant_id"]
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled', settlement_cents=? WHERE id=?",
        settlement,
        grant_id,
    )


def _hold(world):
    return world.row(
        "SELECT held_cents, settled_cents FROM campaigns WHERE id=?", world.campaign_id
    )


def test_a_claimed_attempt_is_never_zero_settled_by_a_foreign_reconcile(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.claim("create")
    world.claim("start")
    held_before = _hold(world)["held_cents"]
    assert held_before > 0
    _reconcile_grant_elsewhere(world, None)
    world.cancel()
    world.tick(5)
    assert world.attempt()["settled_cents"] is None
    assert _hold(world)["held_cents"] == held_before
    assert world.phase() == "awaiting_charge"
    assert world.status() == "cancel_requested"
    world.record_charge(4)
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settled_cents"] == 4


def test_a_foreign_reconcile_with_a_recorded_settlement_settles_at_it(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.claim("create")
    _reconcile_grant_elsewhere(world, 3)
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settled_cents"] == 3


def test_an_unclaimed_foreign_reconcile_settles_at_zero(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    _reconcile_grant_elsewhere(world, None)
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settled_cents"] == 0


def _crash_after_start_route(world, monkeypatch):
    from tessera_control.campaign_salad import SaladStageDriver

    real = SaladStageDriver._record_started

    def crashing(self, *args, **kwargs):
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(SaladStageDriver, "_record_started", crashing)
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_step_intents WHERE campaign_id=? AND step='start_route' "
                "AND status='done'",
                world.campaign_id,
            )
            is not None
        )
    )
    monkeypatch.setattr(SaladStageDriver, "_record_started", real)


def test_a_crash_between_start_route_and_the_operation_id_is_adopted_on_resume(
    tmp_path, monkeypatch
):
    world = SaladWorld(tmp_path)
    _crash_after_start_route(world, monkeypatch)
    assert world.attempt()["operation_id"] is None
    world.tick_until(lambda: world.attempt()["operation_id"] is not None)
    assert world.attempt()["operation_id"] == world.salad()["operation_id"]
    assert world.phase() == "route_started"
    assert world.attempt()["status"] == "running"


def test_cancel_after_that_crash_unwinds_through_salad_and_never_settles_zero(
    tmp_path, monkeypatch
):
    world = SaladWorld(tmp_path)
    _crash_after_start_route(world, monkeypatch)
    assert world.attempt()["operation_id"] is None
    world.claim("create")
    _reconcile_grant_elsewhere(world, None)
    world.cancel()
    world.tick(5)
    assert world.attempt()["settled_cents"] is None
    assert world.phase() == "awaiting_charge"
    assert world.status() == "cancel_requested"


def test_reserve_checks_exposure_in_the_hold_transaction(tmp_path):
    from tessera_control.campaign_runner import _ExposureBlocked

    world = SaladWorld(tmp_path)
    world.to_route_started()
    ctx = world.runner._load(world.campaign_id)
    seen = []
    from tessera_control.campaign_providers import get_provider

    provider = get_provider("salad")
    original = provider.exposure_open

    def probe(connection, campaign_id):
        seen.append(connection.in_transaction)
        return original(connection, campaign_id)

    provider.exposure_open = probe
    with pytest.raises(_ExposureBlocked):
        world.runner._reserve_hold(ctx, "g1", 5)
    assert seen == [True]


def test_a_runner_that_lost_the_lease_cannot_commit_a_transition(tmp_path):
    from tessera_control.campaign_runner import _LeaseLost

    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.env.execute("DELETE FROM campaign_runner_lease")
    with pytest.raises(_LeaseLost):
        world.runner._set_attempt(world.attempt()["id"], status="running")


def _clean_final(**over):
    return dict(
        dict(
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
        ),
        **over,
    )


def test_record_salad_outcome_stores_the_guardian_final_and_refuses_extra_keys(tmp_path):
    from tessera_control.store import StoreError

    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    base = dict(route_id=row["route_id"], live_receipt={"schema": "x"})
    with pytest.raises(StoreError):
        world.env.store.record_salad_outcome(
            world.worker,
            row["attempt_id"],
            "k-extra",
            dict(base, guardian_final=_clean_final(manifest={"secret": 1})),
        )
    assert world.salad()["guardian_final_json"] is None
    world.env.store.record_salad_outcome(
        world.worker, row["attempt_id"], "k-ok", dict(base, guardian_final=_clean_final())
    )
    assert json.loads(world.salad()["guardian_final_json"]) == _clean_final()
    assert world.events("campaign.salad_outcome_recorded")[-1]["guardian_final_present"] is True


def test_the_receipt_reports_approved_code_for_a_staged_snapshot(tmp_path):
    from tessera_control.campaign_receipts import build_receipt
    from tessera_control.snapshots import SnapshotStore
    from tessera_control.workspace import capture_workspace, inventory_digest

    world = SaladWorld(tmp_path)
    world.to_route_started()
    root = tmp_path / "staged"
    root.mkdir()
    (root / "main.py").write_bytes(b"x = 1\n")
    capture = capture_workspace(str(root))
    actor = dict(world.env.owner, automation_label="receipt-test")
    _, adopted = SnapshotStore(world.env.state).adopt_snapshot(
        actor,
        world.env.project["id"],
        "adopt-code",
        inventory_digest(capture.inventory),
        capture,
        code_approval={"paths": ["main.py"]},
    )
    world.env.execute(
        "UPDATE campaign_stages SET input_snapshot_id=? WHERE campaign_id=?",
        adopted["id"],
        world.campaign_id,
    )
    with world.runner.store.runner_connection() as connection:
        receipt, _ = build_receipt(connection, world.row("SELECT * FROM campaigns"), "running")
    shown = receipt["stages"][0]["input"]
    assert shown["input_code"] == "private_code_approved"
    assert [f["relative_path"] for f in shown["input_code_files"]] == ["main.py"]


def test_a_claimed_attempt_with_a_foreign_zero_settlement_waits_for_a_charge(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.claim("create")
    _reconcile_grant_elsewhere(world, 0)
    world.cancel()
    world.tick(5)
    assert world.attempt()["settled_cents"] is None
    assert world.phase() == "awaiting_charge"
    world.record_charge(4)
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settled_cents"] == 4


def test_a_claimed_foreign_settlement_is_raised_to_the_recorded_charges(tmp_path):
    world = SaladWorld(tmp_path, spec_over={"budget_cap_cents": 500})
    world.to_route_started()
    world.claim("create")
    world.record_charge(50)
    _reconcile_grant_elsewhere(world, 3)
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert world.attempt()["settled_cents"] == 50
    assert world.attempt()["settlement_basis"] == "human_charge"


def test_any_external_operation_row_with_a_claim_counts_as_claimed(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    driver = world.driver
    real_rows = world.runner._rows

    def rows(sql, params=()):
        found = real_rows(sql, params)
        if "FROM external_operations WHERE grant_id" in sql:
            return [{"operation_id": "op-first"}, {"operation_id": "op-claimed"}]
        return found

    monkeypatch.setattr(world.runner, "_rows", rows)
    monkeypatch.setattr(driver, "_claimed", lambda org, operation: operation == "op-claimed")
    attempt = dict(world.attempt())
    grant = {"id": attempt["grant_id"], "settlement_cents": None}
    assert driver.reconciled(attempt, grant) is None
    assert world.phase() == "awaiting_charge"


def test_a_missing_salad_row_waits_instead_of_raising(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    attempt = {**dict(world.attempt()), "id": "no-such-attempt"}
    grant = {"id": attempt["grant_id"], "settlement_cents": None}
    assert world.driver.reconciled(attempt, grant) is None


def test_record_started_bumps_the_revision_of_both_rows(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    attempt_id = world.attempt()["id"]
    before = (world.salad()["revision"], world.attempt()["revision"])
    world.driver._record_started(attempt_id, world.salad()["operation_id"])
    assert world.salad()["revision"] == before[0] + 1
    assert world.attempt()["revision"] == before[1] + 1


def test_a_replayed_settlement_that_differs_marks_unknown_blocks_and_settles_nothing(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    world = SaladWorld(tmp_path)
    world.to_route_started()
    runner, driver = world.runner, world.driver
    attempt = dict(world.attempt())
    monkeypatch.setattr(runner, "_settlement_for", lambda *_: (5, "human_charge"))
    # The recorded reconcile step answers with 0 (and the hold, below, with the hold).
    for replayed in (0, int(attempt["hold_cents"])):
        monkeypatch.setattr(
            runner,
            "_step",
            lambda *a, _r=replayed, **k: SimpleNamespace(response={"settlement_cents": _r}),
        )
        world.env.execute(
            "UPDATE campaigns SET status='running', stop_reason=NULL WHERE id=?",
            world.campaign_id,
        )
        seen = {"reconciliation": {"observed_state": "exited", "termination_confirmed": True}}
        done = driver._unwind_claimed(
            SimpleNamespace(), attempt["stage_key"], None, attempt, world.salad(), seen, None
        )
        assert done is True
        assert world.attempt()["status"] == "unknown"
        assert world.attempt()["settled_cents"] is None
        assert world.status() == "blocked"
        assert (
            world.row("SELECT stop_reason FROM campaigns")["stop_reason"] == "settlement_mismatch"
        )
        assert world.phase() == "unknown"
        world.env.execute(
            "UPDATE campaign_stage_attempts SET status='running' WHERE id=?", attempt["id"]
        )
    assert [e["reason"] for e in world.events("campaign.blocked")] == ["settlement_mismatch"] * 2


def test_a_v2_salad_spec_runs_end_to_end_with_delegated_grants(tmp_path):
    """v2 puts the Salad worker only in provider_config; delegation must still own it."""
    world = SaladWorld(tmp_path)
    world.spec = salad_spec_v2(world.env, [salad_stage_v2(world.env, "g1")])
    assert "worker_principal_id" not in world.spec["stages"][0]
    world.to_route_started()
    assert world.phase() == "route_started"
    from tessera_control.campaign_delegation import _spec_workers

    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert world.worker["id"] in _spec_workers(dict(campaign))
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    world.record_charge(13)
    world.tick_until(lambda: world.status() == "succeeded")
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["settled_cents"] == 13 and campaign["held_cents"] == 0
    assert world.phase() == "settled"
