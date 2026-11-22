"""Composition records the ADR 0039 class: composed snapshots are always private_derived."""

from __future__ import annotations

import json

import pytest
from campaign_fixture import Env, record_snapshot_private
from campaign_runner_harness import World, stage_spec
from salad_campaign_fixture import SaladWorld

from tessera_control.data_class import snapshot_data_class
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.store import Forbidden, _connect, canonical_digest


def recorded(env, snapshot_id):
    with _connect(env.state / "tessera.sqlite3") as connection:
        return snapshot_data_class(connection, snapshot_id)


def composed_events(env):
    return [
        json.loads(row["payload_json"])
        for row in env.query(
            "SELECT payload_json FROM events WHERE event_type='snapshot.composed' ORDER BY cursor"
        )
    ]


def chain(world):
    env = world.env
    spec = env.spec()
    spec["stages"] = [
        stage_spec(env, "s1"),
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
        ),
    ]
    campaign = world.campaign(spec)
    world.scenarios = [(0, {"data.json": json.dumps({"score": 3})})] * 2
    world.tick_all(campaign["id"])
    return campaign


def test_a_composed_snapshot_is_recorded_private_derived_in_the_same_transaction(tmp_path):
    world = World(tmp_path)
    campaign = chain(world)
    (event,) = composed_events(world.env)
    record = event["data_class"]
    assert record["data_class"] == "private_derived" and record["origin"] == "derived"
    assert recorded(world.env, event["snapshot_id"]) == record
    adopted = [
        json.loads(row["payload_json"])
        for row in world.env.query(
            "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_adopted'"
        )
    ]
    assert any(
        item["snapshot_id"] == event["snapshot_id"] and item["data_class"] == record
        for item in adopted
    )
    stage = world.row(
        "SELECT input_data_class FROM campaign_stages WHERE campaign_id=? AND stage_key='s2'",
        campaign["id"],
    )
    assert stage["input_data_class"] == "private_derived"


def test_the_hook_refuses_a_public_release_of_a_derived_snapshot(tmp_path):
    env = Env(tmp_path)
    env.execute("UPDATE snapshot_releases SET status='revoked'")
    campaign, approval = env.approved()
    record_snapshot_private(env.state, env.snapshot_id, "derived")
    body = {
        "expected_digest": env.snapshot_digest,
        "worker_principal_id": env.worker["id"],
        "classification": "public",
        "permits_worker_transfer": True,
        "rationale": "pre-approved",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    principal, _intent, key, digest = env.intent(
        campaign,
        approval,
        "release_snapshot",
        {"snapshot_id": env.snapshot_id, "classification": "public"},
        body,
        attempt=1,
    )
    with pytest.raises(Forbidden):
        SnapshotReleaseStore(env.state).release_snapshot(
            principal, env.snapshot_id, key, digest, body
        )
    assert (
        env.query("SELECT count(*) AS n FROM snapshot_releases WHERE status='active'")[0]["n"] == 0
    )
    assert canonical_digest(body) == digest


def test_a_salad_stage_whose_input_is_recorded_private_fails_before_any_release(tmp_path):
    world = SaladWorld(tmp_path)
    world.start()
    world.tick_until(
        lambda: (
            world.row("SELECT 1 FROM campaign_salad_stages WHERE source_id IS NOT NULL") is not None
        )
    )
    record_snapshot_private(world.env.state, world.env.snapshot_id, "default")
    world.assess_source()
    world.tick_until(lambda: world.status() in {"failed", "stopped"})
    assert world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)[
        "stop_reason"
    ].endswith("input_not_admitted")
    assert (
        world.env.query("SELECT count(*) AS n FROM snapshot_releases WHERE status='active'")[0]["n"]
        == 0
    )
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]
        == 0
    )
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 0
