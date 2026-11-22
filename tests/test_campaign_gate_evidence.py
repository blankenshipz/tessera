"""A campaign stage's pre-registered gate evidence becomes metric records unattended (ADR 0049).

Synthetic fixtures: the fake worker writes the output; the runner binds the split before
dispatch and records the derived measurement under the campaign's own approval.
"""

from __future__ import annotations

import json
import uuid

import pytest
from campaign_runner_harness import IMAGE, World, dump
from test_campaign_models_v2 import _spec2

from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as cp
from tessera_control.store import utc_now

PARTITIONS = [{"name": "test", "member_count": 12}, {"name": "train", "member_count": 40}]


@pytest.fixture(autouse=True)
def _local_provider():
    yield
    worker.register_from_config(None)


def _world(tmp_path) -> World:
    from provider_trust_fixture import DERIVED, install_trust

    world = World(tmp_path)
    worker_id = world.env.worker["id"]
    cp.configure_providers({"local_docker": {"worker_principal_id": worker_id}})
    install_trust({"local-docker": (DERIVED, [worker_id])})
    return world


def _lock(world, project_id, owner_id) -> str:
    ident = "lock_" + uuid.uuid4().hex
    world.env.execute(
        """INSERT INTO gate_split_locks (id,project_id,name,membership_digest,partitions_json,
        member_count,created_at,created_by_principal_id) VALUES (?,?,?,?,?,?,?,?)""",
        ident,
        project_id,
        "lock",
        uuid.uuid4().hex + uuid.uuid4().hex,
        json.dumps(PARTITIONS),
        52,
        utc_now(),
        owner_id,
    )
    return ident


def _stage(env, lock, *, partition="test", seed=7):
    return {
        "key": "a",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
        "metrics": [{"name": "rmse", "path": "data.json", "json_pointer": "/score"}],
        "gate_evidence": {
            "split_lock_id": lock,
            "seed": seed,
            "metrics": [{"name": "rmse", "partition": partition, "unit": "kcal/mol"}],
        },
    }


def test_declared_metrics_become_gate_metric_records_under_the_campaign_approval(tmp_path):
    world = _world(tmp_path)
    env = world.env
    lock = _lock(world, env.project["id"], env.owner["id"])
    campaign = world.campaign(_spec2(_stage(env, lock)))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])

    attempt = world.attempts(campaign["id"])[0]
    binding = world.row(
        "SELECT * FROM gate_attempt_split_bindings WHERE attempt_id=?",
        attempt["admitted_attempt_id"],
    )
    assert (binding["lock_id"], binding["seed"]) == (lock, 7)
    records = env.query(
        "SELECT * FROM gate_metric_records WHERE attempt_id=?", attempt["admitted_attempt_id"]
    )
    assert [(r["metric"], r["partition"], r["seed"], r["value"]) for r in records] == [
        ("rmse", "test", 7, 3.0)
    ]
    measured = json.loads(attempt["measurements_json"])
    assert measured[0]["gate_metric_record_id"] == records[0]["id"]
    # The record was written by the delegated step, not a manual human record.
    steps = {
        r["step"]
        for r in env.query(
            "SELECT step FROM campaign_step_intents WHERE campaign_id=?", campaign["id"]
        )
    }
    assert {"bind_split", "record_gate_metric"} <= steps


def test_an_undeclared_partition_fails_the_attempt_and_records_nothing(tmp_path):
    world = _world(tmp_path)
    env = world.env
    lock = _lock(world, env.project["id"], env.owner["id"])
    campaign = world.campaign(_spec2(_stage(env, lock, partition="holdout")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) != "succeeded", dump(world, campaign["id"])
    assert env.query("SELECT 1 FROM gate_metric_records") == []
    failed = env.query(
        "SELECT step, status, error_class FROM campaign_step_intents "
        "WHERE campaign_id=? AND step='bind_split'",
        campaign["id"],
    )
    assert failed and all(r["status"] == "failed" for r in failed), [dict(r) for r in failed]


def test_specs_without_gate_evidence_keep_their_digest_and_never_bind(tmp_path):
    from tessera_control.campaign_models import parse_spec

    world = _world(tmp_path)
    env = world.env
    stage = _stage(env, "lock_x")
    plain = {k: v for k, v in stage.items() if k != "gate_evidence"}
    assert "gate_evidence" not in parse_spec(_spec2(plain)).model_dump(mode="json")["stages"][0]
    campaign = world.campaign(_spec2(plain))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert env.query("SELECT 1 FROM gate_attempt_split_bindings") == []


def test_gate_evidence_cannot_be_combined_with_retries(tmp_path):
    from pydantic import ValidationError

    from tessera_control.campaign_models import parse_spec

    world = _world(tmp_path)
    stage = _stage(world.env, "lock_x")
    stage["retry"] = {"max_attempts": 2, "transient_exit_codes": [7]}
    with pytest.raises(ValidationError, match="retry.max_attempts of 1"):
        parse_spec(_spec2(stage))
    stage["retry"] = {"max_attempts": 1, "transient_exit_codes": []}
    parse_spec(_spec2(stage))


def test_delegated_bind_split_must_equal_the_approved_spec(tmp_path):
    import sqlite3

    from tessera_control.campaign_delegation import (
        DELEGATED_ACTOR_KEY,
        assert_gate_binding_matches,
    )
    from tessera_control.store import Forbidden

    world = _world(tmp_path)
    env = world.env
    lock = _lock(world, env.project["id"], env.owner["id"])
    campaign = world.campaign(_spec2(_stage(env, lock)))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    intent = world.row(
        "SELECT id FROM campaign_step_intents WHERE campaign_id=? AND step='bind_split'",
        campaign["id"],
    )
    live = {DELEGATED_ACTOR_KEY: {"campaign_id": campaign["id"], "intent_id": intent["id"]}}
    good = {
        "split_lock_id": lock,
        "seed": 7,
        "metrics": [{"name": "rmse", "partition": "test", "unit": "kcal/mol"}],
    }
    with sqlite3.connect(env.state / "tessera.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        assert_gate_binding_matches(connection, live, good)
        # A human actor (no delegation marker) is not constrained by the spec.
        assert_gate_binding_matches(connection, {}, {**good, "seed": 1})
        for bad in (
            {**good, "split_lock_id": "lock_other"},
            {**good, "seed": 8},
            {**good, "metrics": [{"name": "rmse", "partition": "train", "unit": "kcal/mol"}]},
            {
                **good,
                "metrics": good["metrics"] + [{"name": "x", "partition": "test", "unit": "u"}],
            },
        ):
            with pytest.raises(Forbidden):
                assert_gate_binding_matches(connection, live, bad)
