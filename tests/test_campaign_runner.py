"""Campaign runner: staging chain, budget, stop rules, receipt, lineage and cancel (ADR 0038).

Fakes only: the control app runs in-process and a fake worker adapter stands in for Docker.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_models import CampaignSpec, stage_execution_spec
from tessera_control.campaign_receipts import build_receipt
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_steps import StepFailed
from tessera_control.snapshot_composition import composed_inventory
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.store import Conflict, _connect, canonical_digest
from tessera_control.worker_grants import WorkerGrantStore
from tessera_control.workspace import inventory_digest

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _spec(world: World, stages: list[dict], **overrides) -> dict:
    spec = world.env.spec(**overrides)
    spec["stages"] = stages
    return spec


def _receipt(world: World, campaign_id: str) -> tuple[dict, str]:
    row = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    return json.loads(row["receipt_json"]), row["receipt_digest"]


def test_single_stage_runs_to_a_receipt_with_lineage_and_a_settled_ledger(tmp_path):
    world = World(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(world, [stage_spec(env, "s1", metrics=METRIC)]))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["status"] == "succeeded" and attempt["exit_code"] == 0
    assert attempt["run_receipt_digest"] and attempt["manifest_id"] and attempt["grant_id"]
    assert json.loads(attempt["measurements_json"])[0]["value"] == 3
    receipt, digest = _receipt(world, campaign["id"])
    assert digest == canonical_digest(receipt)
    assert receipt["evidence_class"] == "operational"
    assert receipt["verdict"] == {"operational": "continue", "scientific_verdict": "not_assessed"}
    assert receipt["approval"]["decision_class"] == campaign["approval"]["decision_class"]
    stage = receipt["stages"][0]
    assert stage["attempts"][0]["run_receipt_digest"] == attempt["run_receipt_digest"]
    assert stage["attempts"][0]["measurements"][0]["name"] == "score"
    assert len(receipt["lineage"]["node_ids"]) == 2 and len(receipt["lineage"]["edge_ids"]) == 1
    assert all(row["status"] == "done" for row in env.intents(campaign["id"]))
    release = world.row("SELECT status FROM snapshot_releases WHERE id=?", attempt["release_id"])
    assert release["status"] == "revoked"
    ledger = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", campaign["id"])
    assert (ledger["held_cents"], ledger["settled_cents"]) == (0, 0)
    assert world.events_of("campaign.completed")[0]["payload"]["receipt_digest"] == digest
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        assert build_receipt(connection, row, "succeeded")[1] == digest
    finally:
        connection.close()


def test_staging_chain_composes_the_declared_inputs_and_links_lineage(tmp_path):
    world = World(tmp_path)
    env = world.env
    spec = env.spec()
    spec["stages"] = [
        stage_spec(env, "s1", metrics=METRIC),
        stage_spec(
            env,
            "s2",
            inputs=[
                {
                    "kind": "snapshot",
                    "name": "code",
                    "snapshot_id": env.snapshot_id,
                    "digest": env.snapshot_digest,
                    "data_class": "public",
                },
                {"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]},
            ],
            metrics=METRIC,
        ),
    ]
    CampaignSpec.model_validate(spec)
    campaign = world.campaign(spec)
    world.scenarios = [
        (0, {"data.json": json.dumps({"score": 3})}),
        (0, {"data.json": json.dumps({"score": 5})}),
    ]
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    stages = {
        row["stage_key"]: row
        for row in env.query("SELECT * FROM campaign_stages WHERE campaign_id=?", campaign["id"])
    }
    # The chained stage ran on a composed snapshot whose digest the runner precomputed.
    first = world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='s1'",
        campaign["id"],
    )
    artifacts = json.loads(
        world.row("SELECT artifacts_json FROM artifact_manifests WHERE id=?", first["manifest_id"])[
            "artifacts_json"
        ]
    )
    snapshot_entries = json.loads(
        world.row("SELECT manifest_json FROM source_snapshots WHERE id=?", env.snapshot_id)[
            "manifest_json"
        ]
    )["entries"]
    entries = [
        (f"code/{e['relative_path']}", e["sha256"], e["size"], "imported_unverified")
        for e in snapshot_entries
    ] + [("up/data.json", artifacts[0]["sha256"], artifacts[0]["size"], "imported_unverified")]
    expected = inventory_digest(composed_inventory(entries))
    assert stages["s2"]["input_snapshot_digest"] == expected
    assert stages["s2"]["input_snapshot_id"] != env.snapshot_id
    assert stages["s1"]["input_snapshot_id"] == env.snapshot_id
    receipt, digest = _receipt(world, campaign["id"])
    assert digest == canonical_digest(receipt) and receipt["status"] == "succeeded"
    assert [s["status"] for s in receipt["stages"]] == ["succeeded", "succeeded"]
    # The second output derives from the first stage's retained output node.
    edges = [
        json.loads(row["response_json"])
        for row in env.intents(campaign["id"])
        if row["step"] == "record_lineage_edge"
    ]
    assert edges and all(edge["relation"] == "derived_from" for edge in edges)
    nodes = {
        row["id"]: row["sha256"]
        for row in env.query("SELECT id, sha256 FROM artifact_lineage_nodes")
    }
    first_output = artifacts[0]["sha256"]
    assert any(nodes[edge["parent_node_id"]] == first_output for edge in edges)
    assert len(world.worker_runs) == 2
    assert env.events("snapshot.composed")


def test_budget_refusal_stops_the_second_ready_stage_without_any_authority(tmp_path):
    world = World(tmp_path)
    env = world.env
    spec = _spec(
        world,
        [
            stage_spec(env, "a1", max_exposure_cents=6, metrics=METRIC),
            stage_spec(env, "b2", max_exposure_cents=6, metrics=METRIC),
        ],
        budget_cap_cents=10,
    )
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "stopped", dump(world, campaign["id"])
    assert world.stages(campaign["id"]) == {"a1": "succeeded", "b2": "refused_budget"}
    refused = [a for a in world.attempts(campaign["id"]) if a["stage_key"] == "b2"]
    assert [a["status"] for a in refused] == ["refused_budget"]
    assert refused[0]["grant_id"] is None and refused[0]["failure_class"] == "budget_refused"
    # Nothing beyond the reservation was written for the refused stage.
    assert [r for r in env.intents(campaign["id"]) if r["stage_key"] == "b2"] == []
    event = world.events_of("campaign.stage_refused")[0]["payload"]
    assert event["stage_key"] == "b2" and event["budget_cap_cents"] == 10
    receipt, _ = _receipt(world, campaign["id"])
    assert receipt["status"] == "stopped" and receipt["stop_reason"] == "budget_refused:b2"
    assert receipt["budget"] == {"cap": 10, "held": 0, "settled": 0, "currency": "USD"}
    assert receipt["verdict"]["operational"] == "stop"
    # The funded stage ran under a per-stage experiment budget and a task budget hold.
    assert world.row("SELECT count(*) AS n FROM experiment_budgets")["n"] == 1


def test_stop_rule_stops_the_campaign_and_skips_later_stages(tmp_path):
    world = World(tmp_path)
    env = world.env
    spec = env.spec()
    spec["stages"] = [
        stage_spec(
            env, "s1", metrics=METRIC, stop_when=[{"metric": "score", "op": "gt", "value": 2.5}]
        ),
        stage_spec(
            env,
            "s2",
            inputs=[{"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]}],
        ),
    ]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "stopped", dump(world, campaign["id"])
    assert world.stages(campaign["id"]) == {"s1": "succeeded", "s2": "skipped"}
    receipt, _ = _receipt(world, campaign["id"])
    assert receipt["stop_reason"] == "stop_rule:s1.score"
    assert receipt["stages"][0]["stop_results"][0]["result"] is True
    assert len(world.worker_runs) == 1
    assert receipt["verdict"]["scientific_verdict"] == "not_assessed"
    # The true rule moved the campaign through ``stopping`` and said so.
    (stopping,) = world.events_of("campaign.stopping")
    assert stopping["payload"]["stop_reason"] == "stop_rule:s1.score"


def test_missing_metric_is_not_recorded_and_never_trips_a_stop_rule(tmp_path):
    world = World(tmp_path)
    env = world.env
    world.default_scenario = (0, {"data.json": json.dumps({"other": 1})})
    spec = _spec(
        world,
        [
            stage_spec(
                env, "s1", metrics=METRIC, stop_when=[{"metric": "score", "op": "gt", "value": 0}]
            )
        ],
    )
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    receipt, _ = _receipt(world, campaign["id"])
    assert receipt["stages"][0]["stop_results"][0]["result"] == "not_recorded"
    assert receipt["stages"][0]["attempts"][0]["measurements"][0]["value"] is None


def test_human_release_of_the_same_snapshot_is_never_revoked_by_the_runner(tmp_path):
    world = World(tmp_path)
    env = world.env
    env.execute("UPDATE snapshot_releases SET status='active', revoked_at=NULL")
    campaign = world.campaign(_spec(world, [stage_spec(env, "s1")]))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "failed"
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["failure_class"] == "step_refused" and attempt["grant_id"] is None
    assert world.row("SELECT status FROM snapshot_releases")["status"] == "active"
    assert world.worker_runs == []
    ledger = world.row("SELECT held_cents FROM campaigns WHERE id=?", campaign["id"])
    assert ledger["held_cents"] == 0


def test_cancel_before_any_lease_revokes_the_grant_and_reconciles_after_expiry(tmp_path):
    world = World(tmp_path)
    env = world.env
    spec = _spec(
        world,
        [
            stage_spec(env, "s1", metrics=METRIC),
            stage_spec(
                env,
                "s2",
                inputs=[{"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]}],
            ),
        ],
    )
    campaign = world.campaign(spec)
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(campaign["id"])),
        worker=False,
    )
    (attempt,) = world.attempts(campaign["id"])
    release_id = attempt["release_id"]
    world.cancel(campaign)
    assert world.status(campaign["id"]) == "cancel_requested"
    # Revoked, but an unleased grant is only released after it expires.
    world.tick_until(
        lambda: (
            world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])[
                "status"
            ]
            == "cancel_requested"
        ),
        worker=False,
    )
    assert world.status(campaign["id"]) == "cancel_requested"
    env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        attempt["grant_id"],
    )
    world.tick_all(campaign["id"], worker=False)
    assert world.status(campaign["id"]) == "cancelled", dump(world, campaign["id"])
    assert world.stages(campaign["id"]) == {"s1": "cancelled", "s2": "cancelled"}
    (after,) = world.attempts(campaign["id"])
    assert after["status"] == "cancelled" and after["failure_class"] == "cancelled"
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "reconciled"
    )
    assert (
        world.row("SELECT status FROM snapshot_releases WHERE id=?", release_id)["status"]
        == "revoked"
    )
    ledger = world.row("SELECT held_cents FROM campaigns WHERE id=?", campaign["id"])
    assert ledger["held_cents"] == 0
    receipt, _ = _receipt(world, campaign["id"])
    assert receipt["status"] == "cancelled" and world.worker_runs == []


def test_cancel_after_the_worker_confirmed_exit_reconciles_that_grant(tmp_path):
    world = World(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(world, [stage_spec(env, "s1", metrics=METRIC)]))
    # Let the worker finish, but cancel before the runner observes the exit.
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(campaign["id"])),
        worker=False,
    )
    world.poller.run(max_cycles=1)
    assert len(world.worker_runs) == 1
    world.cancel(campaign)
    world.tick_all(campaign["id"], worker=False)
    assert world.status(campaign["id"]) == "cancelled", dump(world, campaign["id"])
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["status"] == "cancelled"
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled"
    assert [r["step"] for r in env.intents(campaign["id"])].count("reconcile_grant") == 1


def _stage(world: World, campaign_id: str, key: str = "s1"):
    return world.row(
        "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key=?", campaign_id, key
    )


def test_an_interleaved_human_plan_revision_is_never_approved_by_delegation(tmp_path):
    world = World(tmp_path)
    env = world.env
    spec = _spec(world, [stage_spec(env, "s1", metrics=METRIC)])
    campaign = world.campaign(spec)
    cid = campaign["id"]
    world.tick_until(lambda: _stage(world, cid)["prep_phase"] == "planned", worker=False)
    stage = _stage(world, cid)
    task = world.row("SELECT * FROM tasks WHERE id=?", stage["task_id"])
    body = {
        "expected_revision": task["revision"],
        "title": "human edit",
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": stage_execution_spec(
            CampaignSpec.model_validate(spec), "s1", stage["input_snapshot_digest"]
        ),
    }
    status, _ = env.plain.revise_task_plan(
        env.human(), task["id"], "human-revise-1", canonical_digest(body), body
    )
    assert status == 200
    world.tick_all(cid, worker=False)
    assert world.status(cid) != "succeeded"
    assert world.row("SELECT status FROM tasks WHERE id=?", task["id"])["status"] != "approved"
    assert [
        r for r in env.intents(cid) if r["step"] == "approve_task" and r["status"] == "done"
    ] == []
    (attempt,) = world.attempts(cid)
    assert attempt["status"] == "failed_permanent" and attempt["failure_class"] == "step_refused"


def test_a_lost_release_reply_is_adopted_on_restart_and_the_release_is_revoked(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    campaign = world.campaign(_spec(world, [stage_spec(world.env, "s1", metrics=METRIC)]))
    cid = campaign["id"]
    original = CampaignRunner._set_attempt
    fired = []

    def lossy(self, attempt_id, **fields):
        if "release_id" in fields and not fired:
            fired.append(True)
            raise RuntimeError("synthetic crash")
        return original(self, attempt_id, **fields)

    monkeypatch.setattr(CampaignRunner, "_set_attempt", lossy)
    world.tick_until(lambda: bool(fired), worker=False)
    assert world.attempts(cid)[0]["release_id"] is None
    world.restart_runner()
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    (attempt,) = world.attempts(cid)
    release = world.row("SELECT status FROM snapshot_releases WHERE id=?", attempt["release_id"])
    assert release["status"] == "revoked"


def test_a_transient_failure_revoking_the_second_release_does_not_wedge_cleanup(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    env = world.env
    spec = _spec(
        world,
        [
            stage_spec(env, "s1", metrics=METRIC),
            stage_spec(
                env,
                "s2",
                inputs=[
                    {
                        "kind": "snapshot",
                        "name": "code",
                        "snapshot_id": env.snapshot_id,
                        "digest": env.snapshot_digest,
                        "data_class": "public",
                    },
                    {"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]},
                ],
                metrics=METRIC,
            ),
        ],
    )
    campaign = world.campaign(spec)
    cid = campaign["id"]
    world.scenarios = [
        (0, {"data.json": json.dumps({"score": 3})}),
        (0, {"data.json": json.dumps({"score": 5})}),
    ]
    original = SnapshotReleaseStore.revoke_snapshot_release
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("synthetic transient failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SnapshotReleaseStore, "revoke_snapshot_release", flaky)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert len(calls) >= 3
    assert world.row("SELECT count(*) AS n FROM campaign_receipts")["n"] == 1
    for attempt in world.attempts(cid):
        release = world.row(
            "SELECT status FROM snapshot_releases WHERE id=?", attempt["release_id"]
        )
        assert release["status"] == "revoked"


def test_an_operation_with_no_observation_yet_is_awaited_not_marked_unknown(tmp_path):
    world = World(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(world, [stage_spec(env, "s1", metrics=METRIC)]))
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(campaign["id"])),
        worker=False,
    )
    world.poller.run(max_cycles=1)
    # The real worker records the operation at lease time and observes later: model the
    # window in which the operation exists but no observation does.
    env.execute("DROP TRIGGER IF EXISTS execution_observations_no_delete")
    env.execute("DELETE FROM execution_observations")
    for _ in range(3):
        world.runner.tick()
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["status"] != "unknown", dump(world, campaign["id"])
    assert attempt["failure_class"] is None


# -- review round 2 (M1, M2, M3, M4, L2, L4, L6) ---------------------------------------


def _single(world: World, **stage_extra) -> dict:
    spec = _spec(world, [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)])
    return world.campaign(spec)


def _admitted(world: World, campaign: dict) -> dict:
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(campaign["id"])),
        worker=False,
    )
    (attempt,) = world.attempts(campaign["id"])
    return attempt


def test_cancel_after_the_approver_lapses_is_finished_by_a_second_human_re_approval(tmp_path):
    world = World(tmp_path)
    campaign = _single(world)
    cid = campaign["id"]
    attempt = _admitted(world, campaign)
    second_human = world.env.human("second")
    world.env.execute(
        "UPDATE principals SET revoked_at='2026-01-01T00:00:00Z' WHERE id=?",
        campaign["approval"]["approving_principal_id"],
    )
    current = world.row("SELECT revision FROM campaigns WHERE id=?", cid)
    body = {"expected_revision": current["revision"], "reason": "operator cancel"}
    status, _ = world.env.store.cancel_campaign(
        second_human, cid, "cancel-lapsed", canonical_digest(body), body
    )
    assert status == 200
    for _ in range(5):
        world.runner.tick()
    # Stuck: every reduce step lapses.
    assert world.status(cid) == "cancel_requested"
    row = dict(world.row("SELECT * FROM campaigns WHERE id=?", cid))
    second = world.env.approve(row, second_human)
    # Re-approval after cancel never resumes forward work.
    assert second["status"] == "cancel_requested"
    assert world.status(cid) == "cancel_requested"
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        attempt["grant_id"],
    )
    world.tick_all(cid, worker=False)
    assert world.status(cid) == "cancelled", dump(world, cid)
    assert world.row("SELECT 1 FROM campaign_receipts WHERE campaign_id=?", cid) is not None
    assert world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"] == 0
    assert world.worker_runs == []


def test_cancel_revokes_unleased_grants_and_stops_the_worker(tmp_path):
    world = World(tmp_path)
    campaign = _single(world)
    attempt = _admitted(world, campaign)
    grants = WorkerGrantStore(world.env.state).list_pending(world.env.worker)["grants"]
    assert [g["id"] for g in grants] == [attempt["grant_id"]]
    world.cancel(campaign)
    # (c) the cancel transaction itself fenced the grant and said so.
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "cancel_requested"
    )
    (event,) = world.events_of("campaign.cancel_requested")
    assert event["payload"]["revoked_grant_ids"] == [attempt["grant_id"]]
    assert world.events_of("execution.grant_cancel_requested")
    world.poller.run(max_cycles=1)
    assert world.worker_runs == []


def test_a_paused_campaign_lists_and_leases_nothing(tmp_path):
    world = World(tmp_path)
    campaign = _single(world)
    cid = campaign["id"]
    attempt = _admitted(world, campaign)
    assert WorkerGrantStore(world.env.state).list_pending(world.env.worker)["grants"]
    world.env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", cid)
    # (a) not listed
    assert WorkerGrantStore(world.env.state).list_pending(world.env.worker)["grants"] == []
    # (b) and not leasable even if the worker knows the grant id
    with pytest.raises(Conflict, match="campaign is not running"):
        world.env.execution.acquire_execution_lease(
            world.env.worker, attempt["grant_id"], "lease-paused", "lease-paused"
        )
    assert world.row("SELECT count(*) AS n FROM execution_leases")["n"] == 0
    world.env.execute("UPDATE campaigns SET status='running' WHERE id=?", cid)
    assert WorkerGrantStore(world.env.state).list_pending(world.env.worker)["grants"]


def test_the_runner_aborts_its_tick_when_the_lease_is_lost(tmp_path):
    world = World(tmp_path)
    campaign = _single(world)
    world.runner.tick()
    world.env.execute("UPDATE campaign_runner_lease SET holder_id='someone-else'")
    before = len(world.env.intents(campaign["id"]))
    report = world.runner.tick()  # lease is still held by another live holder
    assert report.actions == () and len(world.env.intents(campaign["id"])) == before
    # Lost between the tick's acquisition and a step: the step does not run.
    world.env.execute("UPDATE campaign_runner_lease SET expires_at='2000-01-01T00:00:00Z'")
    original = world.runner._lease

    def steal(connection):
        taken = original(connection)
        connection.execute("UPDATE campaign_runner_lease SET holder_id='rival'")
        return taken

    world.runner._lease = steal
    report = world.runner.tick()
    assert "lease_lost" in report.actions
    assert len(world.env.intents(campaign["id"])) == before


def test_project_limits_that_cannot_fit_the_stage_emit_one_waiting_event(tmp_path, monkeypatch):
    world = World(tmp_path)
    campaign = _single(world)
    monkeypatch.setattr(
        CampaignRunner, "_limits_allow", lambda self, project_id, hold, attempt=None: False
    )
    for _ in range(40):
        world.runner.tick()
    (waiting,) = world.events_of("campaign.stage_waiting_limits")
    assert waiting["payload"]["stage_key"] == "s1"
    assert waiting["payload"]["campaign_id"] == campaign["id"]
    assert world.attempts(campaign["id"])[0]["grant_id"] is None


def test_reserve_creates_nothing_once_the_campaign_is_no_longer_running(tmp_path):
    world = World(tmp_path)
    campaign = _single(world)
    cid = campaign["id"]
    ctx = world.runner._load(cid)
    world.env.execute("UPDATE campaigns SET status='cancel_requested' WHERE id=?", cid)
    assert world.runner._reserve(ctx, "s1") is False
    assert world.attempts(cid) == []


def test_unknown_experiment_exposure_blocks_a_new_reservation(tmp_path, monkeypatch):
    world = World(tmp_path)
    campaign = _single(world)
    cid = campaign["id"]
    ctx = world.runner._load(cid)
    monkeypatch.setattr(
        CampaignRunner, "_experiment_exposure_unknown", staticmethod(lambda *args: True)
    )
    with world.runner.store.runner_connection() as connection:
        assert world.runner._lease(connection)  # state transitions require the lease
    assert world.runner._reserve(ctx, "s1") is False
    (attempt,) = world.attempts(cid)
    assert attempt["status"] == "refused_budget" and attempt["failure_class"] == "exposure_unknown"
    assert world.events_of("campaign.stage_refused")[0]["payload"]["reason"] == "exposure_unknown"


def test_experiment_exposure_helper_reads_budget_status(monkeypatch):
    from tessera_control import campaign_runner as module

    campaign = {"org_id": "org"}
    assert not CampaignRunner._experiment_exposure_unknown(None, campaign, {"experiment_id": None})
    for unknown in (True, False):
        monkeypatch.setattr(
            module, "budget_status", lambda db, actor, eid, u=unknown: {"exposure_unknown": u}
        )
        assert (
            CampaignRunner._experiment_exposure_unknown(None, campaign, {"experiment_id": "e"})
            is unknown
        )


def test_only_a_known_worker_provider_settles_at_zero(tmp_path):
    from dataclasses import replace

    from tessera_control.campaign_models import normalize

    world = World(tmp_path)
    spec = CampaignSpec.model_validate(_spec(world, [stage_spec(world.env, "s1")]))
    stage = normalize(spec).stages[0]
    attempt = {"grant_id": None}
    assert world.runner._settlement_for(stage, attempt) == (0, "local_zero")
    other = replace(stage, provider="future-vendor")
    with pytest.raises(StepFailed, match="settlement_unavailable"):
        world.runner._settlement_for(other, attempt)


def test_reconcile_refuses_a_replayed_amount_that_disagrees_with_the_basis(tmp_path, monkeypatch):
    from types import SimpleNamespace

    world = World(tmp_path)
    runner = world.runner
    calls = []
    monkeypatch.setattr(runner, "_settlement_for", lambda stage, attempt: (3, "human_charge"))
    monkeypatch.setattr(runner, "_one", lambda *a, **k: {"revision": 1})
    monkeypatch.setattr(
        runner, "_step", lambda *a, **k: SimpleNamespace(response={"settlement_cents": 10})
    )
    monkeypatch.setattr(runner, "block_settlement_mismatch", lambda attempt: calls.append(attempt))
    monkeypatch.setattr(
        runner, "_settle", lambda *a: pytest.fail("a mismatched amount must not settle")
    )
    # Attempt rows always carry failure_class/failure_detail; the ADR 0212 branch reads them.
    monkeypatch.setattr(
        runner, "_provider", lambda *a: pytest.fail("a non-0212 attempt must not take that path")
    )
    attempt = {
        "id": "a1",
        "grant_id": "g1",
        "attempt": 1,
        "failure_class": "observed_unknown",
        "failure_detail": None,
    }
    assert runner._do_reconcile(None, {"stage_key": "s1"}, None, attempt) is True
    assert calls == [attempt]


def test_a_nonzero_amount_is_never_labelled_unclaimed_or_local_zero(tmp_path):
    world = World(tmp_path)
    with world.runner.store.runner_connection() as connection:
        assert world.runner._lease(connection)
        for basis in ("unclaimed_zero", "local_zero"):
            with pytest.raises(StepFailed, match="settlement_unavailable|cannot settle"):
                world.runner._settle(connection, "none", 5, basis)


def test_tick_surfaces_non_lock_sweep_errors_and_defers_lock_errors(tmp_path):
    import sqlite3

    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_providers import register_provider, unregister_provider

    world = World(tmp_path)
    message = {"text": "database is locked"}

    class Sweeper(worker.WorkerGrantProvider):
        def sweep(self, rt):
            raise sqlite3.OperationalError(message["text"])

    register_provider(Sweeper(worker._descriptor("sweeper-test", "")))
    try:
        world.runner.tick()  # lock contention is deferred
        message["text"] = "no such column: settlement_basis"
        with pytest.raises(sqlite3.OperationalError):
            world.runner.tick()
    finally:
        unregister_provider("sweeper-test")


def test_a_written_receipt_is_immutable(tmp_path):
    import sqlite3

    world = World(tmp_path)
    campaign = _single(world)
    world.tick_all(campaign["id"])
    receipt, digest = _receipt(world, campaign["id"])
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        world.env.execute(
            "UPDATE campaign_receipts SET receipt_json='{}' WHERE campaign_id=?", campaign["id"]
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        world.env.execute("DELETE FROM campaign_receipts WHERE campaign_id=?", campaign["id"])
    assert _receipt(world, campaign["id"]) == (receipt, digest)
    assert canonical_digest(receipt) == digest


def test_waiting_for_project_limits_holds_nothing_and_resumes_when_they_allow(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    campaign = _single(world)
    monkeypatch.setattr(
        CampaignRunner, "_limits_allow", lambda self, project_id, hold, attempt=None: False
    )
    for _ in range(10):
        world.runner.tick()
    assert world.status(campaign["id"]) == "running"
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", campaign["id"])["held_cents"] == 0
    )
    monkeypatch.undo()
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded"


def test_a_stop_rule_lets_an_in_flight_sibling_attempt_finish(tmp_path):
    world = World(tmp_path)
    env = world.env
    spec = env.spec(budget_cap_cents=10)
    spec["stages"] = [
        stage_spec(
            env,
            "s1",
            metrics=METRIC,
            stop_when=[{"metric": "score", "op": "gt", "value": 2.5}],
            max_exposure_cents=2,
        ),
        stage_spec(env, "s2", max_exposure_cents=2),
    ]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    # The rule fired for s1 while s2's attempt was already in flight: s2 finishes, nothing
    # new starts, and the campaign ends stopped, never failed.
    assert world.status(campaign["id"]) == "stopped", dump(world, campaign["id"])
    assert world.stages(campaign["id"]) == {"s1": "succeeded", "s2": "succeeded"}
    assert world.events_of("campaign.stopping")
