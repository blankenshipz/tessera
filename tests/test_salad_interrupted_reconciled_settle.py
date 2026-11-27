"""A confirmed Salad interruption settles at its estimate when the guardian only reconciled.

Field shape: a stage ran, the provider moved its instance after custody began and the
provider's own read confirmed the stop (``salad_interrupted``). The guardian refused the live
check's cancel (``stop_only``) and its final, and control's read of its frame record, said
``reconciled``, not ``stopped`` (the executor journalled ``guardian_not_stopped``). The attempt
waited for a person's charge although the receipt carried a finite accrued estimate within the
hold. It now settles at that estimate (rounded up, at least one cent, within the hold) like a
``stopped`` final; anything that is not clean still waits for a person.

Fakes only (the Salad campaign world, its executor double and a guardian reader double).
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_salad_interruption_retry import FINAL, interrupted, world_for

from tessera_control.campaign_infra_retry import SALAD_INTERRUPTED

RECONCILED = dict(FINAL, status="reconciled", reason="stop_only_recovery")


def _first(world):
    return world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )[0]


def _settled(world) -> bool:
    return _first(world)["settled_cents"] is not None


@pytest.mark.parametrize(
    ("estimate", "cents"),
    [("0.2089", 1), ("3.5", 4), ("0", 1)],
    ids=["field_fraction_rounds_to_one", "rounds_up", "zero_is_at_least_one"],
)
def test_reconciled_final_settles_at_the_estimate_without_a_person(tmp_path, estimate, cents):
    world = world_for(tmp_path)
    interrupted(world, final=dict(RECONCILED, estimated_exposure_cents=estimate))

    world.tick_until(lambda: _first(world)["failure_class"] == SALAD_INTERRUPTED, limit=30)

    first = _first(world)
    assert first["status"] in {"failed_transient", "failed_permanent"}
    assert (first["settled_cents"], first["settlement_basis"]) == (cents, "guardian_bounded_hold")
    # Control read the guardian's own record (``reconciled``) before settling.
    assert world.guardian.reads


def test_the_field_reason_with_allocation_changed_also_settles(tmp_path):
    world = world_for(tmp_path)
    interrupted(world, final=dict(FINAL, status="reconciled"))

    world.tick_until(lambda: _settled(world), limit=30)

    assert _first(world)["settlement_basis"] == "guardian_bounded_hold"


@pytest.mark.parametrize(
    "final",
    [
        # (A non-finite estimate is refused when the outcome is recorded.)
        dict(RECONCILED, estimated_exposure_cents=None),
        dict(RECONCILED, estimated_exposure_cents="999"),  # above the hold
        dict(RECONCILED, orphan=True),
        dict(RECONCILED, escalation_pending=True),
        dict(RECONCILED, budget_fenced=True),
        dict(RECONCILED, rate_unknown=True),
        dict(RECONCILED, actual_billed_cost_cents=3),
        dict(RECONCILED, status="unknown"),
    ],
    ids=[
        "no_estimate",
        "above_hold",
        "orphan",
        "escalation",
        "budget_fenced",
        "rate_unknown",
        "billed_cost_present",
        "status_unknown",
    ],
)
def test_anything_not_clean_still_waits_for_a_person(tmp_path, final):
    world = world_for(tmp_path)
    interrupted(world, final=final)

    world.tick(14)

    assert _first(world)["settled_cents"] is None


def test_control_must_read_the_reconciled_record_itself(tmp_path):
    world = world_for(tmp_path)
    world.guardian.verdict = "open"  # the guardian's own record does not say it is closed
    interrupted(world, final=RECONCILED)

    world.tick(14)

    assert _first(world)["settled_cents"] is None


def test_a_reconciled_final_never_settles_a_run_that_was_not_interrupted(tmp_path):
    """Outside a confirmed interruption a ``reconciled`` final keeps today's rule: no bounded
    hold, a person charges it."""
    from salad_campaign_fixture import SaladWorld

    world = SaladWorld(tmp_path)
    world.to_route_started()
    from test_salad_blocked_route_evidence_settles import _ran_and_stopped, _receipt

    manifest = _ran_and_stopped(world)
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=_receipt(),
            manifest_id=manifest["id"],
            provider_id="provider-group-1",
            guardian_final=RECONCILED,
        ),
    )

    world.tick(14)

    attempt = world.attempt()
    assert attempt["settled_cents"] is None
    assert world.salad()["phase"] == "awaiting_charge"
