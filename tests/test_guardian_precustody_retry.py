"""A guardian-observed pre-custody reallocation is retryable though the route never flagged it.

The shape of a live smoke: the route state says ``reallocated=False`` and the provider's stop and
the exited worker are confirmed, but the live receipt carries the guardian's own pre-custody word
(ADR 0179 addendum). Real stores and runner, executor and human played by the test."""

from __future__ import annotations

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.campaign_infra_retry import REALLOCATED_PRECUSTODY, reallocation_attempts

GUARDIAN = dict(
    reason=REALLOCATED_PRECUSTODY,
    guardian_stop_cause="allocation_changed",
    custody_work_begun=False,
    provider_stop_confirmed=True,
    stop_confirmed=False,
)


def _stages(attempts):
    return lambda env: [salad_stage(env, "g1", attempts=attempts)]


def _bounded(env):
    stage = salad_stage(env, "g1", attempts=1)
    stage["retry"]["max_reallocation_retries"] = 0
    return [stage]


def _first_outcome(world, **extra):
    world.to_route_started()
    world.run_to_outcome(passed=False, outputs=False, receipt_extra=extra)


def test_guardian_evidence_with_a_false_route_flag_is_retried_once(tmp_path):
    world = SaladWorld(tmp_path, stages=_stages(1))
    _first_outcome(world, **GUARDIAN)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(4)
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    first = world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == REALLOCATED_PRECUSTODY
    assert "signal=guardian_evidence" in first["failure_detail"]
    attempts = world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=?", world.campaign_id
    )
    assert reallocation_attempts(attempts) == 1  # the runner's own counter


@pytest.mark.parametrize(
    "change",
    [
        dict(custody_work_begun=True),  # post-custody
        dict(custody_work_begun=None),  # unknown
        dict(guardian_stop_cause="deadline"),
        dict(provider_stop_confirmed=False),
        dict(reason="provider_stopped_custody_unsealed"),
    ],
)
def test_post_custody_unknown_or_other_evidence_stays_permanent(tmp_path, change):
    world = SaladWorld(tmp_path, stages=_stages(1))
    _first_outcome(world, **{**GUARDIAN, **change})
    world.tick_until_phase("awaiting_charge")
    world.record_charge(4)
    world.tick_until(lambda: world.status() == "failed")
    failed = world.attempt()
    assert failed["status"] == "failed_permanent"
    assert failed["failure_class"] == "salad_not_passed"
    assert world.attempt()["attempt"] == 1


def test_the_retry_is_bounded_by_max_reallocation_retries(tmp_path):
    world = SaladWorld(tmp_path, stages=_bounded)
    _first_outcome(world, **GUARDIAN)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(4)
    world.tick_until(lambda: world.status() == "failed")
    assert world.attempt()["attempt"] == 1
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert stop["stop_reason"] == f"stage_failed:g1:{REALLOCATED_PRECUSTODY}_exhausted"
