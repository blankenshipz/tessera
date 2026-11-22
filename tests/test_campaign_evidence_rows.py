"""Typed evidence rows a stage emits are ingested by the platform itself (ADR 0052). Fakes only."""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from campaign_runner_harness import IMAGE, World, dump
from test_campaign_models_v2 import _spec2

from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as cp
from tessera_control.campaign_gate_models import UnlockRequirement
from tessera_control.campaign_gates import evaluate_unlock
from tessera_control.experiments import ExperimentStore
from tessera_control.gate_evidence_paired import PairedRefused, compute_paired_delta
from tessera_control.store import Forbidden, canonical_digest, utc_now

PARTITIONS = [{"name": "test", "member_count": 12}, {"name": "train", "member_count": 40}]


@pytest.fixture(autouse=True)
def _restore_providers():
    yield
    worker.register_from_config(None)


def _world(tmp_path) -> World:
    from provider_trust_fixture import DERIVED, install_trust

    world = World(tmp_path)
    worker_id = world.env.worker["id"]
    cp.configure_providers({"local_docker": {"worker_principal_id": worker_id}})
    install_trust({"local-docker": (DERIVED, [worker_id])})
    return world


def _lock(world) -> str:
    ident = "lock_" + uuid.uuid4().hex
    world.env.execute(
        """INSERT INTO gate_split_locks (id,project_id,name,membership_digest,partitions_json,
        member_count,created_at,created_by_principal_id) VALUES (?,?,?,?,?,?,?,?)""",
        ident,
        world.env.project["id"],
        "lock",
        uuid.uuid4().hex + uuid.uuid4().hex,
        json.dumps(PARTITIONS),
        52,
        utc_now(),
        world.env.owner["id"],
    )
    return ident


def _arms(world) -> dict[str, str]:
    body = {
        "goal": "g",
        "success_criterion": "c",
        "protocol": "p",
        "variants": [
            {
                "kind": kind,
                "name": kind,
                "method_digest": d * 64,
                "input_digest": e * 64,
                "metadata": {},
            }
            for kind, d, e in (("baseline", "a", "b"), ("intervention", "c", "d"))
        ],
    }
    store = ExperimentStore(world.env.state)
    _, created = store.create_experiment(
        world.env.owner,
        world.env.project["id"],
        "arms-" + uuid.uuid4().hex,
        canonical_digest(body),
        body,
    )
    return {v["kind"]: v["id"] for v in created["design"]["variants"]}


def _rows(arms, *, values=None, drop=None, extra=None, sample=12):
    values = values or {("baseline", 0): 2.0, ("baseline", 1): 2.2, ("intervention", 0): 1.5}
    values.setdefault(("intervention", 1), 1.8)
    rows = [
        {
            "variant": arms[kind],
            "seed": seed,
            "metric": "rmse",
            "value": value,
            "partition": "test",
            "sample_count": sample,
        }
        for (kind, seed), value in values.items()
    ]
    if drop is not None:
        rows.pop(drop)
    if extra:
        rows.append(extra)
    return rows


def _stage(env, lock, arms, **grid):
    return {
        "key": "a",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [
            {
                "name": "rows",
                "path": "data.json",
                "kind": "evidence_rows",
                "max_bytes": 100_000,
                "evidence": {
                    "split_lock_id": lock,
                    "variants": [arms["baseline"], arms["intervention"]],
                    "seeds": [0, 1],
                    "metrics": {"rmse": "kcal/mol"},
                    "partitions": ["test"],
                    **grid,
                },
            }
        ],
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
    }


def _run(tmp_path, rows_fn=_rows, **grid):
    world = _world(tmp_path)
    arms, lock = _arms(world), _lock(world)
    world.scenarios = [(0, {"data.json": json.dumps({"rows": rows_fn(arms)})})]
    campaign = world.campaign(_spec2(_stage(world.env, lock, arms, **grid)))
    world.tick_all(campaign["id"])
    return world, campaign, arms, lock


def _connection(world):
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def _delta(world, arms, lock, campaign_id=None):
    with _connection(world) as connection:
        return compute_paired_delta(
            connection,
            world.env.project["id"],
            arm_a_variant_id=arms["baseline"],
            arm_b_variant_id=arms["intervention"],
            lock_id=lock,
            metric="rmse",
            partition="test",
            campaign_id=campaign_id,
        )


def test_the_platform_ingests_the_rows_and_a_paired_delta_unlock_runs_on_them(tmp_path):
    world, campaign, arms, lock = _run(tmp_path)
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    rows = world.env.query("SELECT * FROM gate_ingested_rows ORDER BY variant_id, seed")
    assert len(rows) == 4
    assert {r["unit"] for r in rows} == {"kcal/mol"} and {r["lock_id"] for r in rows} == {lock}
    attempt = world.attempts(campaign["id"])[0]
    assert {r["attempt_id"] for r in rows} == {attempt["admitted_attempt_id"]}
    measured = {m["name"]: m for m in json.loads(attempt["measurements_json"])}
    assert measured["evidence_rows.rows"]["value"] == 4

    result = _delta(world, arms, lock, campaign["id"])
    assert result["n"] == 2 and result["sources"] == ["derived"]
    assert result["mean"] == pytest.approx(((2.0 - 1.5) + (2.2 - 1.8)) / 2)

    requirement = UnlockRequirement.model_validate(
        {
            "campaign_id": campaign["id"],
            "bound_outcome": {
                "arm_a_variant_id": arms["baseline"],
                "arm_b_variant_id": arms["intervention"],
                "metric": "rmse",
                "split_lock_id": lock,
                "partition": "test",
                "statistic": "mean",
                "comparison": "gt",
                "threshold": 0.1,
                "minimum_pairs": 2,
                "minimum_samples": 12,
                "allowed_outcomes": ["pass"],
            },
        }
    )
    with _connection(world) as connection:
        refusal, evaluations = evaluate_unlock(
            connection, world.env.owner["org_id"], world.env.project["id"], [requirement]
        )
    assert refusal is None and evaluations[0]["basis_kind"] == "bound_outcome"


@pytest.mark.parametrize(
    "rows_fn",
    [
        lambda arms: _rows(arms, drop=0),
        lambda arms: _rows(arms, extra={**_rows(arms)[0], "seed": 9}),
        lambda arms: _rows(arms, extra=_rows(arms)[0]),
        lambda arms: _rows(arms, sample=11),
        lambda arms: _rows(arms, extra={**_rows(arms)[0], "variant": "variant_other"}),
    ],
    ids=["gap", "extra-seed", "duplicate-cell", "sample-count", "unknown-variant"],
)
def test_a_gap_padding_or_lock_mismatch_ingests_nothing_and_leaves_the_gate_closed(
    tmp_path, rows_fn
):
    world, campaign, arms, lock = _run(tmp_path, rows_fn)
    assert world.env.query("SELECT 1 FROM gate_ingested_rows") == []
    attempt = world.attempts(campaign["id"])[0]
    measured = {m["name"]: m for m in json.loads(attempt["measurements_json"])}
    assert measured["evidence_rows.rows"]["value"] is None
    assert measured["evidence_rows.rows"]["error_class"]
    with pytest.raises(PairedRefused):
        _delta(world, arms, lock, campaign["id"])


def test_ingestion_has_no_human_route_and_the_spec_must_declare_the_output(tmp_path):
    world, campaign, arms, lock = _run(tmp_path)
    attempt = world.attempts(campaign["id"])[0]
    store = ExperimentStore(world.env.state)
    body = {"expected_revision": 1, "output_name": "rows", "artifact_id": "artifact_x"}
    with pytest.raises(Forbidden, match="approved campaign stage"):
        store.ingest_evidence_rows(
            world.env.owner, attempt["admitted_attempt_id"], "k", canonical_digest(body), body
        )


def test_specs_without_evidence_rows_keep_their_output_digest():
    from tessera_control.campaign_models import OutputSpec

    plain = OutputSpec(name="r", path="r.json", max_bytes=10).model_dump(mode="json")
    assert "evidence" not in plain
    grid = {
        "split_lock_id": "l",
        "variants": ["a"],
        "seeds": [0],
        "metrics": {"m": "u"},
        "partitions": ["p"],
    }
    with pytest.raises(ValueError, match="only for, kind evidence_rows"):
        OutputSpec(name="r", path="r.json", kind="metrics", max_bytes=10, evidence=grid)
    with pytest.raises(ValueError, match="evidence is required"):
        OutputSpec(name="r", path="r.json", kind="evidence_rows", max_bytes=10)


def test_ingested_rows_table_exists_and_is_append_only(tmp_path):
    world = _world(tmp_path)
    with _connection(world) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='gate_ingested_rows'"
        ).fetchone()
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='gate_ingested_rows'"
            )
        }
    assert triggers == {"gate_ingested_rows_no_update", "gate_ingested_rows_no_delete"}


def test_a_repeat_run_of_the_same_grid_ingests_cleanly_and_stays_separate(tmp_path):
    world, first, arms, lock = _run(tmp_path)
    assert world.status(first["id"]) == "succeeded", dump(world, first["id"])
    world.scenarios = [
        (
            0,
            {
                "data.json": json.dumps(
                    {
                        "rows": _rows(
                            arms,
                            values={
                                ("baseline", 0): 3.0,
                                ("baseline", 1): 3.0,
                                ("intervention", 0): 1.0,
                                ("intervention", 1): 1.0,
                            },
                        )
                    }
                )
            },
        )
    ]
    second = world.campaign(_spec2(_stage(world.env, lock, arms)))
    world.tick_all(second["id"])
    assert world.status(second["id"]) == "succeeded", dump(world, second["id"])
    assert len(world.env.query("SELECT 1 FROM gate_ingested_rows")) == 8
    # Each campaign's unlock reads only its own rows.
    assert _delta(world, arms, lock, first["id"])["mean"] == pytest.approx(0.45)
    assert _delta(world, arms, lock, second["id"])["mean"] == pytest.approx(2.0)


def test_a_stored_group_refuses_ingested_rows(tmp_path):
    world, campaign, arms, lock = _run(tmp_path)
    with _connection(world) as connection, pytest.raises(PairedRefused) as error:
        compute_paired_delta(
            connection,
            world.env.project["id"],
            arm_a_variant_id=arms["baseline"],
            arm_b_variant_id=arms["intervention"],
            lock_id=lock,
            metric="rmse",
            partition="test",
            allow_ingested=False,
        )
    assert error.value.code == "ingested_rows_not_storable"


def test_a_provider_that_cannot_ingest_rows_is_refused_at_spec_time(tmp_path):
    import dataclasses

    from pydantic import ValidationError

    from tessera_control.campaign_models import parse_spec

    world = _world(tmp_path)
    arms, lock = _arms(world), _lock(world)
    base = worker._descriptor("no-ingest", world.env.worker["id"])
    descriptor = dataclasses.replace(
        base, delegable_steps=base.delegable_steps - {"ingest_evidence_rows"}
    )
    cp.register_provider(worker.WorkerGrantProvider(descriptor))
    try:
        stage = _stage(world.env, lock, arms)
        stage["provider"] = "no-ingest"
        with pytest.raises(ValidationError, match="cannot deliver evidence_rows"):
            parse_spec(_spec2(stage))
    finally:
        cp.unregister_provider("no-ingest")
