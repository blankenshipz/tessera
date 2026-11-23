"""A change of allocation before anything ran is a re-bind, not a stop (guardian, provider-neutral).

Fakes only. The guardian re-binds while no instance was observed running and the provider
adapter reports no custody started; the clocks, deadline and bound are never reset."""

import json

import pytest

from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_guardian import (
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
)

OPERATION = "operation"


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
        "alloc-a",
        "sha256:image",
        400,
        running_seconds=100,
    )
    seen = {"state": "pending", "allocation": "alloc-a", "prior": None}
    started = [False]
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
            prior_allocation_id=seen["prior"],
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
            custody_started=lambda operation: started[0],
        )
        kw.update(extra)
        guardian = SaladGuardian(tmp_path / db, **kw)
        guardian.arm(assignment)
        return guardian

    guardian = make()
    yield guardian, assignment, clock, seen, started, stops, make
    try:
        guardian.close()
    except Exception:
        pass


def step(guardian, clock, seconds=2):
    """Advance in poll-sized chunks (the guardian stops on a missed poll), heartbeating each."""
    remaining, status = seconds, None
    while remaining > 0:
        chunk = min(5, remaining)
        clock[0] += chunk
        remaining -= chunk
        guardian.heartbeat(OPERATION)
        status = guardian.tick(OPERATION)
    return status


def move(seen, new):
    seen["allocation"] = new


def test_a_change_before_anything_ran_rebinds_and_keeps_waiting(world):
    guardian, a, clock, seen, started, stops, _ = world
    step(guardian, clock)  # bound to alloc-a, pending
    move(seen, "alloc-b")
    status = step(guardian, clock)
    assert stops == [] and status["status"] != "unknown" and status["stop_cause"] is None
    rebinds = status["prestart_rebinds"]
    assert rebinds["count"] == 1
    event = rebinds["events"][0]
    assert (event["from"], event["to"], event["reason"]) == (
        "alloc-a",
        "alloc-b",
        "prestart_reallocation",
    )
    assert event["at"] == clock[0]
    # Bound to the new allocation: the next poll sees no change and nothing else is recorded.
    assert step(guardian, clock)["prestart_rebinds"]["count"] == 1 and stops == []


def test_the_prior_allocation_read_path_rebinds_too(world):
    guardian, a, clock, seen, started, stops, _ = world
    step(guardian, clock)
    seen.update(allocation="alloc-b", prior="alloc-a")  # the adapter's retained previous read
    status = step(guardian, clock)
    assert stops == [] and status["prestart_rebinds"]["count"] == 1


def test_a_status_without_rebinds_has_no_such_field(world):
    guardian, a, clock, seen, started, stops, _ = world
    assert "prestart_rebinds" not in step(guardian, clock)


def test_a_change_after_an_instance_ran_still_stops(world):
    guardian, a, clock, seen, started, stops, _ = world
    seen["state"] = "running"
    step(guardian, clock)  # first running instant observed
    move(seen, "alloc-b")
    status = step(guardian, clock)
    assert status["status"] == "unknown" and status["stop_cause"] == "allocation_changed"
    assert status["allocation_uncertain"] and stops and "prestart_rebinds" not in status


def test_a_change_after_custody_started_still_stops(world):
    guardian, a, clock, seen, started, stops, _ = world
    step(guardian, clock)
    started[0] = True  # an instance redeemed the input ticket (or wrote an output)
    move(seen, "alloc-b")
    status = step(guardian, clock)
    assert status["stop_cause"] == "allocation_changed" and stops
    assert "prestart_rebinds" not in status


def _broken(operation):
    raise RuntimeError("unreadable")


@pytest.mark.parametrize("started", [_broken, None], ids=["unreadable", "no_callable"])
def test_an_adapter_that_cannot_tell_never_rebinds(world, started):
    guardian, a, clock, seen, _, stops, make = world
    fresh = make(db="variant.db", custody_started=started)
    step(fresh, clock)
    move(seen, "alloc-b")
    status = step(fresh, clock)
    assert status["stop_cause"] == "allocation_changed" and stops
    fresh.close()


def test_the_bound_stops_the_change_after_the_last_rebind(world):
    guardian, a, clock, seen, started, stops, make = world
    bounded = make(db="variant.db", max_prestart_rebinds=2)
    step(bounded, clock)
    for new in ("alloc-b", "alloc-c"):
        move(seen, new)
        assert step(bounded, clock)["stop_cause"] is None
    assert bounded.rebinds_left(OPERATION) == 0
    move(seen, "alloc-d")
    status = step(bounded, clock)
    assert status["stop_cause"] == "allocation_changed" and stops
    assert status["prestart_rebinds"]["count"] == 2
    bounded.close()


def test_a_zero_bound_disables_the_rebind(world):
    guardian, a, clock, seen, started, stops, make = world
    off = make(db="variant.db", max_prestart_rebinds=0)
    step(off, clock)
    move(seen, "alloc-b")
    assert step(off, clock)["stop_cause"] == "allocation_changed"
    off.close()


@pytest.mark.parametrize("value", [-1, 11, True, 1.5, "3", None])
def test_an_invalid_bound_refuses_at_startup(world, value):
    guardian, a, clock, seen, started, stops, make = world
    with pytest.raises(ValueError, match="invalid_max_prestart_rebinds"):
        make(db="variant.db", max_prestart_rebinds=value)


def test_rebinds_never_reset_the_deadline_or_the_billing_clock(world):
    guardian, a, clock, seen, started, stops, _ = world
    stored = guardian.db.execute("SELECT assignment FROM assignments").fetchone()[0]
    step(guardian, clock)
    for new in ("alloc-b", "alloc-c", "alloc-d"):
        move(seen, new)
        assert step(guardian, clock, 50)["stop_cause"] is None  # t advances 50 per rebind
    # The assignment (its deadline and running window) is byte for byte unchanged...
    assert guardian.db.execute("SELECT assignment FROM assignments").fetchone()[0] == stored
    assert json.loads(stored)["deadline"] == 400
    # ...so the original deadline (400) still stops the run, however many re-binds happened.
    status = step(guardian, clock, 150)
    assert status["status"] == "unknown" and status["stop_cause"] == "deadline" and stops


def test_the_running_window_starts_at_the_first_running_instant_not_at_a_rebind(world):
    guardian, a, clock, seen, started, stops, _ = world
    step(guardian, clock)
    move(seen, "alloc-b")
    step(guardian, clock, 30)  # re-bound; t=132
    seen["state"] = "running"
    step(guardian, clock, 30)  # the first running instant is observed at t=137 (clock now 162)
    assert stops == []
    status = step(guardian, clock, 70)  # t=232, inside 137 + 100
    assert status["stop_cause"] is None and stops == []
    status = step(guardian, clock, 10)  # t=242, past 237: the window counts from t=137
    assert status["stop_cause"] == "deadline" and stops


def test_heartbeat_loss_still_stops_across_rebinds(world):
    guardian, a, clock, seen, started, stops, _ = world
    step(guardian, clock)
    move(seen, "alloc-b")
    step(guardian, clock)
    clock[0] += 31  # control stopped heartbeating
    status = guardian.tick(OPERATION)
    assert status["stop_cause"] == "control_disconnect" and stops


def test_a_rebind_never_overrides_a_stop_already_decided(world):
    guardian, a, clock, seen, started, stops, _ = world
    step(guardian, clock)
    guardian.cancel(OPERATION)
    move(seen, "alloc-b")
    status = step(guardian, clock)
    assert status["status"] == "unknown" and stops
    assert "prestart_rebinds" not in status


def _status_with(rebinds):
    status = {
        "status": "stopped",
        "reason": "reconciled",
        "orphan": False,
        "allocation_uncertain": False,
        "escalation_pending": False,
        "budget_fenced": False,
        "rate_unknown": False,
        "estimated_exposure_cents": "0.5",
        "published_hourly_rate_usd": "0.5",
        "actual_billed_cost_cents": None,
    }
    if rebinds is not None:
        status["prestart_rebinds"] = rebinds
    return status


def test_the_receipt_projection_carries_the_rebinds_and_fits_the_stored_bound():
    from tessera_control.campaign_salad_executor import project_guardian_final

    long_id = "x" * 200  # the guardian caps ids; the projection caps again
    events = [{"at": 1_700_000_000.0 + n, "from": long_id, "to": long_id} for n in range(5)]
    final = project_guardian_final(_status_with({"count": 5, "events": events}))
    assert final is not None and final["prestart_rebinds"]["count"] == 5
    assert len(final["prestart_rebinds"]["events"]) == 3  # the last three
    assert all(e["reason"] == "prestart_reallocation" for e in final["prestart_rebinds"]["events"])
    stored = json.dumps(final, sort_keys=True, separators=(",", ":"))
    assert len(stored) <= 2000, "guardian_final_json CHECK is length <= 2000"
    plain = project_guardian_final(_status_with(None))
    assert plain is not None and "prestart_rebinds" not in plain  # shape unchanged without one
    assert project_guardian_final(_status_with({"count": 0, "events": []}))
