# ruff: noqa: F811
"""A run whose cost would exceed its per-run cap is refused, fail closed, and the refusal is on
the receipt (README: placement only uses a candidate "within the per-run cap"; "each skipped
candidate's reason [is] on the receipt").

Drives the real runner in process against the Salad placement fixture (a ``near-cpu`` own
provider with a Salad CPU alternate). Fakes only: no provider, no network, no money.
"""

from __future__ import annotations

import dataclasses
import json

from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_campaign_placement_default import GIB, _cpu_world
from test_campaign_placement_salad import NEAR, near  # noqa: F401

from tessera_control import campaign_placement as placement


def _every_candidate_over_the_cap(monkeypatch) -> None:
    # Submit already refuses a candidate whose estimate exceeds the stage's approved cap, so the
    # condition is reached at run time by making every candidate cost one cent more than the
    # per-run cap (the stage's own max_exposure_cents) when the real runner decides.
    real = placement.stage_on_candidate

    def over_cap(base, provider_id):
        stage = real(base, provider_id)
        return dataclasses.replace(stage, max_exposure_cents=base.max_exposure_cents + 1)

    monkeypatch.setattr(placement, "stage_on_candidate", over_cap)


def _receipt(world) -> dict:
    row = world.row(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
    )
    assert row is not None, "a terminal campaign has a receipt"
    return json.loads(row["receipt_json"])


def test_a_run_over_its_per_run_cap_is_refused_and_the_refusal_is_on_the_receipt(
    tmp_path, near, monkeypatch
):
    world = _cpu_world(tmp_path, near, {"memory_bytes": 2 * GIB})
    _every_candidate_over_the_cap(monkeypatch)
    world.start()
    world.tick_until(lambda: world.status() in {"failed", "stopped"})

    # Fail closed: no attempt was reserved, nothing held, settled, granted or routed.
    assert world.status() == "failed"
    assert world.attempt() is None
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None
    money = world.row(
        "SELECT held_cents, settled_cents FROM campaigns WHERE id=?", world.campaign_id
    )
    assert (money["held_cents"], money["settled_cents"]) == (0, 0)
    assert world.row("SELECT 1 FROM execution_grants") is None

    receipt = _receipt(world)
    assert receipt["status"] == "failed"
    assert receipt["stop_reason"] == "stage_failed:g1:no_eligible_provider"
    assert receipt["budget"]["held"] == 0 and receipt["budget"]["settled"] == 0
    (stage,) = receipt["stages"]
    assert stage["status"] == "failed" and stage["attempts"] == []
    # The refusal itself, with each candidate's per-run cap reason, is on the receipt.
    refused = stage["placement_refused"]
    assert refused["attempt"] == 1
    assert refused["candidates"] == ["salad", NEAR]
    skips = {item["provider_id"]: item for item in refused["skipped"]}
    assert set(skips) == {"salad", NEAR}
    for item in skips.values():
        assert item["reason"] == "over_run_cap"
        before, after = item["detail"].split(" > ")
        assert int(before) == int(after) + 1


def test_a_run_within_its_per_run_cap_is_placed_without_a_refusal(tmp_path, near):
    # Control: the same world with no over-cap candidate is placed (on Salad, the default for a
    # heavy stage) and records no refusal.
    world = _cpu_world(tmp_path, near, {"memory_bytes": 2 * GIB})
    world.start()
    world.tick_until(lambda: bool(world.events(placement.PLACED_EVENT)))
    (placed,) = world.events(placement.PLACED_EVENT)
    assert placed["provider_id"] == "salad"
    assert all(item["reason"] != "over_run_cap" for item in placed["skipped"])
    assert placed["hold_cents"] <= 20
    assert world.status() == "running"
    assert not world.events(placement.WAITING_EVENT)
