"""An artifact input's class is the stricter of row and spec, and reaches the receipt (ADR 0039)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any

import pytest
from campaign_runner_harness import World, dump, stage_spec
from test_campaign_io import _finished, _project, _source
from test_campaign_models_v2 import _spec2

from tessera_control import campaign_runner
from tessera_control.campaign_io import resolve_artifact, stricter_data_class


@pytest.mark.parametrize(
    ("recorded", "declared", "expected"),
    [
        ("public", None, "public"),
        ("public", "private_derived", "private_derived"),
        ("private_derived", None, "private_derived"),
        ("private_derived", "public", "private_derived"),
        ("private_derived", "private_derived", "private_derived"),
        # A recorded class outside the known order (a future, stricter class) is never
        # relabelled to private_derived.
        ("sealed_future", "private_derived", "sealed_future"),
        ("sealed_future", None, "sealed_future"),
        # An unknown declared class is not ranked below a known recorded class.
        ("public", "sealed_future", "sealed_future"),
    ],
)
def test_stricter_data_class(recorded, declared, expected):
    assert stricter_data_class(recorded, declared) == expected


def _relabel_output(world: World, row_id: str, data_class: str) -> None:
    """Set the pinned output row's recorded class (the index is append-only, so lift the
    update trigger for this one write and put it back)."""
    with sqlite3.connect(world.env.state / "tessera.sqlite3") as db:
        db.execute("DROP TRIGGER campaign_stage_io_no_update")
        db.execute("UPDATE campaign_stage_io SET data_class=? WHERE id=?", (data_class, row_id))
        db.execute(
            """CREATE TRIGGER campaign_stage_io_no_update BEFORE UPDATE ON campaign_stage_io
            BEGIN SELECT RAISE(ABORT,'campaign stage io is append-only'); END"""
        )


def test_resolution_keeps_the_row_class_unless_the_spec_is_stricter(tmp_path):
    world = World(tmp_path)
    campaign, row = _finished(world)
    _relabel_output(world, row["id"], "public")
    with world.runner.store.runner_connection() as connection:
        plain = resolve_artifact(connection, _project(world), _source(campaign, row))
        declared = resolve_artifact(
            connection, _project(world), _source(campaign, row, data_class="private_derived")
        )
    # Non-vacuous: the two resolutions differ, so the row class is read and the spec's
    # stricter statement wins over it.
    assert (plain.data_class, declared.data_class) == ("public", "private_derived")


def _io_items(value: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(value, dict):
        if value.get("direction") in ("input", "output") and "data_class" in value:
            found.append(value)
        for item in value.values():
            found += _io_items(item)
    elif isinstance(value, list):
        for item in value:
            found += _io_items(item)
    return found


def _consume(world: World, first: dict, row) -> tuple[dict, dict]:
    """Run a second campaign whose stage pins ``row`` as an artifact input; return the input's
    index row and its item on the receipt."""
    env = world.env
    env.trust()  # the artifact is derived data; bind the worker for private_derived
    stage = {
        "key": "reuse",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": stage_spec(env, "x")["image"],
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            },
            {
                "kind": "artifact",
                "name": "prior",
                "campaign_id": first["id"],
                "stage": "s1",
                "output": row["name"],
                "sha256": row["sha256"],
            },
        ],
    }
    spec = _spec2(stage)
    spec["stages"][0]["provider_config"] = {}
    from tessera_control import campaign_provider_worker as worker

    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    try:
        second = world.campaign(spec)
        world.tick_all(second["id"])
        assert world.status(second["id"]) == "succeeded", dump(world, second["id"])
    finally:
        worker.register_from_config(None)
    indexed = env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='input' "
        "AND name='prior'",
        second["id"],
    )
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", second["id"])[
            "receipt_json"
        ]
    )
    items = [i for i in _io_items(receipt) if i["direction"] == "input" and i["name"] == "prior"]
    assert len(indexed) == 1 and items, "the artifact input must be on the index and the receipt"
    assert all(item["sha256"] == row["sha256"] for item in items)
    return dict(indexed[0]), items


def test_the_resolved_class_not_a_constant_is_carried_onto_the_receipt_input_row(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    first, row = _finished(world)
    real = campaign_runner.resolve_artifact

    def resolved_as_public(connection, campaign, source):
        # A resolver that yields a class other than the runner's default: the receipt must
        # show exactly what resolution returned.
        return dataclasses.replace(real(connection, campaign, source), data_class="public")

    monkeypatch.setattr(campaign_runner, "resolve_artifact", resolved_as_public)
    indexed, items = _consume(world, first, row)
    assert indexed["data_class"] == "public"
    assert {item["data_class"] for item in items} == {"public"}


def test_a_less_restrictive_pinned_row_reaches_the_receipt_as_the_stricter_class(tmp_path):
    world = World(tmp_path)
    first, row = _finished(world)
    _relabel_output(world, row["id"], "public")
    indexed, items = _consume(world, first, row)
    # The spec's (default) private_derived is stricter than the row's public.
    assert indexed["data_class"] == "private_derived"
    assert {item["data_class"] for item in items} == {"private_derived"}
