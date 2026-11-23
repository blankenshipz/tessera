# ruff: noqa: F811
"""Control records a Salad stage's public fetch from its retained output.json (ADR 0097).

The runtime puts the rows it fetched into ``output.json`` under ``fetched``. After the attempt
succeeds and that output is retained, the runner asks the platform to read the rows itself and
check them against the frozen pins; the recorded event is what the receipt reads. A row that
contradicts the pins records nothing (the receipt stays ``declared_only``) and the passed stage
is unaffected. Fakes only: no Salad, Docker or network call is made.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_heavy_download_on_provider import HOST, SHA, SIZE, URL, _trust, inputs, public
from test_salad_stage_adapter import cpu_world as _cpu_world
from test_salad_stage_adapter import near, spec_of  # noqa: F401

from tessera_control import campaign_placement_preference as preference
from tessera_control import program_budget
from tessera_control.campaign_receipts import build_receipt
from tessera_control.experiments import ExperimentStore
from tessera_control.provider_fetch_record import check_fetched_rows
from tessera_control.public_fetch_report import EVENT_TYPE
from tessera_control.store import Conflict, Forbidden, canonical_digest

ARGV = ["python", "/inputs/main.py", "--weights=/inputs-public/weights/w.bin", "/outputs"]
GOOD = {"url": URL, "sha256": SHA, "bytes": 4000, "seconds": 1.25, "cache": "off"}


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


def _output(fetched: Any) -> bytes:
    return json.dumps({"score": 1, "fetched": fetched}, sort_keys=True).encode()


def _finish(world, content: bytes) -> None:
    """The executor double of ``SaladWorld.run_to_outcome`` with a chosen output.json."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    manifest = world.upload_manifest(content)
    world.terminate_route()
    world.observe_exit()
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "status": "passed",
        "reason": "verified_journey",
        "stop_confirmed": True,
        "outputs": [
            {
                "name": "output.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        ],
    }
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=receipt,
            manifest_id=manifest["id"],
            provider_id="provider-group-1",
            estimated_exposure={"estimated_exposure_cents": 7},
        ),
    )


def _run(tmp_path, near, content: bytes, *, pinned: bool = True):
    world = _cpu_world(tmp_path, near, slots=0)
    _trust(world, near)
    extra = [public()] if pinned else []
    world.spec = spec_of(world, argv=ARGV, inputs=inputs(world, *extra))
    world.to_route_started()
    _finish(world, content)
    world.tick_until(
        lambda: (
            world.attempt()["status"] in ("measured", "reconciled", "succeeded")
            or world.phase() == "awaiting_charge"
        )
    )
    world.tick(3)
    return world


def _public_fetch(world) -> dict[str, Any] | None:
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, campaign["status"])
    finally:
        connection.close()
    found = []

    def walk(value):
        if isinstance(value, dict):
            if "public_fetch" in value:
                found.append(value["public_fetch"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(receipt)
    return found[-1] if found else None


def _intent(world) -> dict[str, Any] | None:
    return world.row(
        "SELECT status, error_class FROM campaign_step_intents WHERE step='record_provider_fetch'"
    )


def test_control_records_bytes_and_seconds_from_the_retained_output(tmp_path, near):
    world = _run(tmp_path, near, _output([GOOD]))
    assert world.attempt()["salad_verdict"] == "ok", world.dump()
    assert len(world.events(EVENT_TYPE)) == 1, world.dump()
    (event,) = world.events(EVENT_TYPE)
    assert event["operation_id"] == world.attempt()["operation_id"]
    assert event["files"] == [
        {
            "input": "weights",
            "name": "w.bin",
            "source_host": HOST,
            "sha256": SHA,
            "bytes": 4000,
            "seconds": 1.25,
            "cached": None,
            "cache": "off",
        }
    ]
    assert event["via_control"] is False and event["source"] == "retained_output"
    assert event["provider_id"] == "salad" and event["hosts"] == [HOST]
    assert _intent(world)["status"] == "done"
    entry = _public_fetch(world)
    (item,) = entry["files"]
    assert item["evidence"] == "provider_reported"
    assert (item["bytes"], item["seconds"], item["sha256"]) == (4000, 1.25, SHA)
    assert "cache" not in item  # the spec never asked for the cache: the shape is unchanged


@pytest.mark.parametrize(
    "fetched",
    [
        [{**GOOD, "sha256": "d" * 64}],
        [GOOD, GOOD],
        [],
        [{**GOOD, "bytes": SIZE + 1}],
        [{**GOOD, "url": "https://other.example.org/weights/w.bin"}],
        [{**GOOD, "cache": "hit"}],
        [{**GOOD, "extra": 1}],
        "not-a-list",
    ],
    ids=["digest", "extra", "missing", "over-max", "host", "cache", "key", "shape"],
)
def test_a_row_that_contradicts_the_pins_records_nothing_and_the_stage_still_passes(
    tmp_path, near, fetched
):
    world = _run(tmp_path, near, _output(fetched))
    assert world.attempt()["salad_verdict"] == "ok", world.dump()
    assert world.events(EVENT_TYPE) == []
    intent = _intent(world)
    assert intent["status"] == "failed" and intent["error_class"]
    assert world.attempt()["failure_class"] is None, world.dump()
    (item,) = _public_fetch(world)["files"]
    assert item["evidence"] == "declared_only" and item["bytes"] is None


def test_recording_is_idempotent_and_has_no_human_route(tmp_path, near):
    world = _run(tmp_path, near, _output([GOOD]))
    attempt = world.attempt()
    manifest = world.row(
        "SELECT artifacts_json FROM artifact_manifests WHERE id=?", attempt["manifest_id"]
    )
    (artifact,) = json.loads(manifest["artifacts_json"])
    store = ExperimentStore(world.env.state)
    body = {"expected_revision": 1, "artifact_id": artifact["id"]}
    with pytest.raises(Forbidden, match="approved stage"):
        store.record_provider_fetch(
            world.human, attempt["admitted_attempt_id"], "k", canonical_digest(body), body
        )
    # A second runner pass never records a second event for the operation.
    world.tick(3)
    assert len(world.events(EVENT_TYPE)) == 1


def test_a_stage_without_pins_records_no_event_and_keeps_its_receipt(tmp_path, near):
    world = _run(tmp_path, near, b'{"score": 1}', pinned=False)
    assert world.attempt()["salad_verdict"] == "ok", world.dump()
    assert world.events(EVENT_TYPE) == []
    assert _intent(world) is None
    assert _public_fetch(world) is None


PINS = [
    {"input": "a", "name": "x", "url": URL, "sha256": SHA, "max_bytes": SIZE, "cache": False},
    {"input": "b", "name": "y", "url": URL, "sha256": SHA, "max_bytes": SIZE, "cache": True},
]


def test_rows_match_pins_as_a_multiset_and_keep_the_cache_outcome():
    rows = [GOOD, {**GOOD, "cache": "unavailable"}]
    files = check_fetched_rows(PINS, rows)
    assert [(f["input"], f["cache"], f["cached"]) for f in files] == [
        ("a", "off", None),
        ("b", "unavailable", None),
    ]
    files = check_fetched_rows(PINS, [GOOD, {**GOOD, "cache": "hit"}])
    assert files[1]["cached"] is True
    with pytest.raises(Conflict):
        check_fetched_rows(PINS, [GOOD, GOOD])  # the second pin asked for the cache
    with pytest.raises(Conflict):
        check_fetched_rows(PINS, [GOOD])
    with pytest.raises(Conflict):
        check_fetched_rows(PINS[:1], [{**GOOD, "bytes": True}])
    with pytest.raises(Conflict):
        check_fetched_rows(PINS[:1], [{**GOOD, "seconds": float("inf")}])
    with pytest.raises(Forbidden):
        check_fetched_rows([], [])
