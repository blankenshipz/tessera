"""Persistent stage I/O index and cross-campaign artifact inputs (ADR 0040 section 3)."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from campaign_runner_harness import World, dump, stage_spec
from test_campaign_models_v2 import _spec2
from test_campaign_providers import _v2_chain  # noqa: F401 - shared v2 builders

from tessera_control.campaign_io import resolve_artifact
from tessera_control.store import Conflict, canonical_digest


def _finished(world: World) -> tuple[dict, dict]:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1")]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    row = world.row(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output'",
        campaign["id"],
    )
    return campaign, row


def _source(campaign: dict, row, **over):
    values = {
        "name": "pinned",
        "campaign_id": campaign["id"],
        "stage": "s1",
        "output": row["name"],
        "sha256": row["sha256"],
    }
    return SimpleNamespace(**{**values, **over})


def _project(world: World) -> dict:
    return {"org_id": world.env.owner["org_id"], "project_id": world.env.project["id"]}


def test_io_rows_are_written_for_inputs_and_outputs_and_are_immutable(tmp_path):
    world = World(tmp_path)
    campaign, output = _finished(world)
    rows = world.env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? ORDER BY direction",
        campaign["id"],
    )
    assert [r["direction"] for r in rows] == ["input", "output"]
    inp = rows[0]
    assert inp["snapshot_id"] == world.env.snapshot_id and inp["data_class"] == "public"
    assert output["data_class"] == "private_derived" and output["path"] == "data.json"
    assert output["manifest_id"] and output["attempt_id"] and output["size_bytes"] > 0
    with sqlite3.connect(world.env.state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE campaign_stage_io SET name='x'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM campaign_stage_io")


def test_artifact_resolution_is_same_project_digest_pinned_and_succeeded_only(tmp_path):
    world = World(tmp_path)
    campaign, row = _finished(world)
    project = _project(world)
    with world.runner.store.runner_connection() as connection:
        resolved = resolve_artifact(connection, project, _source(campaign, row))
        assert resolved.manifest_id == row["manifest_id"] and resolved.sha256 == row["sha256"]
        assert resolved.data_class == "private_derived"
        # Wrong digest, other project and unknown campaign never resolve.
        for source, scope in (
            (_source(campaign, row, sha256="0" * 64), project),
            (_source(campaign, row), {**project, "project_id": "project_other"}),
            (_source(campaign, row), {**project, "org_id": "org_other"}),
            (_source(campaign, row, campaign_id="campaign_none"), project),
            (_source(campaign, row, output="nope"), project),
        ):
            with pytest.raises(Conflict, match="does not resolve"):
                resolve_artifact(connection, scope, source)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='failed_permanent' WHERE id=?",
        row["attempt_id"],
    )
    with world.runner.store.runner_connection() as connection:
        with pytest.raises(Conflict, match="did not succeed"):
            resolve_artifact(connection, project, _source(campaign, row))


def test_artifact_resolution_needs_a_released_retained_manifest(tmp_path):
    world = World(tmp_path)
    campaign, row = _finished(world)
    manifest = world.row("SELECT * FROM artifact_manifests WHERE id=?", row["manifest_id"])
    human = world.row("SELECT id FROM principals WHERE role='human'")["id"]
    world.env.execute(
        """INSERT INTO artifact_custody_releases (id, manifest_id, manifest_digest, revision,
        state, rationale, actor_principal_id, created_at)
        VALUES ('rel_x', ?, ?, 99, 'revoked', 'test', ?, '2026-01-01T00:00:00Z')""",
        manifest["id"],
        manifest["manifest_digest"],
        human,
    )
    with world.runner.store.runner_connection() as connection:
        with pytest.raises(Conflict, match="custody-released"):
            resolve_artifact(connection, _project(world), _source(campaign, row))


def test_a_later_campaign_consumes_an_earlier_output_by_artifact_input(tmp_path):
    world = World(tmp_path)
    env = world.env
    first, row = _finished(world)
    env.trust()  # the artifact is derived data; bind the worker for private_derived
    snapshot = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    artifact = {
        "kind": "artifact",
        "name": "prior",
        "campaign_id": first["id"],
        "stage": "s1",
        "output": row["name"],
        "sha256": row["sha256"],
    }
    stage = {
        "key": "reuse",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": stage_spec(env, "x")["image"],
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [snapshot, artifact],
    }
    spec = _spec2(stage)
    spec["stages"][0]["provider_config"] = {}
    # v2 specs carry no worker; the built-in instance has none, so name it via the v1 route.
    from tessera_control import campaign_provider_worker as worker

    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    try:
        second = world.campaign(spec)
        world.tick_all(second["id"])
        assert world.status(second["id"]) == "succeeded", dump(world, second["id"])
        inputs = world.env.query(
            "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='input' "
            "AND name='prior'",
            second["id"],
        )
        assert len(inputs) == 1
        assert (
            inputs[0]["sha256"] == row["sha256"] and inputs[0]["manifest_id"] == row["manifest_id"]
        )
        assert inputs[0]["data_class"] == "private_derived"
        assert json.loads(
            world.row(
                "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", second["id"]
            )["receipt_json"]
        )
        # An artifact that does not resolve is a 409 at submit.
        bad = json.loads(json.dumps(spec))
        bad["stages"][0]["inputs"][1]["sha256"] = "9" * 64
        body = {"spec": bad}
        with pytest.raises(Conflict, match="does not resolve"):
            env.store.submit_campaign(
                env.owner, env.project["id"], "bad", canonical_digest(body), body
            )
    finally:
        worker.register_from_config(None)
