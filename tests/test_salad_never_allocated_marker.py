"""A route that ended with no machine ever placed is retryable infrastructure (ADR 0225).

The guardian's closed frame carries ``never_allocated`` only when its own database says no machine
was placed, none seen running and (by the adapter's positive word) no custody began, and the frame
ended at the route limit or by the provider's own stop. Control retries it within
``retry.infra_retries`` and settles it at the guardian's estimate, never above the hold. Any
sighting, an unknown answer, or another recorded cause keeps today's behaviour. Fakes only."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_settlement import CLEAN, run
from test_salad_never_allocated import step
from test_salad_never_allocated import world as guardian_world  # noqa: F401

from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_NEVER_ALLOCATED,
    PLACEMENT_RETRY_CLASSES,
    is_infra_class,
)
from tessera_control.campaign_salad import SaladStageDriver, _never_allocated, _never_marked
from tessera_control.campaign_salad_commands import _GuardianFinal
from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.salad_guardian import NEVER_ALLOCATED_CAUSES, RECONCILE_PROVIDER
from tessera_control.salad_safety_service import SaladSafetyService

# -- the guardian says it -------------------------------------------------------------------


def _provider_ends_the_group(seen, clock, guardian, seconds=30):
    """The provider stopped the group itself: a terminal read, no guardian stop decision."""
    seen.update(state="stopped")
    return step(guardian, clock, seconds)


def test_a_provider_stop_of_a_group_that_never_showed_a_machine_is_reported(guardian_world):  # noqa: F811
    clock, seen, stops, make = guardian_world
    guardian = make(provisioning_seconds=None)
    status = _provider_ends_the_group(seen, clock, guardian)
    assert status["status"] in {"stopped", "reconciled"}
    assert status["never_allocated"] is True
    assert "allocation_timeout" not in status and "route_deadline" not in status


def test_a_placed_machine_is_never_reported_never_allocated(guardian_world):  # noqa: F811
    clock, seen, stops, make = guardian_world
    guardian = make(provisioning_seconds=None)
    seen.update(count=1, started=True)
    step(guardian, clock, 10)
    status = _provider_ends_the_group(seen, clock, guardian)
    assert "never_allocated" not in status


@pytest.mark.parametrize("custody", [True, None])
def test_begun_or_unknown_custody_is_never_reported_never_allocated(guardian_world, custody):  # noqa: F811
    clock, seen, stops, make = guardian_world
    seen["custody"] = custody
    guardian = make(provisioning_seconds=None)
    status = _provider_ends_the_group(seen, clock, guardian)
    assert "never_allocated" not in status


def test_reads_with_no_definite_count_never_give_the_marker(guardian_world):  # noqa: F811
    """N1: an adapter that cannot say how many instances exist says nothing about placement."""
    clock, seen, stops, make = guardian_world
    seen.update(count=None, started=None)
    guardian = make(provisioning_seconds=None)
    status = _provider_ends_the_group(seen, clock, guardian)
    assert status["status"] in {"stopped", "reconciled"}
    assert "never_allocated" not in status


def test_one_read_without_a_count_disqualifies_the_marker(guardian_world):  # noqa: F811
    clock, seen, stops, make = guardian_world
    guardian = make(provisioning_seconds=None)
    step(guardian, clock, 10)  # definite zero counts
    seen.update(count=None)
    step(guardian, clock, 10)  # one read with no count
    seen.update(count=0)
    status = _provider_ends_the_group(seen, clock, guardian)
    assert "never_allocated" not in status


def test_reads_that_all_failed_give_no_marker(guardian_world):  # noqa: F811
    clock, seen, stops, make = guardian_world
    guardian = make(provisioning_seconds=None)
    seen["fail"] = True
    step(guardian, clock, 20)
    seen.update(fail=False, count=None)
    status = _provider_ends_the_group(seen, clock, guardian)
    assert "never_allocated" not in status


def test_the_allocation_timeout_keeps_its_own_record(guardian_world):  # noqa: F811
    clock, seen, stops, make = guardian_world
    guardian = make()
    status = step(guardian, clock, 400)
    assert status["stop_cause"] == "allocation_timeout"
    assert "never_allocated" not in status and "allocation_timeout" in status


# -- the projection and the stored final -----------------------------------------------------

MARKED = dict(CLEAN, stop_cause=None, never_allocated=True)


def test_the_projection_keeps_only_a_closed_marker_of_a_closed_frame():
    assert project_guardian_final(MARKED)["never_allocated"] is True
    assert "never_allocated" not in project_guardian_final(dict(MARKED, never_allocated=1))
    assert "never_allocated" not in project_guardian_final(dict(MARKED, status="armed"))
    assert "never_allocated" not in project_guardian_final(dict(MARKED, stop_cause="cost_cap"))
    # F1: a provider-reported allocation change is never a never-placed group.
    assert "never_allocated" not in project_guardian_final(
        dict(MARKED, stop_cause="allocation_changed")
    )
    assert project_guardian_final(dict(MARKED, stop_cause="deadline"))["never_allocated"] is True
    assert "never_allocated" not in project_guardian_final(dict(CLEAN))


def test_the_stored_final_keeps_its_shape_without_the_marker():
    from tessera_control.campaign_salad_commands import _stored_final

    plain = _GuardianFinal.model_validate(project_guardian_final(dict(CLEAN)))
    assert "never_allocated" not in json.loads(_stored_final(plain))
    marked = _GuardianFinal.model_validate(project_guardian_final(MARKED))
    assert json.loads(_stored_final(marked))["never_allocated"] is True
    with pytest.raises(ValueError):
        _GuardianFinal.model_validate(dict(project_guardian_final(CLEAN), never_allocated=False))


def test_only_the_recorded_marker_counts_for_settlement():
    stored = project_guardian_final(MARKED)
    assert _never_allocated({"guardian_final_json": json.dumps(stored)})
    contradicted = dict(stored, start_events={"outcome": "read"})
    assert not _never_allocated({"guardian_final_json": json.dumps(contradicted)})
    cut = dict(stored, route_deadline={"running_seconds": 900, "limit": "route_deadline"})
    assert not _never_allocated({"guardian_final_json": json.dumps(cut)})
    assert not _never_allocated({"guardian_final_json": json.dumps(project_guardian_final(CLEAN))})


def test_the_class_is_infrastructure_in_the_placement_retry_table():
    assert is_infra_class(INFRA_SALAD_NEVER_ALLOCATED)
    assert INFRA_SALAD_NEVER_ALLOCATED in PLACEMENT_RETRY_CLASSES


# -- control: retryable, bounded, settled at the estimate -------------------------------------


def _control(tmp_path, *, infra_retries, max_attempts=1):
    retry = {"max_attempts": max_attempts, "infra_retries": infra_retries}
    return SaladWorld(tmp_path, stages=lambda env: [salad_stage(env, retry=retry)])


def _final(**over):
    return dict(project_guardian_final(dict(MARKED, estimated_exposure_cents="1.42")), **over)


def _attempts(world):
    return world.env.query("SELECT * FROM campaign_stage_attempts ORDER BY attempt")


def test_a_never_allocated_route_is_retried_and_settled_at_the_estimate_not_the_hold(tmp_path):
    world = _control(tmp_path, infra_retries=1)
    run(world, _final(), passed=False)
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == INFRA_SALAD_NEVER_ALLOCATED != "salad_not_passed"
    assert INFRA_SALAD_NEVER_ALLOCATED in first["failure_detail"]
    assert "nothing ran" in first["failure_detail"]
    assert first["settled_cents"] == 2 and 2 < HOLD  # 1.42 rounded up, never the hold
    assert first["settlement_basis"] == "guardian_bounded_hold"  # provisional, never final


def test_an_estimate_above_the_hold_bounds_nothing(tmp_path):
    world = _control(tmp_path, infra_retries=0)
    run(world, _final(estimated_exposure_cents=str(HOLD + 5)), passed=False)
    world.tick_until(lambda: world.status() == "blocked")
    attempt = world.attempt()  # fail closed as today: an estimate past the hold settles nothing
    assert attempt["status"] == "unknown" and attempt["settled_cents"] is None


def test_the_attempt_cap_still_stops_the_retries(tmp_path):
    world = _control(tmp_path, infra_retries=0)
    run(world, _final(), passed=False)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_NEVER_ALLOCATED,
    )
    assert len(_attempts(world)) == 1


def test_a_stage_that_ran_keeps_today_s_behaviour(tmp_path):
    world = _control(tmp_path, infra_retries=1)
    run(world, dict(CLEAN, reason="allocation_changed"), passed=False)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert attempt["failure_class"] == "salad_not_passed"
    assert attempt["settled_cents"] == HOLD
    assert len(_attempts(world)) == 1


@pytest.mark.parametrize(
    "contradiction",
    [
        {"route_deadline": {"running_seconds": 900, "limit": "route_deadline"}},
        {"status": "unknown"},
    ],
)
def test_a_contradicted_marker_is_not_a_never_allocated_attempt(tmp_path, contradiction):
    from tessera_control.campaign_provider_salad import SaladRouteProvider

    class Rt:
        def __init__(self, final):
            self.final = final

        def _one(self, *_):
            return {"guardian_final_json": json.dumps(self.final)}

    attempt = {"id": "a1"}
    assert SaladRouteProvider._guardian_never_allocated(Rt(_final()), attempt)
    assert not SaladRouteProvider._guardian_never_allocated(Rt(_final(**contradiction)), attempt)
    assert not SaladRouteProvider._guardian_never_allocated(None, attempt)
    assert not SaladRouteProvider._guardian_never_allocated(
        Rt(project_guardian_final(CLEAN)), attempt
    )


def test_the_allocation_change_is_not_a_never_allocated_cause():
    assert "allocation_changed" not in NEVER_ALLOCATED_CAUSES


# -- F2: only the marker widens settlement -------------------------------------------------------

PROVIDER_CLOSED = dict(
    status="reconciled",
    reason=RECONCILE_PROVIDER,
    allocation_uncertain=True,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="1.42",
)


def _bounded(final, failure_class):
    driver = object.__new__(SaladStageDriver)
    row = {"guardian_final_json": json.dumps(dict(CLEAN, **final))}
    attempt = {
        "hold_cents": 5,
        "failure_class": failure_class,
        "failure_detail": "x",
        "salad_verdict": "failed",
    }
    return driver.bounded_hold(attempt, row)


def test_a_provider_confirmed_close_settles_only_with_the_marker():
    marked = dict(PROVIDER_CLOSED, never_allocated=True)
    assert _bounded(marked, INFRA_SALAD_NEVER_ALLOCATED) == 2
    # The allocation timeout and the provisioning overrun keep today's behaviour: a reconciled,
    # uncertain close bounds nothing for them.
    timeout = dict(
        PROVIDER_CLOSED, allocation_timeout={"elapsed_seconds": 9, "deadline_seconds": 9}
    )
    assert _bounded(timeout, "infra_allocation_timeout") is None
    overrun = dict(
        PROVIDER_CLOSED,
        route_deadline={
            "running_seconds": 0,
            "limit": "route_deadline",
            "custody_work_begun": False,
        },
    )
    assert _bounded(overrun, "infra_salad_provisioning_overrun") is None
    assert _never_marked({"guardian_final_json": json.dumps(dict(CLEAN, **marked))})
    assert not _never_marked({"guardian_final_json": json.dumps(dict(CLEAN, **timeout))})
    assert not _never_marked({"guardian_final_json": json.dumps(dict(CLEAN, **overrun))})


def test_a_stopped_allocation_timeout_still_settles_at_its_estimate():
    stopped = dict(
        estimated_exposure_cents="0.2",
        allocation_timeout={"elapsed_seconds": 9, "deadline_seconds": 9},
    )
    assert _bounded(stopped, "infra_allocation_timeout") == 1


# -- F3: the safety service keeps the marker only when the reservation shows no sighting ---------


@pytest.mark.parametrize(
    "sighting",
    [
        {"allocation": "alloc:machine"},
        {"machine": "machine"},
        {"seen_running": 1},
        {"seen_running_at": 12.0},
        {"first_running_at": 12.0},
        {"allocation_uncertain": 1},
    ],
)
def test_any_reservation_sighting_drops_the_marker(sighting):
    clear = dict(
        allocation=None,
        machine=None,
        seen_running=0,
        seen_running_at=None,
        first_running_at=None,
        allocation_uncertain=0,
    )
    assert SaladSafetyService._sighted(clear) is False
    assert SaladSafetyService._sighted(dict(clear, **sighting)) is True
