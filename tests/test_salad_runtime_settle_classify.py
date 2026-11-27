"""Salad runtime: bounded settlement of a provider move, safety-stop class, retained-then-moved.

Fakes only (the Salad campaign world, its guardian reader and executor doubles; no network).
Covers the guardian-final shapes control settles and classifies from: a provider-confirmed
reconcile after a route-flagged reallocation, a clean stop caused by the safety plane, and a run
that retained its output before the provider moved it.
"""

from __future__ import annotations

import typing

import pytest
import test_salad_profile_fallback as fallback
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_executor import RECEIPT
from test_salad_never_allocated import _reallocated_world

from tessera_control import campaign_report, campaign_runner
from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_REALLOCATED,
    INFRA_SALAD_SAFETY_STOP,
    PLACEMENT_RETRY_CLASSES,
    SAFETY_STOP_CAUSES,
)
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_salad_commands import _GuardianFinal
from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.salad_guardian import RECONCILE_OPERATOR, RECONCILE_PROVIDER

CLEAN_STOPPED = dict(
    status="stopped",
    reason="control_cancel",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
    observed_at="2026-11-19T00:00:00Z",
)
RECONCILED = dict(
    CLEAN_STOPPED,
    status="reconciled",
    reason=RECONCILE_PROVIDER,
    allocation_uncertain=True,
)
UNSEALED = dict(
    RECEIPT,
    status="inconclusive",
    reason="provider_stopped_custody_unsealed",
    stop_confirmed=False,
)


def _moved_world(tmp_path, final, *, infra=1, receipt=None, guardian_final=True):
    """A route-flagged provider reallocation whose outcome carries ``final`` as the guardian
    final (and ``receipt`` when given); the hold is the world's own."""
    world = _reallocated_world(tmp_path, infra_retries=infra)
    world.to_route_started()
    original = fallback._record_outcome

    def record(w):
        row = w.salad()
        payload = dict(
            route_id=row["route_id"],
            live_receipt=receipt or dict(RECEIPT, status="inconclusive", stop_confirmed=False),
            provider_id="provider-group-1",
        )
        if guardian_final:
            payload["guardian_final"] = final
        w.env.store.record_salad_outcome(w.worker, row["attempt_id"], w.key("outcome"), payload)

    fallback._record_outcome = record
    try:
        fallback._ran_unstable(world, within=True)
    finally:
        fallback._record_outcome = original
    return world


def _attempts(world):
    return world.env.query("SELECT * FROM campaign_stage_attempts ORDER BY attempt")


def _first(world):
    return world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")


def _retried_without_charge(world):
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _first(world)
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == INFRA_SALAD_REALLOCATED
    assert first["salad_verdict"] != "ok"
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert _attempts(world)[1]["attempt"] == 2
    return first


# -- ADR 0207 item 1: a provider-confirmed move settles at the guardian's estimate


def test_a_provider_confirmed_reallocation_settles_at_the_estimate_and_retries_without_a_charge(
    tmp_path,
):
    world = _moved_world(tmp_path, RECONCILED)
    _retried_without_charge(world)


@pytest.mark.parametrize(
    "change",
    [
        dict(estimated_exposure_cents="9999"),  # above the hold
        dict(estimated_exposure_cents=None),
        dict(reason=RECONCILE_OPERATOR),  # an operator's attestation never qualifies
        dict(orphan=True),
        dict(budget_fenced=True),
        dict(rate_unknown=True),
        dict(escalation_pending=True),
    ],
)
def test_anything_short_of_the_providers_own_confirmed_close_waits_for_a_charge(tmp_path, change):
    world = _moved_world(tmp_path, dict(RECONCILED, **change))
    world.tick_until_phase("awaiting_charge")
    world.tick(3)
    assert world.phase() == "awaiting_charge"
    assert world.attempt()["settled_cents"] is None
    assert len(_attempts(world)) == 1


def test_a_guardian_record_that_is_still_open_never_settles_the_move(tmp_path):
    world = _moved_world(tmp_path, RECONCILED)
    world.guardian.verdict = "open"
    world.tick_until_phase("awaiting_charge")
    world.tick(3)
    assert world.phase() == "awaiting_charge" and world.attempt()["settled_cents"] is None


def test_a_later_charge_above_the_estimate_is_added_as_a_late_charge(tmp_path):
    world = _moved_world(tmp_path, RECONCILED)
    first = _retried_without_charge(world)
    world.record_charge(first["settled_cents"] + 6)
    world.tick_until(lambda: world.events("campaign.late_charge") != [])
    assert world.events("campaign.late_charge")[0]["delta_cents"] == 6


# -- ADR 0207 item 3: a safety-plane stop is infrastructure


def _safe_world(tmp_path, *, infra=1) -> SaladWorld:
    stage = lambda env: salad_stage(env, retry={"max_attempts": 1, "infra_retries": infra})  # noqa: E731
    return SaladWorld(tmp_path, stages=lambda env: [stage(env)])


def test_the_class_is_known_to_the_runner_the_report_and_the_placement_retry():
    assert INFRA_SALAD_SAFETY_STOP in FAILURE_CLASSES
    assert INFRA_SALAD_SAFETY_STOP in campaign_runner._FAILURE_CLASSES
    assert INFRA_SALAD_SAFETY_STOP in campaign_report._FAILURES
    assert INFRA_SALAD_SAFETY_STOP in PLACEMENT_RETRY_CLASSES


@pytest.mark.parametrize("cause", sorted(SAFETY_STOP_CAUSES))
def test_a_safety_stop_is_retried_as_infrastructure_and_settles_the_whole_hold(tmp_path, cause):
    world = _safe_world(tmp_path)
    world.to_route_started()
    world.run_to_outcome(
        passed=False, outputs=False, guardian_final=dict(CLEAN_STOPPED, safety_stop_cause=cause)
    )
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_SALAD_SAFETY_STOP,
    )
    assert cause in first["failure_detail"]
    assert first["salad_verdict"] != "ok"
    assert first["settlement_basis"] == "guardian_bounded_hold"
    assert first["settled_cents"] == first["hold_cents"]


def test_with_no_infra_retry_left_a_safety_stop_fails_under_its_own_class(tmp_path):
    world = _safe_world(tmp_path, infra=0)
    world.to_route_started()
    world.run_to_outcome(
        passed=False,
        outputs=False,
        guardian_final=dict(CLEAN_STOPPED, safety_stop_cause="heartbeat_silence"),
    )
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_SAFETY_STOP,
    )
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert "salad_not_passed" not in stop["stop_reason"]
    assert len(_attempts(world)) == 1


def test_a_cause_only_on_the_receipt_never_selects_the_safety_stop_class(tmp_path):
    world = _safe_world(tmp_path, infra=0)
    world.to_route_started()
    world.run_to_outcome(
        passed=False,
        outputs=False,
        guardian_final=CLEAN_STOPPED,
        receipt_extra=dict(guardian_stop_cause="heartbeat_silence"),
    )
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert attempt["failure_class"] == "salad_not_passed"


STATUS = dict(
    status="stopped",
    reason="x",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
)


@pytest.mark.parametrize("cause", sorted(SAFETY_STOP_CAUSES))
def test_the_projection_carries_a_safety_stop_cause(cause):
    assert project_guardian_final(dict(STATUS, stop_cause=cause))["safety_stop_cause"] == cause


@pytest.mark.parametrize(
    "cause", ["cost_cap", "control_cancel", "deadline", "workload_failed:x", None]
)
def test_the_projection_omits_every_cause_outside_the_closed_table(cause):
    projected = project_guardian_final(dict(STATUS, stop_cause=cause))
    assert projected is not None and "safety_stop_cause" not in projected


def test_the_recorded_final_accepts_exactly_the_closed_set_of_causes():
    annotation = _GuardianFinal.model_fields["safety_stop_cause"].annotation
    literals = [a for a in typing.get_args(annotation) if typing.get_origin(a) is typing.Literal]
    assert len(literals) == 1
    assert set(typing.get_args(literals[0])) == SAFETY_STOP_CAUSES


# -- retained, then moved by the provider (the live field shape) -----------------------------


def _retained(**over):
    return dict(RECONCILED, retained_before_move=True, **over)


def _retained_world(tmp_path, final):
    """No route flag and no provider read of a move: only the guardian's recorded final and the
    receipt (custody unsealed, no manifest) say what happened."""
    world = _safe_world(tmp_path)
    world.to_route_started()
    world.run_to_outcome(
        passed=False,
        outputs=False,
        guardian_final=final,
        receipt_extra=dict(reason=UNSEALED["reason"]),
    )
    return world


def test_a_run_retained_before_the_provider_moved_it_is_retried_and_settles_the_estimate(tmp_path):
    world = _retained_world(tmp_path, _retained())
    _retried_without_charge(world)


def test_a_retained_run_on_a_route_flagged_move_is_retried_the_same_way(tmp_path):
    world = _moved_world(tmp_path, _retained(), receipt=UNSEALED)
    _retried_without_charge(world)


def test_the_projection_carries_retained_before_move_only_for_the_provider_close():
    base = dict(STATUS, status="reconciled", reason=RECONCILE_PROVIDER, allocation_uncertain=True)
    assert project_guardian_final(dict(base, output_retained=True))["retained_before_move"] is True
    for status in (
        dict(base, output_retained=True, reason=RECONCILE_OPERATOR),
        dict(base),
        dict(base, output_retained=False),
        dict(base, output_retained=True, allocation_uncertain=False),
        dict(base, output_retained=True, status="stopped"),
    ):
        projected = project_guardian_final(status)
        assert projected is None or "retained_before_move" not in projected


def test_the_guardian_reports_retained_output_only_when_complete_was_the_first_stop(tmp_path):
    from tessera_control.salad_custody import CustodyBinding, SaladCustody
    from tessera_control.salad_guardian import (
        GuardianAssignment,
        ProviderObservation,
        SaladGuardian,
    )

    def build(name, operation):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        binding = CustodyBinding("tenant", "project", operation, 1, "artifact")
        clock = [100.0]
        custody = SaladCustody(root / "custody.db", clock=lambda: clock[0])
        custody.register(binding, byte_cap=10, file_cap=1)
        assignment = GuardianAssignment(
            binding, "org", "proj", "group-id", "attempt-op", "alloc", "sha256:image", 200
        )

        def observe(a):
            return ProviderObservation(
                a.organization,
                a.provider_project,
                a.group_id,
                a.group_name,
                a.allocation_id,
                a.image_digest,
                "running",
                clock[0],
            )

        guardian = SaladGuardian(
            root / "guardian.db",
            custody=custody,
            observe=observe,
            stop=lambda a: "pending",
            clock=lambda: clock[0],
        )
        guardian.arm(assignment)
        return guardian

    done = build("done", "operation")
    assert "output_retained" not in done.status("operation")
    done.complete("operation")
    assert done.status("operation")["output_retained"] is True
    cancelled = build("cancelled", "operation")
    cancelled.cancel("operation")
    cancelled.complete("operation")
    assert "output_retained" not in cancelled.status("operation")
