"""A Salad group that never gets an instance is cancelled at its allocation deadline.

Guardian (provider-neutral wording, fakes only), the service setting, the receipt evidence and the
retryable placement failure on the control side."""

import json

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_settlement import CLEAN, run

from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_guardian import (
    ALLOCATION_TIMEOUT_REASON,
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
)

OPERATION = "operation"
LIMIT = 120


@pytest.fixture
def world(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    binding = CustodyBinding("tenant", "project", OPERATION, 1, "artifact")
    custody = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    custody.register(binding, byte_cap=10, file_cap=1)
    assignment = GuardianAssignment(
        binding,
        "org",
        "provider-project",
        "group-id",
        "attempt-operation",
        "unallocated",
        "sha256:image",
        2000,  # the route deadline is far beyond the allocation deadline
    )
    seen = {"state": "pending", "allocation": "unallocated", "count": 0, "started": True}
    stops = []

    def observe(a):
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

    def make(db="guardian.db", **extra):
        kw = dict(
            custody=custody,
            observe=observe,
            stop=stop,
            clock=lambda: clock[0],
            allocation_seconds=LIMIT,
        )
        kw.update(extra)
        guardian = SaladGuardian(tmp_path / db, **kw)
        guardian.arm(assignment)
        return guardian

    guardian = make()
    yield guardian, clock, seen, stops, make
    try:
        guardian.close()
    except Exception:
        pass


def step(guardian, clock, seconds=2):
    """Advance in poll-sized chunks, heartbeating each (the guardian stops on a missed poll)."""
    remaining, status = seconds, None
    while remaining > 0:
        chunk = min(5, remaining)
        clock[0] += chunk
        remaining -= chunk
        guardian.heartbeat(OPERATION)
        status = guardian.tick(OPERATION)
    return status


def test_a_group_that_never_gets_an_instance_is_cancelled_at_the_allocation_deadline(world):
    guardian, clock, seen, stops, _ = world
    step(guardian, clock)  # first poll: the group is started, the clock starts
    assert step(guardian, clock, LIMIT - 20)["stop_cause"] is None and not stops
    status = step(guardian, clock, 30)
    assert status["stop_cause"] == ALLOCATION_TIMEOUT_REASON and stops
    record = status["allocation_timeout"]
    assert record["deadline_seconds"] == LIMIT and LIMIT <= record["elapsed_seconds"] < LIMIT + 10
    # Cancelled through the ordinary stop path, which is retried but never re-created.
    assert status["status"] == "unknown" and status["orphan"] is True


def test_the_clock_starts_when_the_group_is_started_not_when_it_is_armed(world):
    guardian, clock, seen, stops, _ = world
    seen["started"] = False  # created but stopped: input delivery and settle wait happen here
    assert step(guardian, clock, LIMIT * 3)["stop_cause"] is None and not stops
    seen["started"] = True
    step(guardian, clock)
    assert step(guardian, clock, LIMIT - 20)["stop_cause"] is None and not stops
    assert step(guardian, clock, 30)["stop_cause"] == ALLOCATION_TIMEOUT_REASON


def test_an_instance_that_is_still_pulling_its_image_disables_the_deadline_for_good(world):
    guardian, clock, seen, stops, _ = world
    step(guardian, clock)
    seen["count"] = 1  # allocated/downloading: not running, but it exists
    step(guardian, clock, 10)
    seen["count"] = 0  # a later empty read must not bring the deadline back
    status = step(guardian, clock, LIMIT * 5)
    assert status["stop_cause"] is None and not stops
    assert "allocation_timeout" not in status


@pytest.mark.parametrize("evidence", [{"state": "running"}, {"allocation": "alloc-a"}])
def test_an_allocation_or_a_running_instant_disables_it(world, evidence):
    guardian, clock, seen, stops, _ = world
    step(guardian, clock)
    seen.update(evidence)
    step(guardian, clock, 10)
    seen.update(state="pending", allocation="alloc-a", count=0)
    assert step(guardian, clock, LIMIT * 5)["stop_cause"] is None and not stops


@pytest.mark.parametrize("missing", ["instance_count", "group_started"])
def test_an_adapter_that_cannot_say_never_triggers_it(world, missing):
    guardian, clock, seen, stops, _ = world
    seen["count" if missing == "instance_count" else "started"] = None
    assert step(guardian, clock, LIMIT * 4)["stop_cause"] is None and not stops


def test_the_default_without_a_setting_is_no_allocation_deadline(world):
    guardian, clock, seen, stops, make = world
    plain = make(db="plain.db", allocation_seconds=None)
    assert step(plain, clock, LIMIT * 4)["stop_cause"] is None
    plain.close()


def test_the_route_deadline_is_untouched_and_still_wins_when_earlier(world, tmp_path):
    guardian, clock, seen, stops, make = world
    short = GuardianAssignment(
        CustodyBinding("tenant", "project", "op2", 1, "artifact"),
        "org",
        "provider-project",
        "group-id",
        "attempt-op2",
        "unallocated",
        "sha256:image",
        clock[0] + 90,  # earlier than the allocation deadline
    )
    guardian.custody.register(short.binding, byte_cap=10, file_cap=1)
    guardian.arm(short)
    status = None
    for _ in range(40):
        clock[0] += 5
        guardian.heartbeat("op2")
        status = guardian.tick("op2")
    assert status["stop_cause"] == "deadline"


def test_a_non_fresh_read_that_shows_an_instance_stops_the_guardian_not_the_deadline(world):
    guardian, clock, seen, stops, _ = world
    step(guardian, clock)
    seen["count"] = 1
    clock[0] += 400  # the observation below is older than the poll window: not fresh
    guardian.heartbeat(OPERATION)
    status = guardian.tick(OPERATION)
    assert status["stop_cause"] != ALLOCATION_TIMEOUT_REASON
    seen["count"] = 0
    assert step(guardian, clock, LIMIT * 3)["stop_cause"] != ALLOCATION_TIMEOUT_REASON


@pytest.mark.parametrize("value", [59, 3601, 0, True, 90.5, "120", -1])
def test_an_invalid_allocation_setting_refuses_at_startup(world, value):
    guardian, clock, seen, stops, make = world
    with pytest.raises(ValueError, match="invalid_allocation_seconds"):
        make(db="invalid.db", allocation_seconds=value)


def test_a_group_that_never_starts_never_starts_the_allocation_clock(world):
    guardian, clock, seen, stops, _ = world
    guardian.db.execute("UPDATE assignments SET alloc_started_at=NULL, instance_seen=0")
    guardian.db.commit()
    seen["started"] = False  # never starts, so the clock is never set
    assert step(guardian, clock, LIMIT * 4)["stop_cause"] is None


# -- the service setting ------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 59, 3601, 1.5, True, "600", None])
def test_the_service_refuses_an_invalid_allocation_deadline(tmp_path, value):
    from cryptography.hazmat.primitives.asymmetric import ec
    from test_salad_safety_service import Provider, make_service

    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="invalid_allocation_deadline"):
        make_service(
            tmp_path,
            lambda: 100,
            ec.generate_private_key(ec.SECP256R1()),
            Provider(),
            allocation_deadline_seconds=value,
        )


def test_the_service_default_is_600_and_shows_in_status(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec
    from test_salad_safety_service import Provider, make_service

    tmp_path.chmod(0o700)
    service = make_service(
        tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), Provider()
    )
    assert service.allocation_deadline_seconds == 600
    assert service.guardian.allocation_seconds == 600


def test_the_service_observation_counts_an_instance_that_is_still_pulling():
    from tessera_control.salad_safety_service import _group_started, _instance_count

    counts = dict(allocating_count=0, creating_count=0, running_count=0, stopping_count=0)
    assert _instance_count({"instance_status_counts": counts}) == 0
    assert _instance_count({"instance_status_counts": {**counts, "allocating_count": 1}}) == 1
    assert _instance_count({"instances": [{"state": "downloading"}]}) == 1
    assert (
        _instance_count({"instance_status_counts": counts, "instance_counts_complete": False})
        is None
    )
    assert _instance_count({}) is None
    assert _group_started({"state": "stopped"}) is False
    assert _group_started({"state": "pending", "pending_change": False}) is True
    # the create-settle transient and an adapter that cannot say are not start evidence
    assert _group_started({"state": "pending", "pending_change": True}) is False
    assert _group_started({"state": "pending"}) is False
    assert _group_started({}) is None


# -- the receipt evidence ------------------------------------------------------------------


def test_the_final_carries_the_timeout_and_stays_within_the_stored_bound():
    from tessera_control.campaign_salad_executor import project_guardian_final

    status = dict(
        CLEAN,
        reason="allocation_timeout",
        stop_cause="allocation_timeout",
        allocation_timeout={"elapsed_seconds": 123, "deadline_seconds": 120},
    )
    final = project_guardian_final(status)
    assert final["allocation_timeout"] == {"elapsed_seconds": 123, "deadline_seconds": 120}
    assert len(json.dumps(final, sort_keys=True, separators=(",", ":"))) <= 2000
    plain = project_guardian_final(dict(CLEAN))
    assert "allocation_timeout" not in plain  # the shape of every other run is unchanged
    other = project_guardian_final(
        dict(CLEAN, stop_cause="deadline", allocation_timeout={"elapsed_seconds": 1})
    )
    assert "allocation_timeout" not in other


def test_the_live_check_reports_the_timeout_as_its_reason_and_it_survives_the_stop_path():
    from tessera_control.salad_live_check import _allocation_timeout

    assert _allocation_timeout({"elapsed_seconds": 130, "deadline_seconds": 120}) == {
        "elapsed_seconds": 130,
        "deadline_seconds": 120,
    }
    for bad in (None, {}, {"elapsed_seconds": "1", "deadline_seconds": 2}, [1]):
        assert _allocation_timeout(bad) is None


# -- control: a retryable placement failure, never a stage logic failure -------------------

TIMEOUT_FINAL = dict(
    CLEAN,
    reason="allocation_timeout",
    estimated_exposure_cents="0",
    allocation_timeout={"elapsed_seconds": 125, "deadline_seconds": 120},
)


def _timeout_world(tmp_path, *, infra_retries):
    retry = {"max_attempts": 1, "infra_retries": infra_retries}
    return SaladWorld(tmp_path, stages=lambda env: [salad_stage(env, retry=retry)])


def test_a_timed_out_attempt_fails_permanently_when_no_infrastructure_retry_is_enabled(tmp_path):
    world = _timeout_world(tmp_path, infra_retries=0)
    run(world, TIMEOUT_FINAL, passed=False)
    world.tick_until(lambda: world.attempt()["status"] in {"failed_permanent", "failed_transient"})
    attempt = world.attempt()
    assert attempt["failure_class"] == "infra_allocation_timeout"
    assert attempt["status"] == "failed_permanent"  # bounded: never an unbounded retry
    assert "no infrastructure retry left" in attempt["failure_detail"]
    assert attempt["failure_class"] != "salad_not_passed"  # not a stage logic failure


def test_a_timed_out_attempt_is_a_retryable_placement_failure_within_the_bound(tmp_path):
    world = _timeout_world(tmp_path, infra_retries=1)
    run(world, TIMEOUT_FINAL, passed=False)
    # Not a scientific retry: max_attempts is 1, yet a second attempt is placed (with its own
    # hold under the campaign and project caps), so look at attempt 1 by number.
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM campaign_stage_attempts")["n"] >= 2
    )
    assert world.row("SELECT count(*) AS n FROM campaign_stage_attempts")["n"] == 2
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == "infra_allocation_timeout"
    assert "retry 1 of 1" in first["failure_detail"]


def test_the_receipt_shows_the_timeout_with_the_spend_so_far(tmp_path):
    world = _timeout_world(tmp_path, infra_retries=0)
    run(world, TIMEOUT_FINAL, passed=False)
    world.tick_until(lambda: world.status() == "failed")
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entry = receipt["stages"][0]["attempts"][0]
    assert entry["allocation_timeout"] == {
        "reason": "allocation_timeout",
        "elapsed_seconds": 125,
        "deadline_seconds": 120,
    }
    assert entry["guardian_estimate"]["label"] == "estimate_not_invoice"
    assert entry["guardian_estimate"]["estimated_exposure_cents"] == "0"


def test_the_executor_reported_reason_alone_is_not_a_placement_failure(tmp_path):
    from tessera_control.campaign_provider_salad import SaladRouteProvider

    provider = SaladRouteProvider(SaladWorld(tmp_path).profiles)
    base = {"salad_evidence_digest": "e" * 64, "manifest_id": "m1", "salad_verdict": "failed"}
    forged = provider.observe(
        None, {**base, "failure_detail": "live receipt not passed: allocation_timeout"}
    )
    assert (forged.outcome, forged.failure_class) == ("failed_permanent", "salad_not_passed")


def test_the_retry_detail_keeps_the_deadline_and_elapsed_seconds(tmp_path):
    world = _timeout_world(tmp_path, infra_retries=1)
    run(world, TIMEOUT_FINAL, passed=False)
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM campaign_stage_attempts")["n"] >= 2
    )
    first = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")
    assert "120s (elapsed 125s)" in first["failure_detail"]
