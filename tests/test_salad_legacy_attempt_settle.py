"""An attempt recorded before the move markers existed settles on the guardian's own evidence.

The recorded final was closed by the safety service's provider-confirmed reconcile, but carries
no ``retained_before_move`` and the attempt no move class (it was recorded by an older release).
Control's own read of the guardian record confirms the frame closed; the estimate is finite and
within the hold, so it settles at the rounded-up estimate (at least 1 cent), provisional
(``guardian_bounded_hold``), with no person recording a charge. A missing or over-hold estimate,
an operator's attested reconcile, or a frame not confirmed closed still wait for a charge, a
human charge already posted is never doubled, and replaying changes nothing.

Fakes only (the Salad campaign world and its guardian reader); no network.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_salad_runtime_settle_classify import (
    RECONCILED,
    _attempts,
    _retained_world,
)

from tessera_control.salad_guardian import RECONCILE_OPERATOR


def _settled(world):
    world.tick_until(lambda: world.attempt()["settled_cents"] is not None)
    row = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    return row["settled_cents"], row["settlement_basis"]


def test_a_legacy_provider_closed_attempt_settles_at_the_estimate_without_a_charge(tmp_path):
    world = _retained_world(tmp_path, RECONCILED)
    assert _settled(world) == (4, "guardian_bounded_hold")
    assert world.events("campaign.late_charge") == []
    # The basis is on the attempt's receipt entry, provisional until a charge confirms it.
    world.tick(3)
    assert _settled(world) == (4, "guardian_bounded_hold")  # idempotent
    # Only the settlement changes: the attempt keeps its outcome class (a workload verdict, not
    # an infrastructure move), so there is no retry and no output is accepted.
    world.tick_until(lambda: world.attempt()["status"] not in ("resolved", "running"))
    attempts = _attempts(world)
    assert len(attempts) == 1
    first = attempts[0]
    assert (first["status"], first["failure_class"]) == ("failed_permanent", "salad_not_passed")
    assert first["manifest_id"] is None
    assert world.env.query("SELECT * FROM campaign_stage_io WHERE direction='output'") == []


def test_a_zero_estimate_settles_at_one_cent(tmp_path):
    world = _retained_world(tmp_path, dict(RECONCILED, estimated_exposure_cents="0"))
    assert _settled(world) == (1, "guardian_bounded_hold")


@pytest.mark.parametrize(
    "change",
    [
        dict(estimated_exposure_cents="9999"),  # above the hold
        dict(estimated_exposure_cents=None),
        dict(reason=RECONCILE_OPERATOR),  # an operator's attestation never qualifies
        dict(orphan=True),
        dict(rate_unknown=True),
    ],
)
def test_without_a_usable_estimate_or_provider_close_it_waits_for_a_charge(tmp_path, change):
    world = _retained_world(tmp_path, dict(RECONCILED, **change))
    world.tick_until_phase("awaiting_charge")
    world.tick(3)
    assert world.phase() == "awaiting_charge"
    assert world.attempt()["settled_cents"] is None


def test_a_frame_not_confirmed_closed_waits_for_a_charge(tmp_path):
    world = _retained_world(tmp_path, RECONCILED)
    world.guardian.verdict = "open"
    world.tick_until_phase("awaiting_charge")
    world.tick(3)
    assert world.phase() == "awaiting_charge" and world.attempt()["settled_cents"] is None


def test_a_human_charge_already_posted_is_never_doubled(tmp_path):
    world = _retained_world(tmp_path, RECONCILED)
    world.guardian.verdict = "open"  # not yet confirmed closed: the attempt waits
    world.tick_until_phase("awaiting_charge")
    world.record_charge(2)
    world.guardian.verdict = "stopped"
    world.tick_until(lambda: world.attempt()["settled_cents"] is not None)
    row = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert (row["settled_cents"], row["settlement_basis"]) == (2, "human_charge")
    world.tick(3)
    total = world.env.query(
        "SELECT COALESCE(SUM(amount_cents),0) AS n FROM experiment_budget_entries "
        "WHERE category='compute'"
    )[0]["n"]
    assert total == 2
