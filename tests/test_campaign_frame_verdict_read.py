"""ADR 0041 stage 2: a Salad frame settles on a verdict control read from the guardian itself.

The executor's recorded final supplies the cost estimate, never the verdict. Fakes only: the
guardian is a reader double, no provider, network or credential."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_settlement import CLEAN, run


def _frame(world):
    return world.row("SELECT * FROM execution_frames WHERE kind='stage'")


def _reads(world):
    return world.env.query("SELECT * FROM execution_frame_guardian_records ORDER BY sequence")


def _settled(world) -> bool:
    return world.attempt()["settled_cents"] is not None


def test_a_clean_read_settles_the_bounded_hold_and_records_the_read_digest(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    attempt = world.attempt()
    assert attempt["settlement_basis"] == "guardian_bounded_hold"
    assert attempt["settled_cents"] == HOLD
    reads = _reads(world)
    assert reads and reads[-1]["verdict"] == "stopped"
    frame = _frame(world)
    assert frame["guardian_verdict_digest"] == reads[-1]["record_digest"]
    assert frame["guardian_reservation_id"] == reads[-1]["guardian_reservation_id"]
    assert world.guardian.reads and set(world.guardian.reads) == {frame["guardian_reservation_id"]}


def test_the_receipt_carries_the_guardian_record_and_where_the_verdict_came_from(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entry = receipt["stages"][0]["attempts"][0]
    record = entry["guardian_record"]
    assert record["verdict"] == "stopped" and record["verdict_source"] == "guardian_read"
    assert record["record_digest"] == _frame(world)["guardian_verdict_digest"]


@pytest.mark.parametrize("failure", ["no_reader", "unreachable", "open", "malformed", "foreign"])
def test_no_usable_read_means_nothing_settles_even_with_a_clean_worker_final(tmp_path, failure):
    world = SaladWorld(tmp_path)
    if failure == "no_reader":
        from tessera_control.campaign_provider_salad import register_provider_instance

        register_provider_instance(world.profiles)  # the provider with no guardian reader
    elif failure == "unreachable":
        world.guardian.unreachable = True
    elif failure == "open":
        world.guardian.verdict = "open"  # the worker says stopped; the guardian record does not
    elif failure == "malformed":
        world.guardian.answer = lambda reservation: {"reservation": reservation, "verdict": "ok"}
    else:
        world.guardian.answer = lambda reservation: {
            "reservation": "f" * 64,
            "verdict": "stopped",
            "sequence": 1,
            "record_digest": "a" * 64,
        }
    run(world)
    world.tick(10)
    assert not _settled(world)
    assert world.attempt()["settlement_basis"] is None
    frame = _frame(world)
    assert frame is None or frame["guardian_verdict_digest"] is None
    assert world.status() not in {"succeeded"}
    held = world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)
    assert held["held_cents"] == HOLD


def test_a_reconciled_or_absent_verdict_is_recorded_but_does_not_bound_the_hold(tmp_path):
    world = SaladWorld(tmp_path)
    world.guardian.verdict = "reconciled"
    run(world)
    world.tick(10)
    assert not _settled(world)
    assert {row["verdict"] for row in _reads(world)} == {"reconciled"}
    assert _frame(world)["guardian_verdict_digest"] is None


def test_the_read_is_retried_and_settles_once_the_guardian_says_stopped(tmp_path):
    world = SaladWorld(tmp_path)
    world.guardian.verdict = "open"
    run(world)
    world.tick(6)
    assert not _settled(world)
    world.guardian.verdict = "stopped"
    world.guardian.sequence += 1
    world.tick_until(lambda: world.status() == "succeeded")
    assert world.attempt()["settlement_basis"] == "guardian_bounded_hold"
    assert [row["verdict"] for row in _reads(world)][-1] == "stopped"


def test_a_repeated_read_adds_no_second_row(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    count = len(_reads(world))
    from tessera_control import execution_frames as frames

    frame = _frame(world)
    last = _reads(world)[-1]
    with world.runner.store.runner_connection() as connection:
        again = frames.record_guardian_read(
            connection,
            frame["frame_id"],
            frames.Tenant(frame["org_id"], frame["project_id"]),
            verdict=last["verdict"],
            record_sequence=last["record_sequence"],
            record_digest=last["record_digest"],
            now="2026-11-22T00:00:00Z",
        )
    assert again["sequence"] == last["sequence"]
    assert len(_reads(world)) == count


def test_a_clean_worker_final_with_a_non_stopped_status_still_needs_a_human(tmp_path):
    world = SaladWorld(tmp_path)
    run(world, final={**CLEAN, "status": "reconciled"})
    world.tick(8)
    assert not _settled(world)
