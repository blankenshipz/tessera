"""A transient provider read failure AFTER the run began is retried briefly (guardian).

Smoke evidence: inputs were delivered and the run started, then one status read timed out and
the guardian stopped the run (provider_unavailable). After custody began, an allocation was bound
or the instance was seen running, a transient failure (timeout / provider_unknown) is retried at
the next poll, bounded by POSTSTART_READ_RETRY_LIMIT consecutive failures and
POSTSTART_READ_WINDOW_SECONDS. Deterministic failures, an already-stopping guardian and the
assignment's own deadline and heartbeat clocks still stop at once. Fakes only."""

import pytest
from test_salad_guardian_prestart_read import Unknown

from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_guardian import (
    POSTSTART_READ_RETRY_LIMIT,
    POSTSTART_READ_RETRY_PREFIX,
    POSTSTART_READ_WINDOW_SECONDS,
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
)
from tessera_control.salad_safety_service import UNSEEN_RUN_GAP_SECONDS

OPERATION = "operation"


@pytest.fixture
def world(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    binding = CustodyBinding("tenant", "project", OPERATION, 1, "artifact")
    custody = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    custody.register(binding, byte_cap=10, file_cap=1)
    assignment = GuardianAssignment(
        binding, "org", "provider-project", "group-id", "attempt-operation",
        "unallocated", "sha256:image", 400, heartbeat_seconds=100, poll_seconds=30,
    )  # fmt: skip
    seen = {"state": "pending", "allocation": "unallocated", "fail": None}
    started = [True]  # inputs delivered: custody began
    stops = []

    def observe(a):
        if seen["fail"] is not None:
            raise seen["fail"]
        return ProviderObservation(
            a.organization, a.provider_project, a.group_id, a.group_name,
            seen["allocation"], a.image_digest, seen["state"], clock[0],
        )  # fmt: skip

    def stop(a):
        stops.append(a)
        return "pending"

    guardian = SaladGuardian(
        tmp_path / "guardian.db",
        custody=custody,
        observe=observe,
        stop=stop,
        clock=lambda: clock[0],
        custody_started=lambda operation: started[0],
    )
    guardian.arm(assignment)
    yield guardian, clock, seen, started, stops
    guardian.close()


def poll(guardian, clock, seconds=5, *, heartbeat=True):
    clock[0] += seconds
    if heartbeat:
        guardian.heartbeat(OPERATION)
    return guardian.tick(OPERATION)


def events(guardian, prefix=POSTSTART_READ_RETRY_PREFIX):
    rows = guardian.db.execute("SELECT kind FROM events WHERE operation=?", (OPERATION,))
    return [kind for (kind,) in rows if kind.startswith(prefix)]


def test_the_window_is_the_unseen_run_gap_the_admission_margin_already_counts():
    assert POSTSTART_READ_WINDOW_SECONDS == UNSEEN_RUN_GAP_SECONDS
    assert POSTSTART_READ_RETRY_LIMIT == 3


def test_one_timeout_after_custody_began_is_retried_and_the_window_resets(world):
    guardian, clock, seen, started, stops = world
    poll(guardian, clock)
    seen["fail"] = TimeoutError()
    status = poll(guardian, clock)  # smoke: the read timed out right after start
    assert stops == [] and status["status"] == "armed" and status["stop_cause"] is None
    assert len(events(guardian)) == 1
    retries = status["poststart_read_retries"]
    assert retries["count"] == 1 and retries["limit"] == 3 and retries["window_seconds"] == 45.0
    assert guardian.read_retry_backoff(OPERATION) == 5.0
    seen["fail"] = None
    status = poll(guardian, clock)
    assert stops == [] and status["status"] == "armed"
    assert guardian.read_retry_backoff(OPERATION) is None  # the run continues, window closed
    # Two later failures are a fresh window of consecutive failures, not a continuation.
    seen["fail"] = Unknown()
    poll(guardian, clock)
    status = poll(guardian, clock)
    assert stops == [] and status["stop_cause"] is None
    assert len(events(guardian)) == 3  # 1 + 2 recorded, none of them stopped the run


def test_an_instance_seen_running_gets_the_same_retry(world):
    guardian, clock, seen, started, stops = world
    started[0] = False
    seen["state"], seen["allocation"] = "running", "alloc-a"
    poll(guardian, clock)
    seen["fail"] = Unknown()
    assert poll(guardian, clock)["stop_cause"] is None and stops == []
    assert len(events(guardian)) == 1


def test_the_third_consecutive_transient_failure_stops_as_provider_unavailable(world):
    guardian, clock, seen, started, stops = world
    seen["fail"] = Unknown()
    for _ in range(POSTSTART_READ_RETRY_LIMIT - 1):
        status = poll(guardian, clock)
        assert stops == [] and status["stop_cause"] is None
    status = poll(guardian, clock)
    assert status["stop_cause"] == "provider_unavailable" and len(stops) == 1
    assert len(events(guardian)) == POSTSTART_READ_RETRY_LIMIT - 1


def test_the_window_closing_stops_even_before_the_count_is_spent(world):
    guardian, clock, seen, started, stops = world
    seen["fail"] = Unknown()
    assert poll(guardian, clock, 30)["stop_cause"] is None  # first failure opens the window
    status = poll(guardian, clock, POSTSTART_READ_WINDOW_SECONDS + 1)  # second, past 45 s
    assert status["stop_cause"] == "provider_unavailable" and len(stops) == 1
    assert len(events(guardian)) == 1


def test_a_deterministic_failure_after_start_stops_at_once(world):
    guardian, clock, seen, started, stops = world
    seen["fail"] = Unknown(reason="group_gone")
    status = poll(guardian, clock)
    assert status["stop_cause"] == "provider_unavailable" and len(stops) == 1
    assert events(guardian) == []
    seen["fail"] = Unknown(reason="identity_mismatch", field="group_id")
    other = poll(guardian, clock)  # already stopping; nothing is retried either way
    assert other["status"] != "armed" and events(guardian) == []


def test_a_non_provider_error_after_start_stops_at_once(world):
    guardian, clock, seen, started, stops = world
    seen["fail"] = RuntimeError("boom")
    assert poll(guardian, clock)["stop_cause"] == "provider_unavailable" and len(stops) == 1
    assert events(guardian) == []


def test_an_already_stopping_guardian_takes_no_retry(world):
    guardian, clock, seen, started, stops = world
    poll(guardian, clock)
    guardian.cancel(OPERATION)  # e.g. a budget fence or an operator stop
    seen["fail"] = Unknown()
    status = poll(guardian, clock)
    assert events(guardian) == [] and status["status"] != "armed"


def test_the_assignment_deadline_is_not_extended_by_a_retry(world):
    guardian, clock, seen, started, stops = world
    seen["fail"] = Unknown()
    assert poll(guardian, clock)["stop_cause"] is None  # retried at t=105
    status = poll(guardian, clock, 400)  # past the 400 s deadline
    # A failed read on a tick that is already stopping keeps today's cause label.
    assert status["stop_cause"] in {"deadline", "provider_unavailable"} and len(stops) == 1
    assert len(events(guardian)) == 1  # the stopping poll recorded no further retry


def test_a_lapsed_heartbeat_is_not_extended_by_a_retry(world):
    guardian, clock, seen, started, stops = world
    seen["fail"] = Unknown()
    assert poll(guardian, clock)["stop_cause"] is None
    status = poll(guardian, clock, 101, heartbeat=False)  # heartbeat_seconds=100 lapsed
    assert status["stop_cause"] in {"control_disconnect", "provider_unavailable"}
    assert len(stops) == 1
    assert len(events(guardian)) == 1


def test_stop_never_comes_later_than_the_window_plus_one_poll(world):
    """Charging stays conservative: the run is stopped within the window the service already
    counts as unseen running time, measured from the first failed read."""
    guardian, clock, seen, started, stops = world
    seen["fail"] = Unknown()
    first = clock[0] + 5
    stopped_at = None
    for _ in range(6):
        status = poll(guardian, clock)
        if status["stop_cause"] == "provider_unavailable":
            stopped_at = clock[0]
            break
    assert stopped_at is not None
    assert stopped_at - first <= POSTSTART_READ_WINDOW_SECONDS
