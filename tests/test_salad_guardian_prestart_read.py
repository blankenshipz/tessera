"""A failed provider read before start, while unallocated, is retried across polls (guardian).

Fakes only. Nothing ran and nothing is billable (0c) until an instance is placed, so a transient
read failure is retried with bounded backoff until the profile deadline; a deterministic failure,
a started custody or an allocated route still stops at once."""

import pytest

from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_guardian import (
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
    is_deterministic_read_failure,
    read_failure_detail,
)

OPERATION = "operation"


class Unknown(ValueError):
    """Shape of the safety service's ProviderUnknown: closed text plus optional attributes."""

    def __init__(self, reason=None, field=None):
        super().__init__("provider_unknown")
        self.reason, self.field = reason, field


@pytest.fixture
def world(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    binding = CustodyBinding("tenant", "project", OPERATION, 1, "artifact")
    custody = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    custody.register(binding, byte_cap=10, file_cap=1)
    assignment = GuardianAssignment(
        binding, "org", "provider-project", "group-id", "attempt-operation",
        "unallocated", "sha256:image", 1000, running_seconds=100,
    )  # fmt: skip
    seen = {"state": "pending", "allocation": "unallocated", "fail": None}
    started = [False]
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

    def make(db="guardian.db", **extra):
        kw = dict(
            custody=custody,
            observe=observe,
            stop=stop,
            clock=lambda: clock[0],
            custody_started=lambda operation: started[0],
        )
        kw.update(extra)
        guardian = SaladGuardian(tmp_path / db, **kw)
        guardian.arm(assignment)
        return guardian

    guardian = make(prestart_read_deadline_seconds=30)
    yield guardian, clock, seen, started, stops, make
    guardian.close()


def poll(guardian, clock, seconds=5):
    clock[0] += seconds
    guardian.heartbeat(OPERATION)
    return guardian.tick(OPERATION)


def test_one_unknown_read_before_start_does_not_stop_and_recovers(world):
    guardian, clock, seen, started, stops, _ = world
    poll(guardian, clock)
    seen["fail"] = Unknown()
    status = poll(guardian, clock)  # smoke 9: one provider_unknown read ~37 s after bind
    assert stops == [] and status["status"] == "armed" and status["stop_cause"] is None
    assert status["prestart_read_retries"]["count"] == 1
    assert guardian.read_retry_backoff(OPERATION) == 5.0
    seen["fail"] = None
    status = poll(guardian, clock)
    assert stops == [] and status["status"] == "armed"
    assert guardian.read_retry_backoff(OPERATION) is None  # the window closed on success
    # A later failure starts a fresh window rather than inheriting the old one.
    seen["fail"] = Unknown()
    clock[0] += 5
    guardian.heartbeat(OPERATION)
    assert guardian.tick(OPERATION)["prestart_read_retries"]["count"] == 2
    assert stops == []


def test_each_attempt_is_recorded_with_closed_detail_and_backoff_grows(world):
    guardian, clock, seen, started, stops, _ = world
    seen["fail"] = Unknown(reason="provider_unavailable")
    backoffs = []
    for _ in range(3):
        status = poll(guardian, clock, 4)
        backoffs.append(guardian.read_retry_backoff(OPERATION))
    assert backoffs == [5.0, 10.0, 15.0] and stops == []
    retries = status["prestart_read_retries"]
    assert retries["count"] == 3 and retries["last_reason"] == "provider_unavailable"
    assert retries["deadline_seconds"] == 30.0
    assert [a["reason"] for a in retries["attempts"]] == ["provider_unavailable"] * 3


def test_after_the_deadline_it_stops_as_provider_unavailable(world):
    guardian, clock, seen, started, stops, _ = world
    seen["fail"] = Unknown()
    for _ in range(5):  # 20 s of continuous failure, inside the 30 s deadline
        status = poll(guardian, clock, 4)
        assert stops == [] and status["stop_cause"] is None
    status = poll(guardian, clock, 12)  # past the deadline
    assert status["stop_cause"] == "provider_unavailable" and len(stops) == 1


def test_timeouts_are_retried_too(world):
    guardian, clock, seen, started, stops, _ = world
    seen["fail"] = TimeoutError()
    status = poll(guardian, clock)
    assert stops == [] and status["prestart_read_retries"]["last_reason"] == "timeout"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Unknown(reason="identity_mismatch"), True),
        (Unknown(reason="identity_mismatch", field="container_resources"), True),
        (Unknown(reason="group_gone"), True),
        (Unknown(reason="malformed_response"), False),
        (Unknown(reason="provider_unavailable"), False),
        (Unknown(), False),
        (TimeoutError(), False),
    ],
)
def test_deterministic_classification(error, expected):
    assert is_deterministic_read_failure(error) is expected


def test_identity_mismatch_stops_immediately_without_burning_the_deadline(world):
    guardian, clock, seen, started, stops, _ = world
    seen["fail"] = Unknown(reason="identity_mismatch", field="gpu_classes")
    status = poll(guardian, clock)
    assert status["stop_cause"] == "provider_unavailable" and len(stops) == 1
    assert "prestart_read_retries" not in status
    assert read_failure_detail(seen["fail"]) == ("identity_mismatch", "gpu_classes")


def test_a_non_provider_unknown_error_still_stops(world):
    guardian, clock, seen, started, stops, _ = world
    seen["fail"] = RuntimeError("boom")
    assert poll(guardian, clock)["stop_cause"] == "provider_unavailable" and len(stops) == 1


def test_started_custody_is_not_a_prestart_read_but_gets_the_short_poststart_retry(world):
    guardian, clock, seen, started, stops, make = world
    seen["fail"] = Unknown()
    started[0] = True  # custody begun: not a pre-start read any more
    status = poll(guardian, clock)
    assert stops == [] and "prestart_read_retries" not in status
    assert status["poststart_read_retries"]["count"] == 1  # q-2505; see its own test file


def test_no_adapter_or_zero_deadline_stops_as_before(world):
    guardian, clock, seen, started, stops, make = world
    seen["fail"] = Unknown()
    for name, extra in {
        "none.db": dict(custody_started=None, prestart_read_deadline_seconds=30),
        "off.db": dict(prestart_read_deadline_seconds=0),
    }.items():
        started[0] = False
        other = make(db=name, **extra)
        before = len(stops)
        clock[0] += 5
        other.heartbeat(OPERATION)
        assert other.tick(OPERATION)["stop_cause"] == "provider_unavailable"
        assert len(stops) == before + 1
        other.close()


def test_an_allocated_or_running_route_gets_the_poststart_retry_not_the_prestart_one(world):
    guardian, clock, seen, started, stops, _ = world
    seen["state"], seen["allocation"] = "running", "alloc-a"
    poll(guardian, clock)  # bound and seen running: post-allocation custody rules apply
    seen["fail"] = Unknown()
    status = poll(guardian, clock)
    assert stops == [] and status["stop_cause"] is None
    assert "prestart_read_retries" not in status
    assert status["poststart_read_retries"]["count"] == 1


@pytest.mark.parametrize("value", [-1, float("nan"), 601, "30", None])
def test_invalid_deadline_is_refused(tmp_path, value):
    tmp_path.chmod(0o700)
    custody = SaladCustody(tmp_path / "custody.db", clock=lambda: 1.0)
    with pytest.raises(ValueError, match="invalid_prestart_read_deadline"):
        SaladGuardian(
            tmp_path / "g.db", custody=custody, observe=None, stop=None,
            prestart_read_deadline_seconds=value,
        )  # fmt: skip
