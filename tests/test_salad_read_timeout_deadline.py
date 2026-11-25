"""A started Salad group whose instance reads time out still hits its allocation deadline.

Replays a started group that reads ``running`` at group level with only an allocating or creating
instance (no machine assigned) while every instance read times out. Fakes only, no provider."""

import pytest
from test_campaign_salad_settlement import CLEAN
from test_salad_allocation_deadline import (  # noqa: F401
    GONE,
    LIMIT,
    OPERATION,
    _from_group,
    flaky,
    step,
    world,
)

from tessera_control.salad_guardian import ALLOCATION_TIMEOUT_REASON

MACHINE = "a" * 32  # the HTTP adapter gives every listed instance a UUID machine_id
COUNTS = ("allocating_count", "creating_count", "running_count", "stopping_count")


def _group(instances=None, **counts):
    base = {
        "provider_id": "group-id",
        "state": "running",
        "instance_status_counts": {**dict.fromkeys(COUNTS, 0), **counts},
        "instance_counts_complete": True,
        "pending_change": False,
    }
    if instances is not None:
        base["instances"] = instances
    return base


# Counts without an instance list name no machine (the HTTP adapter never lists an instance
# without one).
CREATING_COUNTS_ONLY = _group(creating_count=1)
ALLOCATING_COUNTS_ONLY = _group(allocating_count=1)
# A CPU group that omits its counts, with an instance list that was read and is empty.
EMPTY_LIST_NO_COUNTS = {
    **_group([]),
    "instance_counts_complete": False,
    "instance_status_counts": {},
}
RUNNING_ON_MACHINE = _group([{"state": "running", "machine_id": MACHINE}], running_count=1)


def _drive_failing_reads(guardian, clock, mode):
    mode["fail"] = True
    status = None
    for _ in range(2 * LIMIT // 5):
        status = step(guardian, clock, 5)
        if status["stop_cause"] is not None:
            break
    return status


def test_a_running_group_with_only_a_creating_instance_and_timing_out_reads_hits_the_deadline(
    flaky,  # noqa: F811 (fixture imported from test_salad_allocation_deadline)
):
    guardian, clock, seen, stops, mode = flaky
    seen.update(_from_group(GONE))
    step(guardian, clock)  # started: the clock starts
    seen.update(_from_group(CREATING_COUNTS_ONLY))
    assert seen["state"] == "running" and seen["count"] == 0  # group says running, no machine
    step(guardian, clock, 10)
    status = _drive_failing_reads(guardian, clock, mode)  # every instance read times out
    assert status["stop_cause"] == ALLOCATION_TIMEOUT_REASON and stops
    assert status["allocation_timeout"]["elapsed_seconds"] >= LIMIT


def test_a_creating_or_allocating_group_without_an_assigned_machine_does_not_latch(
    flaky,  # noqa: F811 (fixture imported from test_salad_allocation_deadline)
):
    guardian, clock, seen, stops, mode = flaky
    seen.update(_from_group(GONE))
    step(guardian, clock)
    for shape in (ALLOCATING_COUNTS_ONLY, CREATING_COUNTS_ONLY):
        seen.update(_from_group(shape))
        assert seen["count"] == 0
        step(guardian, clock, 10)
    status = step(guardian, clock, LIMIT)
    assert status["stop_cause"] == ALLOCATION_TIMEOUT_REASON and stops


def test_an_assigned_machine_with_running_evidence_is_allocated_and_never_expires(
    flaky,  # noqa: F811 (fixture imported from test_salad_allocation_deadline)
):
    guardian, clock, seen, stops, mode = flaky
    seen.update(_from_group(GONE))
    step(guardian, clock)
    seen.update(_from_group(RUNNING_ON_MACHINE), allocation="alloc-a")
    assert seen["count"] == 1 and seen["state"] == "running"
    step(guardian, clock, 10)
    seen.update(_from_group(GONE), allocation="alloc-a")
    assert step(guardian, clock, LIMIT * 3)["stop_cause"] is None and not stops
    status = _drive_failing_reads(guardian, clock, mode)
    assert status["stop_cause"] != ALLOCATION_TIMEOUT_REASON
    assert "allocation_timeout" not in status


def test_instance_count_names_machines_only():
    from tessera_control.salad_safety_service import _instance_count

    assert _instance_count(ALLOCATING_COUNTS_ONLY) == 0
    assert _instance_count(CREATING_COUNTS_ONLY) == 0
    assert _instance_count(RUNNING_ON_MACHINE) == 1
    assert _instance_count(_group(running_count=1)) == 1
    downloading = _group([{"state": "downloading", "machine_id": MACHINE}], creating_count=1)
    assert _instance_count(downloading) == 1


def test_a_running_group_with_an_empty_list_and_no_counts_still_hits_the_deadline(
    flaky,  # noqa: F811 (fixture imported from test_salad_allocation_deadline)
):
    guardian, clock, seen, stops, mode = flaky
    seen.update(_from_group(GONE))
    step(guardian, clock)
    seen.update(_from_group(EMPTY_LIST_NO_COUNTS))
    assert seen["state"] == "running" and seen["count"] == 0 and seen["started"] is True
    step(guardian, clock, LIMIT - 20)
    assert not stops
    status = step(guardian, clock, 30)
    assert status["stop_cause"] == ALLOCATION_TIMEOUT_REASON and stops
    from tessera_control.campaign_salad_executor import project_guardian_final

    final = project_guardian_final(
        dict(
            CLEAN,
            stop_cause=status["stop_cause"],
            allocation_timeout=status["allocation_timeout"],
        )
    )
    assert final["allocation_timeout"]["elapsed_seconds"] >= LIMIT  # fallback keys on this


def test_a_running_group_with_an_unsaid_count_keeps_the_running_cap_and_never_fires(
    world,  # noqa: F811 (fixture imported from test_salad_allocation_deadline)
):
    guardian, clock, seen, stops, _ = world
    seen.update(state="running", count=None)  # an adapter that cannot say
    step(guardian, clock, LIMIT * 4)
    row = guardian.db.execute(
        "SELECT first_running FROM assignments WHERE operation=?", (OPERATION,)
    ).fetchone()
    assert row[0] is not None  # the running-seconds cap stays fail closed
    assert guardian.status(OPERATION)["stop_cause"] is None and not stops


# -- the failure reason reaches the recorded readback ---------------------------------------


def test_a_failed_read_readback_keeps_its_closed_reason_detail_and_request_kind():
    from tessera_control.salad_proxy_contract import ProxyResult
    from tessera_control.salad_server import TrustedSaladServer

    result = ProxyResult(
        status="unknown",
        reason="provider_unavailable",
        detail="timeout",
        request_kind="read_instance",
    )
    state = {"allocation_id": None}
    body = TrustedSaladServer.readback(result, state).model_dump(mode="json")
    assert body["state"] == "unknown"
    assert body["proxy_reason"] == "provider_unavailable"
    assert body["proxy_detail"] == "timeout"
    assert body["proxy_request_kind"] == "read_instance"


def test_a_dispatched_read_that_raises_records_the_timeout_reason():
    from tessera_worker.remote_dispatch import _failed_readback

    class Unavailable(Exception):
        reason, detail, field, request_kind = (
            "provider_unavailable",
            "timeout",
            None,
            "read_instance",
        )

    body = _failed_readback(Unavailable("https://secret.example/path?token=abc"), True)
    dumped = body.model_dump(mode="json")
    assert (dumped["proxy_reason"], dumped["proxy_detail"], dumped["proxy_request_kind"]) == (
        "provider_unavailable",
        "timeout",
        "read_instance",
    )
    assert "secret" not in str(dumped) and dumped["provenance"] == "provider"


@pytest.mark.parametrize("bad", ["https://host/x", "Timeout!", "a" * 80])
def test_a_token_outside_the_closed_vocabulary_is_dropped_not_recorded(bad):
    from tessera_worker.remote_dispatch import _failed_readback

    class Odd(Exception):
        reason, field, request_kind = "provider_unavailable", None, "read_instance"
        detail = bad

    dumped = _failed_readback(Odd(), False).model_dump(mode="json")
    assert dumped["state"] == "unknown" and bad not in str(dumped)
    assert dumped["proxy_detail"] is None


def test_diagnostics_are_refused_on_a_readback_that_is_not_unknown():
    from pydantic import ValidationError

    from tessera_control.remote_execution_models import ProviderReadback

    base = dict(provider_id="a" * 32, state="running", observed_at="2026-11-25T12:00:00Z")
    ProviderReadback(**base)
    for token in (
        {"proxy_detail": "timeout"},
        {"proxy_request_kind": "read_instance"},
        {"proxy_field": "group"},
    ):
        with pytest.raises(ValidationError):
            ProviderReadback(**base, **token)
    unknown = ProviderReadback(
        state="unknown", observed_at="2026-11-25T12:00:00Z", proxy_detail="timeout"
    )
    assert unknown.proxy_detail == "timeout"


def test_a_reallocated_readback_carries_no_read_failure_diagnostics():
    from tessera_control.salad_proxy_contract import ProxyResult
    from tessera_control.salad_server import TrustedSaladServer

    result = ProxyResult(
        status="observed",
        reason="observed",
        detail="timeout",
        request_kind="read_instance",
        observation=dict(
            provider_id="a" * 32,
            state="running",
            instances=[{"instance_id": "b" * 32, "machine_id": MACHINE, "state": "running"}],
        ),
    )
    body = TrustedSaladServer.readback(result, {"allocation_id": "other"}).model_dump(mode="json")
    assert body["state"] == "unknown" and body["proxy_reason"] == "reallocation_uncertain"
    assert body["proxy_detail"] is None and body["proxy_request_kind"] is None
