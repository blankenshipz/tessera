# ruff: noqa: F811
"""The campaign receipt carries a heavy stage's guard refusal, and a Salad candidate refused at
submit says why in structured fields (ADR 0107 amendment 2026-11-23c, ADR 0141 amendment).

Fakes only: the Salad executor is a ``SaladWorld`` test double, the CPU host is a worker-grant
provider with free slots; no provider call, no network, no money. Label names are generic.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_campaign_placement_default import _cpu_world
from test_heavy_placement_data_class import NON_COMMERCIAL, _label, _trust
from test_heavy_stage_remote_candidate import _configure, _stored
from test_heavy_unexplained_local import FAR, far  # noqa: F401
from test_salad_stage_adapter import NEAR, cpu_world, near, spec_of, stage  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_placement as placement
from tessera_control import campaign_placement_preference as preference
from tessera_control import campaign_receipts, program_budget
from tessera_control.campaign_receipts import build_receipt

GIB = 1024**3
CODE = "heavy_local_unexplained"
TAGGED = {"placement_tags": ["heavy"]}
MEMORY = {"alternate_providers": None, "memory_bytes": 2 * GIB}


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


def _receipt(world):
    with world.env.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, row, "running")
    return receipt


def _stage_status(world):
    sql = "SELECT status FROM campaign_stages WHERE campaign_id=?"
    return world.row(sql, world.campaign_id)["status"]


def _single(world, **over):
    stage_ = {k: v for k, v in world.spec["stages"][0].items() if k != "alternate_providers"}
    world.spec = salad_spec_v2(world.env, [{**stage_, **over}])


# -- 1. refused before any attempt: the stage entry carries the refusal ------------------------


def test_a_one_provider_refusal_is_on_the_receipt_stage_entry(tmp_path, near):
    world = _cpu_world(tmp_path, near, TAGGED)
    _single(world)
    world.start()
    world.tick(5)
    assert world.attempt() is None
    (event,) = world.events(guard.REFUSED_EVENT)
    entry = _receipt(world)["stages"][0]
    assert entry["attempts"] == [] and entry["status"] == "failed"
    block = entry["placement"]["heavy_guard"]
    recorded_at = block.pop("recorded_at")
    assert isinstance(recorded_at, str) and recorded_at
    assert block == {
        "warning": False,
        "refused": True,
        "code": CODE,
        "provider_id": NEAR,
        "attempt": 1,
        "mode": "warn",
        "signals": ["tag:heavy"],
        "reasons": ["not_listed"],
        "detail": [],
        "unexplained_local": "refuse",
    }
    assert {k: event[k] for k in ("code", "reasons", "mode", "detail")} == {
        k: block[k] for k in ("code", "reasons", "mode", "detail")
    }


def test_a_placement_path_refusal_is_on_the_receipt_stage_entry(tmp_path, near, far):
    world = _cpu_world(tmp_path, near, TAGGED)
    far(world)
    stage_ = world.spec["stages"][0]
    world.spec = salad_spec_v2(
        world.env,
        [{**stage_, "alternate_providers": [{"provider": FAR, "runtime": "docker-cpu-v1"}]}],
    )
    world.start()
    world.tick(5)
    assert world.attempt() is None
    entry = _receipt(world)["stages"][0]
    block = entry["placement"]["heavy_guard"]
    assert block["code"] == CODE and block["reasons"] == ["not_listed"]
    assert block["refused"] is True and block["attempt"] == 1
    assert block["provider_id"] in (NEAR, FAR)


def test_a_refuse_mode_wait_that_ends_without_an_attempt_is_on_the_receipt(tmp_path, near):
    preference.configure(
        {
            "heavy_remote_candidate": {"provider": "salad", "max_exposure_cents": 1},
            "heavy_local_fallback": "refuse",
        },
        [NEAR, "salad"],
    )
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, **MEMORY)
    world.start()
    world.tick(4)
    assert world.attempt() is None
    waits = [w for w in world.events(placement.WAITING_EVENT) if w.get("heavy_guard")]
    assert waits and waits[-1]["heavy_guard"]["refused"] is True
    # Still waiting: a stage that has not ended carries no refusal on the receipt.
    assert "placement" not in _receipt(world)["stages"][0]
    world.cancel()
    world.tick_until(lambda: _stage_status(world) == "cancelled")
    entry = _receipt(world)["stages"][0]
    assert entry["attempts"] == []
    block = entry["placement"]["heavy_guard"]
    assert block["code"] == guard.REFUSED_REASON and block["mode"] == "refuse"
    assert block["reasons"] == ["remote_candidate_not_added"]
    assert block["detail"] == [
        {"provider_id": "salad", "reason": "over_operator_cap", "source": guard.RECORD_SOURCE}
    ]
    assert block["provider_id"] == NEAR and block["refused"] is True
    assert not world.events(guard.REFUSED_EVENT)


# -- 2. a structured cause for a Salad candidate refused at submit ------------------------------


def _local_attempt_block(world):
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    (event,) = world.events(guard.EVENT)
    attempt = _receipt(world)["stages"][0]["attempts"][0]
    block = attempt["placement"]["heavy_guard"]
    assert block["detail"] == event["detail"]
    return event


def test_a_licence_label_refusal_records_cause_and_labels(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, **MEMORY)
    _trust(world, near, salad_labels=["open"])
    _label(world, [NON_COMMERCIAL])
    world.start()
    record = _stored(world)["stages"][0]["heavy_remote_candidate"]
    assert record["reason"] == "candidate_refused"
    assert record["cause"] == "license_label" and record["labels"] == [NON_COMMERCIAL]
    assert "data_class" not in record
    event = _local_attempt_block(world)
    assert event["reasons"] == ["remote_candidate_not_added"]
    assert event["detail"] == [
        {
            "provider_id": "salad",
            "reason": "candidate_refused",
            "source": guard.RECORD_SOURCE,
            "cause": "license_label",
            "labels": [NON_COMMERCIAL],
        }
    ]
    assert guard.explained(event["reasons"], event["detail"])


def test_a_data_class_refusal_records_cause_and_class(tmp_path, near):
    from campaign_fixture import record_snapshot_private

    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    record_snapshot_private(world.env.state, world.env.snapshot_id, "derived")
    (source,) = stage(world.env)["inputs"]
    _trust(world, near, salad_classes=("public",))
    world.spec = spec_of(world, **MEMORY, inputs=[{**source, "data_class": "private_derived"}])
    world.start()
    record = _stored(world)["stages"][0]["heavy_remote_candidate"]
    assert record["reason"] == "candidate_refused"
    assert record["cause"] == "data_class" and record["data_class"] == "private_derived"
    assert "labels" not in record
    event = _local_attempt_block(world)
    assert event["detail"] == [
        {
            "provider_id": "salad",
            "reason": "candidate_refused",
            "source": guard.RECORD_SOURCE,
            "cause": "data_class",
            "data_class": "private_derived",
        }
    ]


# -- 3. stages without a refusal keep their receipt entries -------------------------------------


def _without_refusals(monkeypatch, world):
    monkeypatch.setattr(campaign_receipts, "recorded_heavy_refusals", lambda *a, **k: {})
    try:
        return _receipt(world)
    finally:
        monkeypatch.undo()


def test_a_placed_heavy_stage_keeps_its_receipt_entry(tmp_path, near, monkeypatch):
    preference.configure(
        {"heavy_remote_candidate": {"provider": "salad", "max_exposure_cents": 1}},
        [NEAR, "salad"],
    )
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, **MEMORY)
    world.start()
    record = _stored(world)["stages"][0]["heavy_remote_candidate"]
    assert record["reason"] == "over_operator_cap"
    assert not {"cause", "labels", "data_class"} & set(record)
    world.tick_until(lambda: world.attempt() is not None)
    with_change = _receipt(world)
    assert "placement" not in with_change["stages"][0]
    assert with_change == _without_refusals(monkeypatch, world)
    (event,) = world.events(guard.EVENT)
    assert event["detail"] == [
        {"provider_id": "salad", "reason": "over_operator_cap", "source": guard.RECORD_SOURCE}
    ]


def test_a_light_stage_keeps_its_receipt_entry(tmp_path, near, monkeypatch):
    world = _cpu_world(tmp_path, near, {"max_duration_seconds": 60})
    _single(world)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    with_change = _receipt(world)
    assert "placement" not in with_change["stages"][0]
    assert with_change == _without_refusals(monkeypatch, world)
