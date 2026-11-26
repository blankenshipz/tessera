"""A partial manifest is admitted only for an attempt that ended interrupted, and the executor's
interrupted path (import, upload, salvage) works end to end against the real stores."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_campaign_salad_executor import config_for
from test_salad_interruption_retry import (
    CHECKPOINT,
    CUSTODY_BEGAN,
    FINAL,
    checkpoint_world,
)
from test_salad_termination_confirm import _ran_and_reallocated

from tessera_control.campaign_salad_executor import SaladCampaignExecutor, StoreControl


def _executor(world, tmp_path, importer):
    return SaladCampaignExecutor(
        config_for(tmp_path),
        world.profiles.get("gpu-main"),
        StoreControl(
            world.env.state,
            world.worker,
            guardian_status=lambda route: dict(
                FINAL, custody_work_begun=True, stop_cause="allocation_changed"
            ),
        ),
        worker_principal_id=world.worker["id"],
        route_runner=lambda *a: None,
        importer=importer,
        sleep=lambda _: None,
    )


def _checkpoint_importer(calls):
    """What the trusted importer returns for an incomplete custody: only the checkpoint."""

    def importer(route_id, destination, profile, partial=False):
        calls.append(partial)
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / "checkpoint.bin").write_bytes(CHECKPOINT)
        return [
            dict(
                name="checkpoint.bin",
                sha256=hashlib.sha256(CHECKPOINT).hexdigest(),
                bytes=len(CHECKPOINT),
            )
        ]

    return importer


def _item(world):
    row = world.salad()
    return dict(
        attempt_id=row["attempt_id"],
        route_id=row["route_id"],
        operation_id=row["operation_id"],
        stage_key="g1",
        profile="gpu-main",
        phase="executing",
    )


def test_the_executor_imports_uploads_and_the_retry_salvages_the_checkpoint(tmp_path):
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    world.observe_exit()
    calls: list[bool] = []
    executor = _executor(world, tmp_path, _checkpoint_importer(calls))
    executor._finish(_item(world), dict(CUSTODY_BEGAN))
    assert calls == [True]  # the interrupted end state alone asks for a partial import
    row = world.salad()
    assert row["manifest_id"] is not None
    manifest = world.row("SELECT * FROM artifact_manifests WHERE id=?", row["manifest_id"])
    listed = json.loads(manifest["artifacts_json"])
    assert [i["relative_path"] for i in listed] == ["checkpoint.bin"]  # no diagnostic needed
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    (event,) = world.events("campaign.interrupted_checkpointed")
    assert event["digest"] == hashlib.sha256(CHECKPOINT).hexdigest()
    first = world.env.query("SELECT * FROM campaign_stage_attempts WHERE attempt=1")[0]
    assert first["salad_verdict"] == "failed"  # never a pass


def test_a_receipt_without_the_interruption_evidence_asks_for_a_complete_import(tmp_path):
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    world.observe_exit()
    calls: list[bool] = []
    executor = _executor(world, tmp_path, _checkpoint_importer(calls))
    receipt = dict(CUSTODY_BEGAN, stop_confirmed=True, custody_work_begun=None)
    executor._finish(_item(world), receipt)
    assert calls == [False]
    assert world.salad()["manifest_id"] is None  # a lone checkpoint is not a complete manifest


def test_control_refuses_a_partial_manifest_for_a_route_that_was_not_moved(tmp_path):
    world = checkpoint_world(tmp_path)
    world.to_route_started()  # running, never reallocated
    with pytest.raises(Exception, match="needs an interrupted attempt"):
        world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)


def test_a_refused_premature_partial_does_not_block_the_complete_manifest(tmp_path):
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    with pytest.raises(Exception, match="needs an interrupted attempt"):
        world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)
    assert world.row("SELECT 1 FROM artifact_manifests") is None  # nothing was admitted


def test_the_partial_import_requires_a_reallocated_route():
    from types import SimpleNamespace

    from tessera_control.salad_server import TrustedSaladServer
    from tessera_control.store import Conflict

    def server(state):
        built = object.__new__(TrustedSaladServer)
        built.worker = {}
        built.store = SimpleNamespace(get=lambda *_: state, _bound=lambda _: True)
        built._revoke_inputs = lambda _: None
        built.safety = SimpleNamespace(import_retained=lambda *a, **k: dict(files=[], got=k))
        return built

    moved = dict(termination_confirmed=True, reallocated=True)
    stayed = dict(termination_confirmed=True, reallocated=False)
    with pytest.raises(Conflict):
        server(stayed).import_retained("r", destination="d", partial=True)
    with pytest.raises(Conflict):
        server(moved).import_retained("r", destination="d")  # complete needs an unmoved route
    assert server(moved).import_retained("r", destination="d", partial=True)["got"] == dict(
        destination="d", partial=True
    )
    assert server(stayed).import_retained("r", destination="d")["got"] == dict(destination="d")


METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _ordinary_campaign(tmp_path):
    from campaign_runner_harness import World, stage_spec

    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return world, world.campaign(spec)


def test_a_runner_exit_zero_with_a_partial_manifest_is_unknown(tmp_path, monkeypatch):
    """An ordinary run that exits 0 but whose manifest lacks declared files never passes."""
    from tessera_control.campaign_runner import CampaignRunner

    real = CampaignRunner._observe_in

    def observed(connection, attempt):
        seen = real(connection, attempt)
        if seen["manifest"] is not None:
            seen["manifest_partial"] = True  # what the plan comparison reports for a subset
        return seen

    monkeypatch.setattr(CampaignRunner, "_observe_in", staticmethod(observed))
    world, campaign = _ordinary_campaign(tmp_path)
    world.tick_until(
        lambda: (
            bool(world.attempts(campaign["id"]))
            and world.attempts(campaign["id"])[0]["status"] in ("unknown", "succeeded")
        )
    )
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["status"] == "unknown" and attempt["failure_class"] == "missing_manifest"
    assert "partial output manifest" in attempt["failure_detail"]
    assert world.status(campaign["id"]) != "succeeded"


def test_the_plan_comparison_reports_a_subset_manifest_as_partial(tmp_path):
    from tessera_control.artifact_manifests import grant_manifest_is_partial

    world, campaign = _ordinary_campaign(tmp_path)
    world.tick_all(campaign["id"])
    (attempt,) = world.attempts(campaign["id"])
    manifest = world.row("SELECT * FROM artifact_manifests WHERE id=?", attempt["manifest_id"])
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    listed = json.loads(manifest["artifacts_json"])
    with world.env.store.runner_connection() as connection:
        assert not grant_manifest_is_partial(connection, grant, manifest["artifacts_json"])
        assert grant_manifest_is_partial(connection, grant, json.dumps(listed[:-1]))
        assert grant_manifest_is_partial(connection, dict(grant, plan_digest="0" * 64), "[]")


def test_a_reallocated_route_of_another_project_never_admits_a_partial_manifest(
    tmp_path, monkeypatch
):
    """The route state is read only within the owning campaign's org and project: a same-id
    state a foreign tenant recorded (reallocated, stop confirmed) must not count."""
    import test_salad_termination_confirm as confirm

    real = confirm._route_event
    world = checkpoint_world(tmp_path)
    world.to_route_started()  # the campaign's own route: running, never moved
    org = world.row("SELECT org_id FROM campaigns WHERE id=?", world.campaign_id)["org_id"]
    world.env.execute(
        "INSERT INTO projects (id, org_id, name, created_at) VALUES (?,?,?,?)",
        "project_other",
        org,
        "other",
        "2026-11-25T00:00:00Z",
    )
    monkeypatch.setattr(
        confirm,
        "_route_event",
        lambda world, state, **kw: real(world, state, project_id="project_other"),
    )
    _ran_and_reallocated(world, [dict(stopped=True)])  # the moved state lands in another project
    world.record_custody()
    with pytest.raises(Exception, match="needs an interrupted attempt"):
        world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)


def test_the_same_state_inside_the_campaigns_own_org_admits_it(tmp_path):
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    world.record_custody()
    manifest = world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)
    assert manifest["id"]


@pytest.mark.parametrize(
    "evidence",
    [
        None,  # control holds no custody record for the operation
        dict(began=False),  # the guardian says custody never began (the precustody retry)
        dict(began=True, cause="deadline"),  # not an allocation change
    ],
)
def test_without_custody_evidence_the_upload_is_refused_and_nothing_is_stored(tmp_path, evidence):
    """Reallocated, stop confirmed, but no guardian record of custody having begun: refused at
    upload time, no manifest row, no checkpoint salvaged."""
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    if evidence is not None:
        world.record_custody(**evidence)
    with pytest.raises(Exception, match="needs an interrupted attempt"):
        world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)
    assert world.row("SELECT 1 FROM artifact_manifests") is None


def test_a_receipt_that_disagrees_with_the_custody_record_is_not_linked(tmp_path):
    """Defence in depth: admitted on control's custody record, but linked only when the live
    receipt agrees too."""
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    world.record_custody()
    manifest = world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)
    world.observe_exit()
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=dict(CUSTODY_BEGAN, custody_work_begun=False),
            manifest_id=manifest["id"],
            provider_id="provider-group-1",
            guardian_final=FINAL,
        ),
    )
    assert world.salad()["manifest_id"] is None
