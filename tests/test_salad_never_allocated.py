"""A Salad route that never gets a machine is a placement failure, never the workload's.

The guardian's provisioning bound (counted from arming, independent of any read saying the group
started or how many instances it lists), the never-placed evidence on a route-limit stop, the
live check ending on that evidence, zero-estimate settlement on control, and a provider-confirmed
reallocation without the guardian's custody word retried as infrastructure. Fakes only."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_settlement import CLEAN, run

from tessera_control.campaign_infra_retry import INFRA_SALAD_REALLOCATED
from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_guardian import (
    ALLOCATION_TIMEOUT_REASON,
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
)

OPERATION = "operation"
ALLOCATION = 120
PROVISIONING = 300


@pytest.fixture
def world(tmp_path):
    """A guardian whose reads never say the group started (``pending_change`` stays set, so the
    allocation deadline's clock never runs) and never list a machine."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    binding = CustodyBinding("tenant", "project", OPERATION, 1, "artifact")
    custody = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    custody.register(binding, byte_cap=10, file_cap=1)
    seen = {
        "state": "pending",
        "allocation": "unallocated",
        "count": 0,
        "started": False,
        "custody": False,
        "fail": False,
    }
    stops = []

    def observe(a):
        if seen["fail"]:
            raise TimeoutError("provider read timed out")
        return ProviderObservation(
            a.organization,
            a.provider_project,
            a.group_id,
            a.group_name,
            seen["allocation"],
            a.image_digest,
            seen["state"],
            clock[0],
            instance_count=seen["count"],
            group_started=seen["started"],
        )

    def stop(a):
        stops.append(a)
        return "pending"

    def custody_started(operation):
        if isinstance(seen["custody"], Exception):
            raise seen["custody"]
        return seen["custody"]

    def make(deadline=5000, db="guardian.db", operation=OPERATION, **extra):
        kw = dict(
            custody=custody,
            observe=observe,
            stop=stop,
            clock=lambda: clock[0],
            allocation_seconds=ALLOCATION,
            provisioning_seconds=PROVISIONING,
            custody_started=custody_started,
        )
        kw.update(extra)
        guardian = SaladGuardian(tmp_path / db, **kw)
        own = CustodyBinding("tenant", "project", operation, 1, "artifact")
        if operation != OPERATION:
            custody.register(own, byte_cap=10, file_cap=1)
        guardian.arm(
            GuardianAssignment(
                own,
                "org",
                "provider-project",
                "group-id",
                "attempt-" + operation,
                "unallocated",
                "sha256:image",
                clock[0] + deadline,
            )
        )
        return guardian

    return clock, seen, stops, make


def step(guardian, clock, seconds, operation=OPERATION):
    remaining, status = seconds, None
    while remaining > 0:
        chunk = min(5, remaining)
        clock[0] += chunk
        remaining -= chunk
        guardian.heartbeat(operation)
        status = guardian.tick(operation)
    return status


# -- the provisioning bound ------------------------------------------------------------------


def test_a_group_never_read_as_started_is_cancelled_at_the_provisioning_bound(world):
    clock, seen, stops, make = world
    guardian = make()
    assert step(guardian, clock, PROVISIONING - 10)["stop_cause"] is None and not stops
    status = step(guardian, clock, 15)
    assert status["stop_cause"] == ALLOCATION_TIMEOUT_REASON and stops
    record = status["allocation_timeout"]
    assert record["deadline_seconds"] == PROVISIONING
    assert PROVISIONING <= record["elapsed_seconds"] < PROVISIONING + 10


def test_counts_that_are_not_said_do_not_hold_off_the_provisioning_bound(world):
    clock, seen, stops, make = world
    seen.update(count=None, started=None)  # an adapter that cannot say either
    guardian = make()
    status = step(guardian, clock, PROVISIONING + 5)
    assert status["stop_cause"] == ALLOCATION_TIMEOUT_REASON


def test_a_placed_machine_is_a_slow_pull_and_never_cut_by_the_provisioning_bound(world):
    clock, seen, stops, make = world
    guardian = make()
    seen.update(count=1, started=True)  # a machine was assigned: pulling its image
    step(guardian, clock, 10)
    seen.update(count=0)
    status = step(guardian, clock, PROVISIONING * 3)
    assert status["stop_cause"] is None and not stops


@pytest.mark.parametrize("custody", [True, None, RuntimeError("custody lookup failed")])
def test_custody_begun_or_unknown_never_fires_the_provisioning_bound(world, custody):
    clock, seen, stops, make = world
    seen["custody"] = custody
    guardian = make()
    status = step(guardian, clock, PROVISIONING * 3)
    assert status["stop_cause"] is None and not stops


def test_with_the_bound_off_nothing_fires(world):
    clock, seen, stops, make = world
    off = make(db="off.db", provisioning_seconds=None)
    assert step(off, clock, PROVISIONING * 2)["stop_cause"] is None


def test_without_a_custody_answer_nothing_fires(world):
    clock, seen, stops, make = world
    blind = make(db="blind.db", custody_started=None)
    assert step(blind, clock, PROVISIONING * 2)["stop_cause"] is None


def test_an_assignment_armed_by_an_older_build_has_no_arming_instant_and_never_fires(world):
    clock, seen, stops, make = world
    guardian = make()
    with guardian.db:
        guardian.db.execute("UPDATE assignments SET armed_at=NULL")
    assert step(guardian, clock, PROVISIONING * 2)["stop_cause"] is None


def test_a_group_seen_running_is_bounded_from_first_running_not_by_provisioning(world):
    clock, seen, stops, make = world
    guardian = make()
    seen.update(state="running", count=None, started=True)
    step(guardian, clock, 10)
    seen.update(state="pending")
    assert step(guardian, clock, PROVISIONING * 2)["stop_cause"] is None


@pytest.mark.parametrize("value", [0, 119, 7201, 1.5, True, "1500"])
def test_an_invalid_provisioning_setting_refuses_at_startup(world, value):
    clock, seen, stops, make = world
    with pytest.raises(ValueError, match="invalid_provisioning_seconds"):
        make(db="invalid.db", provisioning_seconds=value)


def test_the_provisioning_bound_is_never_below_the_allocation_deadline(world):
    clock, seen, stops, make = world
    with pytest.raises(ValueError, match="invalid_provisioning_seconds"):
        make(db="short.db", allocation_seconds=600, provisioning_seconds=300)


# -- a route-limit stop of a group that never ran ---------------------------------------------


def test_a_route_limit_before_any_start_read_carries_the_never_placed_evidence(world):
    clock, seen, stops, make = world
    guardian = make(deadline=200, provisioning_seconds=None)
    status = step(guardian, clock, 260)
    assert status["stop_cause"] == "deadline"
    assert status["allocation_timeout"]["elapsed_seconds"] >= 200
    assert "route_deadline" not in status


def test_a_route_limit_after_failed_reads_still_carries_it(world):
    clock, seen, stops, make = world
    guardian = make(deadline=120, provisioning_seconds=None)
    seen["fail"] = True  # reads fail inside the pre-start retry window ...
    assert step(guardian, clock, 60)["stop_cause"] is None
    seen["fail"] = False  # ... then answer, never saying the group started
    status = step(guardian, clock, 90)
    assert status["stop_cause"] == "deadline"
    assert status["allocation_timeout"]["elapsed_seconds"] >= 120


@pytest.mark.parametrize("custody", [True, None])
def test_a_route_limit_with_custody_begun_or_unknown_carries_no_timeout(world, custody):
    clock, seen, stops, make = world
    seen["custody"] = custody
    guardian = make(deadline=200, provisioning_seconds=None)
    status = step(guardian, clock, 260)
    assert status["stop_cause"] == "deadline" and "allocation_timeout" not in status


def test_a_route_limit_after_a_placed_machine_carries_no_timeout(world):
    clock, seen, stops, make = world
    guardian = make(deadline=200, provisioning_seconds=None)
    seen.update(count=1, started=True)
    step(guardian, clock, 10)
    status = step(guardian, clock, 260)
    assert status["stop_cause"] == "deadline" and "allocation_timeout" not in status


# -- the service setting ----------------------------------------------------------------------


def _service(tmp_path, **kw):
    from cryptography.hazmat.primitives.asymmetric import ec
    from test_salad_safety_service import Provider, make_service

    tmp_path.chmod(0o700)
    return make_service(
        tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), Provider(), **kw
    )


def test_the_service_defaults_to_the_route_windows_provisioning_allowance(tmp_path):
    from tessera_control.remote_execution_models import SALAD_PROVISIONING_ALLOWANCE_SECONDS

    service = _service(tmp_path)
    assert service.provisioning_deadline_seconds == SALAD_PROVISIONING_ALLOWANCE_SECONDS
    assert service.guardian.provisioning_seconds == SALAD_PROVISIONING_ALLOWANCE_SECONDS


def test_the_service_default_never_falls_below_a_longer_allocation_deadline(tmp_path):
    service = _service(tmp_path, allocation_deadline_seconds=3600)
    assert service.provisioning_deadline_seconds == 3600


@pytest.mark.parametrize("value", [119, 7201, 1.5, True, "1500", 300])
def test_the_service_refuses_an_invalid_provisioning_deadline(tmp_path, value):
    with pytest.raises(ValueError, match="invalid_provisioning_deadline"):
        _service(tmp_path, provisioning_deadline_seconds=value)  # 300 < the 600 s default


# -- the live check ends on the evidence ------------------------------------------------------


def test_the_live_check_reads_a_route_limit_timeout_record_like_its_own():
    from tessera_control.salad_live_check import _allocation_timeout

    record = {"elapsed_seconds": 1805, "deadline_seconds": 1805}
    assert _allocation_timeout(record) == record
    assert _allocation_timeout({"elapsed_seconds": 1805}) is None


# -- control: retryable and settled at the guardian's estimate --------------------------------

NEVER_PLACED_FINAL = dict(
    CLEAN,
    reason="deadline",
    estimated_exposure_cents="0",
    allocation_timeout={"elapsed_seconds": 1805, "deadline_seconds": 1805},
)


def _control(tmp_path, *, infra_retries):
    retry = {"max_attempts": 1, "infra_retries": infra_retries}
    return SaladWorld(tmp_path, stages=lambda env: [salad_stage(env, retry=retry)])


def test_a_never_placed_route_is_retried_and_settles_at_zero_not_the_hold(tmp_path):
    world = _control(tmp_path, infra_retries=1)
    run(world, NEVER_PLACED_FINAL, passed=False)
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM campaign_stage_attempts")["n"] >= 2
    )
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == "infra_allocation_timeout"
    assert first["failure_class"] != "salad_not_passed"
    assert first["settled_cents"] == 0 and HOLD > 0
    assert first["settlement_basis"] == "guardian_bounded_hold"  # provisional, never final


def test_a_never_placed_route_settles_at_its_rounded_up_estimate_when_one_accrued(tmp_path):
    world = _control(tmp_path, infra_retries=0)
    run(world, dict(NEVER_PLACED_FINAL, estimated_exposure_cents="0.2"), passed=False)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert attempt["failure_class"] == "infra_allocation_timeout"
    assert attempt["settled_cents"] == 1


def test_a_route_that_ran_still_settles_at_the_whole_hold(tmp_path):
    world = _control(tmp_path, infra_retries=0)
    final = dict(CLEAN, route_deadline={"running_seconds": 900, "limit": "route_deadline"})
    run(world, final, passed=False)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert attempt["failure_class"] == "route_deadline_reached"
    assert attempt["settled_cents"] == HOLD


def test_the_executor_reason_alone_never_settles_at_zero(tmp_path):
    from tessera_control.campaign_salad import _never_allocated

    assert _never_allocated({"guardian_final_json": json.dumps(NEVER_PLACED_FINAL)})
    assert not _never_allocated({"guardian_final_json": json.dumps(CLEAN)})
    assert not _never_allocated({"guardian_final_json": None})
    assert not _never_allocated({"guardian_final_json": "not json"})
    both = dict(NEVER_PLACED_FINAL, route_deadline={"running_seconds": 1, "limit": "x"})
    assert not _never_allocated({"guardian_final_json": json.dumps(both)})


# -- a provider-confirmed reallocation without the guardian's custody word ---------------------


def _attempt(detail):
    return {
        "salad_verdict": "failed",
        "salad_evidence_digest": "e" * 64,
        "failure_detail": detail,
        "manifest_id": None,
    }


def test_the_provider_reports_a_route_flagged_reallocation_as_transient_infrastructure():
    from tessera_control.campaign_provider_salad import SaladRouteProvider

    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    moved = provider.observe(
        None,
        _attempt(
            f"{INFRA_SALAD_REALLOCATED}: signal=route_flag; allocation changed; termination "
            "confirmed by provider read"
        ),
    )
    assert (moved.outcome, moved.failure_class) == ("failed_transient", INFRA_SALAD_REALLOCATED)
    bare = provider.observe(None, _attempt("allocation changed; termination confirmed"))
    assert (bare.outcome, bare.failure_class) == ("failed_permanent", "salad_not_passed")


def test_the_class_counts_against_infra_retries():
    from tessera_control.campaign_infra_retry import is_infra_class
    from tessera_control.campaign_models import FAILURE_CLASSES
    from tessera_control.campaign_report import _FAILURES

    assert is_infra_class(INFRA_SALAD_REALLOCATED)
    assert INFRA_SALAD_REALLOCATED in FAILURE_CLASSES and INFRA_SALAD_REALLOCATED in _FAILURES


def _reallocated_world(tmp_path, *, infra_retries):
    from test_salad_profile_fallback import _profiles

    from tessera_control.campaign_provider_salad import register_provider_instance

    def stages(env):
        stage = salad_stage(env, attempts=1)
        stage["retry"] = {**stage["retry"], "infra_retries": infra_retries}
        return [stage]

    world = SaladWorld(tmp_path, stages=stages)
    world.profiles = _profiles(world.env)
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    return world


def test_a_short_run_the_provider_moved_is_retried_within_infra_retries(tmp_path):
    from test_salad_profile_fallback import _ran_unstable

    world = _reallocated_world(tmp_path, infra_retries=1)
    world.to_route_started()
    _ran_unstable(world, within=True)  # latch cleared by a reallocation, provider stop confirmed
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM campaign_stage_attempts")["n"] >= 2
    )
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == INFRA_SALAD_REALLOCATED
    assert "retry 1 of 1" in first["failure_detail"]
    assert world.status() != "failed"


def test_with_no_infra_retry_left_it_fails_under_its_own_class(tmp_path):
    from test_salad_profile_fallback import _ran_unstable

    world = _reallocated_world(tmp_path, infra_retries=0)
    world.to_route_started()
    _ran_unstable(world, within=True)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_REALLOCATED,
    )
    assert "no infrastructure retry left" in attempt["failure_detail"]


# -- the route window is not spent by provisioning ---------------------------------------------


def test_a_salad_lease_covers_the_run_plus_the_provisioning_allowance(tmp_path):
    from datetime import datetime

    from tessera_control.remote_execution_models import SALAD_PROVISIONING_ALLOWANCE_SECONDS

    world = SaladWorld(tmp_path)
    world.to_route_started()
    lease = world.row(
        "SELECT l.issued_at, l.hard_expires_at, t.max_duration_seconds FROM execution_leases l "
        "JOIN execution_grants g ON g.id=l.grant_id JOIN tasks t ON t.id=g.task_id"
    )
    issued = datetime.fromisoformat(lease["issued_at"].replace("Z", "+00:00"))
    hard = datetime.fromisoformat(lease["hard_expires_at"].replace("Z", "+00:00"))
    expected = lease["max_duration_seconds"] + SALAD_PROVISIONING_ALLOWANCE_SECONDS
    # Grant or credential expiry still bound it (this world's worker credential lasts about an
    # hour): the lease reaches past the task's own duration, never past the allowance.
    assert lease["max_duration_seconds"] < (hard - issued).total_seconds() <= expected


def test_the_guardian_says_when_a_route_limit_cut_a_run_before_any_custody_work(world):
    clock, seen, stops, make = world
    guardian = make(deadline=200, provisioning_seconds=None)
    seen.update(count=1, started=True)  # placed: a slow pull
    step(guardian, clock, 190)
    seen.update(state="running")  # first running lands just before the route limit
    status = step(guardian, clock, 30)
    assert status["stop_cause"] == "deadline" and "allocation_timeout" not in status
    assert status["route_deadline"]["limit"] == "route_deadline"
    assert status["route_deadline"]["running_seconds"] <= 15
    assert status["route_deadline"]["custody_work_begun"] is False


@pytest.mark.parametrize("custody", [True, None, RuntimeError("custody lookup failed")])
def test_custody_begun_or_unknown_is_never_reported_as_not_begun(world, custody):
    clock, seen, stops, make = world
    seen["custody"] = custody
    guardian = make(deadline=200, provisioning_seconds=None)
    seen.update(count=1, started=True, state="running")
    status = step(guardian, clock, 230)
    assert status["stop_cause"] == "deadline"
    assert "custody_work_begun" not in status["route_deadline"]


def _overrun(**change):
    return {"running_seconds": 0, "limit": "route_deadline", "custody_work_begun": False, **change}


def test_only_a_route_limit_with_no_run_and_no_custody_is_a_provisioning_overrun():
    from tessera_control.campaign_infra_retry import provisioning_overrun

    assert provisioning_overrun(_overrun())
    assert provisioning_overrun(_overrun(running_seconds=60))
    assert not provisioning_overrun(_overrun(running_seconds=61))
    assert not provisioning_overrun(_overrun(limit="running_cap"))
    assert not provisioning_overrun({"running_seconds": 0, "limit": "route_deadline"})
    assert not provisioning_overrun(None)


def test_the_stored_final_keeps_its_shape_without_the_custody_word():
    from tessera_control.campaign_salad_commands import _final_dump, _GuardianFinal

    cut = {"running_seconds": 5, "limit": "route_deadline"}
    plain = _GuardianFinal.model_validate(dict(CLEAN, route_deadline=cut))
    assert _final_dump(plain)["route_deadline"] == cut
    marked = _GuardianFinal.model_validate(dict(CLEAN, route_deadline=_overrun()))
    assert _final_dump(marked)["route_deadline"] == _overrun()
    with pytest.raises(ValueError):
        _GuardianFinal.model_validate(dict(CLEAN, route_deadline=_overrun(custody_work_begun=True)))


def test_the_projection_carries_the_custody_word_only_when_false():
    from tessera_control.campaign_salad_executor import project_guardian_final

    raw = dict(CLEAN, stop_cause="deadline", route_deadline=_overrun())
    assert project_guardian_final(raw)["route_deadline"] == _overrun()
    unknown = dict(raw, route_deadline={"running_seconds": 0, "limit": "route_deadline"})
    assert "custody_work_begun" not in project_guardian_final(unknown)["route_deadline"]


def test_a_route_limit_at_zero_running_is_retried_and_settled_at_the_estimate(tmp_path):
    world = _control(tmp_path, infra_retries=1)
    run(world, dict(CLEAN, estimated_exposure_cents="0", route_deadline=_overrun()), passed=False)
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM campaign_stage_attempts")["n"] >= 2
    )
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == "infra_salad_provisioning_overrun"
    assert "retry 1 of 1" in first["failure_detail"]
    assert first["settled_cents"] == 0 and first["settlement_basis"] == "guardian_bounded_hold"


def test_a_route_limit_after_a_real_run_stays_permanent_at_the_hold(tmp_path):
    world = _control(tmp_path, infra_retries=1)
    final = dict(CLEAN, route_deadline=_overrun(running_seconds=600))
    run(world, final, passed=False)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        "route_deadline_reached",
    )
    assert attempt["settled_cents"] == HOLD
