"""Start-failure loop before custody, one closed events read, and pending/deploying readbacks.

Fakes only: no provider, no network, no credentials."""

import json
from types import SimpleNamespace

import pytest
from test_salad_http import ID, Factory, run

from tessera_control.campaign_class_fallback import REASONS
from tessera_control.campaign_infra_retry import INFRA_PREFIX, REALLOCATED_PRECUSTODY
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.remote_execution_models import ProviderReadback
from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_events import summarize_events
from tessera_control.salad_guardian import (
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
)
from tessera_control.salad_live_check import _note_start_evidence
from tessera_control.salad_proxy_contract import ProxyResult
from tessera_control.salad_safety_service import ProviderUnknown, SaladSafetyService
from tessera_control.salad_server import TrustedSaladServer

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
        "unallocated",
        "sha256:image",
        100 + 3600,
    )
    seen = {"machines": ("m1",), "state": "pending"}
    started = [False]
    stops = []

    def observe(a):
        return ProviderObservation(
            a.organization,
            a.provider_project,
            a.group_id,
            a.group_name,
            "unallocated",
            a.image_digest,
            seen["state"],
            clock[0],
            instance_count=len(seen["machines"]),
            group_started=True,
            machine_ids=seen["machines"],
        )

    def stop(a):
        stops.append(a)
        return "pending"

    guardian = SaladGuardian(
        tmp_path / "guardian.db",
        custody=custody,
        observe=observe,
        stop=stop,
        clock=lambda: clock[0],
        allocation_seconds=600,
        custody_started=lambda operation: started[0],
    )
    guardian.arm(assignment)
    yield guardian, clock, seen, started, stops
    guardian.close()


def poll(guardian, clock, seconds=5):
    clock[0] += seconds
    guardian.heartbeat(OPERATION)
    return guardian.tick(OPERATION)


def test_three_distinct_machines_before_custody_stop_as_a_start_failure(world):
    guardian, clock, seen, started, stops = world
    for machine in ("m1", "m2", "m3"):
        seen.update(machines=(machine,))
        status = poll(guardian, clock)
        assert stops == [] or machine == "m3"
    assert status["stop_cause"] == "start_failure_suspected" and stops
    assert status["start_failure"] == {"machines": 3}
    assert status["status"] == "unknown" and "allocation_timeout" not in status


def test_one_machine_stuck_downloading_is_a_slow_pull_not_a_loop(world):
    guardian, clock, seen, started, stops = world
    for _ in range(240):  # twenty minutes of 5 second polls
        status = poll(guardian, clock)
    assert stops == [] and status["stop_cause"] is None and "start_failure" not in status


def test_machines_are_not_counted_once_custody_began(world):
    guardian, clock, seen, started, stops = world
    started[0] = True
    for machine in ("m1", "m2", "m3", "m4"):
        seen.update(machines=(machine,))
        status = poll(guardian, clock)
    assert stops == [] and status["stop_cause"] is None


def test_an_adapter_that_cannot_say_never_counts(world):
    guardian, clock, seen, started, stops = world
    guardian.custody_started = None
    for machine in ("m1", "m2", "m3", "m4"):
        seen.update(machines=(machine,))
        poll(guardian, clock)
    assert stops == []


def test_the_same_machine_reappearing_counts_once(world):
    guardian, clock, seen, started, stops = world
    for machine in ("m1", "m2", "m1", "m2", "m1"):
        seen.update(machines=(machine,))
        status = poll(guardian, clock)
    assert stops == [] and status["stop_cause"] is None


def test_the_guardian_keeps_no_raw_machine_id(world):
    guardian, clock, seen, started, stops = world
    seen.update(machines=("secret-machine-id",))
    poll(guardian, clock)
    rows = guardian.db.execute("SELECT machine FROM prestart_machines").fetchall()
    assert rows and all("secret" not in r[0] for r in rows)


def test_the_events_read_is_claimed_once_and_only_for_a_stop(world):
    guardian, clock, seen, started, stops = world
    assert guardian.claim_start_events_read(OPERATION) is False  # not stopping
    guardian.cancel(OPERATION)
    assert guardian.claim_start_events_read(OPERATION) is True
    assert guardian.claim_start_events_read(OPERATION) is False  # never repeated


def test_no_events_read_after_the_output_was_retained(world):
    guardian, clock, seen, started, stops = world
    guardian.complete(OPERATION)
    assert guardian.claim_start_events_read(OPERATION) is False


def test_only_a_closed_events_record_is_kept(world):
    guardian, clock, seen, started, stops = world
    guardian.cancel(OPERATION)
    guardian.record_start_events(
        OPERATION, dict(outcome="read", events=["Instance Exited"], exit_codes=[1], machines=1)
    )
    assert "start_events" not in guardian.status(OPERATION)
    guardian.record_start_events(
        OPERATION,
        dict(outcome="read", events=["instance_exited"], exit_codes=[127], machines=2),
    )
    assert guardian.status(OPERATION)["start_events"] == dict(
        outcome="read", events=["instance_exited"], exit_codes=[127], machines=2
    )


# -- the events read -------------------------------------------------------------------------

EVENTS_BODY = {
    "organization_name": "acme",
    "page_min_time": "2026-11-25T00:00:00Z",
    "page_max_time": "2026-11-25T01:00:00Z",
    "items": [
        {
            "time": "2026-11-25T00:10:00Z",
            "receive_time": "2026-11-25T00:10:01Z",
            "severity": "error",
            "resource": {
                "type": "instance_controller",
                "labels": {"machine_id": "host-aaa.internal", "instance_id": "i-1"},
            },
            "text_log": "Instance Exited:127 (Error) on host-aaa.internal",
        },
        {
            "time": "2026-11-25T00:09:00Z",
            "receive_time": "2026-11-25T00:09:01Z",
            "severity": "info",
            "resource": {"type": "instance_controller", "labels": {"machine_id": "host-bbb"}},
            "json_log": {"message": "Instance Started"},
        },
    ],
}


def test_the_reduction_keeps_closed_tokens_only():
    summary = summarize_events(EVENTS_BODY["items"])
    assert summary == {
        "events": ["instance_started", "instance_exited"],
        "exit_codes": [127],
        "machines": 2,
        "empty": False,
    }
    assert "host-" not in json.dumps(summary) and "Error" not in json.dumps(summary)


def test_the_transport_makes_one_bounded_org_scoped_query_and_stores_no_body():
    f = Factory((200, EVENTS_BODY))
    result = run(f, "read_events")
    assert result.status == "observed"
    assert result.observation["exit_codes"] == [127] and result.observation["machines"] == 2
    assert "host-aaa" not in json.dumps(result.observation) and "Error" not in json.dumps(
        result.observation
    )
    assert len(f.calls) == 1
    method, path, body, _ = f.calls[0]
    assert method == "POST" and path == "/api/public/organizations/acme/log-entries"
    sent = json.loads(body)
    assert sent["page_size"] <= 100 and sent["sort_order"] == "desc"
    assert 'resource.type = "instance_controller"' in sent["query"]
    assert 'project_name = "research"' in sent["query"]
    assert 'container_group_name = "attempt-' in sent["query"]
    assert set(sent) == {"start_time", "end_time", "page_size", "sort_order", "query"}


def test_a_429_on_the_events_read_is_one_request_and_labelled():
    f = Factory((429, {"message": "daily limit"}))
    result = run(f, "read_events")
    assert len(f.calls) == 1 and result.status == "unknown"
    assert result.detail == "rate_limited" and result.request_kind == "read_events"


def _service(guardian, send):
    row = {"provider_id": ID, "operation": OPERATION}
    service = SimpleNamespace(
        _row=lambda reservation: row,
        guardian=guardian,
        _bounded_send=send,
        diagnostic_write_failures=0,
        _start_events_done=[],
        _start_events_threads=[],
    )
    service._drain_start_events = lambda: SaladSafetyService._drain_start_events(service)
    return service


def read_events(service):
    """Run the read, wait for its thread (a test only), then let the loop thread record it."""
    SaladSafetyService._read_start_events(service, "r")
    for thread in list(service._start_events_threads):
        thread.join(5)
    service._drain_start_events()


def test_the_service_stores_the_exit_code_and_no_body_text(world):
    guardian, clock, seen, started, stops = world
    guardian.cancel(OPERATION)
    f = Factory((200, EVENTS_BODY))
    send = lambda row, kind, pid: run(f, kind)  # noqa: E731
    read_events(_service(guardian, send))
    events = guardian.status(OPERATION)["start_events"]
    assert events["outcome"] == "read" and events["exit_codes"] == [127]
    dumped = json.dumps(guardian.status(OPERATION))
    assert "host-aaa" not in dumped and "Instance Exited" not in dumped


def test_a_429_is_recorded_once_and_never_retried_and_the_stop_completes(world):
    guardian, clock, seen, started, stops = world
    calls = []

    def send(row, kind, pid):
        calls.append(kind)
        raise ProviderUnknown("provider_unavailable", "rate_limited", None, "read_events")

    service = _service(guardian, send)
    guardian.cancel(OPERATION)
    for _ in range(3):
        read_events(service)
    assert calls == ["read_events"]
    assert guardian.status(OPERATION)["start_events"]["outcome"] == "log_quota_exhausted"
    poll(guardian, clock)
    assert stops  # the stop is dispatched by the guardian, independent of the read


def test_a_failed_events_read_is_recorded_unavailable_not_retried(world):
    guardian, clock, seen, started, stops = world
    calls = []

    def send(row, kind, pid):
        calls.append(kind)
        raise RuntimeError("boom")

    service = _service(guardian, send)
    guardian.cancel(OPERATION)
    read_events(service)
    read_events(service)
    assert len(calls) == 1
    assert guardian.status(OPERATION)["start_events"]["outcome"] == "unavailable"


# -- readback and receipts -------------------------------------------------------------------


def _readback(group_state, provider_id=ID):
    result = ProxyResult(
        status="observed",
        reason="observed",
        observation=dict(
            provider_id=provider_id, state=group_state, observed_at="2026-11-25T00:00:00Z"
        ),
    )
    return TrustedSaladServer.readback(result, dict(allocation_id=None))


@pytest.mark.parametrize("state", ["pending", "deploying"])
def test_pending_and_deploying_pass_through_as_observations(state):
    readback = _readback(state)
    assert readback.state == state and not readback.termination_confirmed
    assert ProviderReadback.model_validate_json(readback.model_dump_json()).state == state


def test_a_failed_read_and_unknown_statuses_stay_unknown():
    assert _readback("unknown").state == "unknown"
    assert _readback("something_new").state == "unknown"
    failed = TrustedSaladServer.readback(
        ProxyResult(status="unknown", reason="provider_unavailable"), dict(allocation_id=None)
    )
    assert failed.state == "unknown"


def test_the_receipt_carries_closed_start_evidence_only_when_present():
    receipt = {}
    _note_start_evidence(receipt, {})
    assert receipt == {}
    _note_start_evidence(
        receipt,
        {"start_failure": {"machines": 3}, "start_events": {"outcome": "read"}},
    )
    assert receipt["guardian_start_failure"] == {"machines": 3}


class _Rt:
    def __init__(self, final):
        self.final = final

    def _one(self, sql, args):
        return {"guardian_final_json": json.dumps(self.final)}


def test_a_start_failure_is_permanent_and_never_a_fallback_or_infra_class():
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    final = {
        "start_failure": {"machines": 3},
        "start_events": dict(
            outcome="read", events=["instance_exited"], exit_codes=[127], machines=3
        ),
    }
    observation = provider.observe(
        _Rt(final), {"id": "a1", "salad_verdict": "failed", "salad_evidence_digest": "d" * 64}
    )
    assert observation.outcome == "failed_permanent"
    assert observation.failure_class == "start_failure_suspected"
    assert "exit_code=127" in observation.detail
    assert not observation.failure_class.startswith(INFRA_PREFIX)
    assert observation.failure_class not in REASONS


def _final(**extra):
    return {"start_failure": {"machines": 3}, **extra}


def _observe_with(final):
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    return provider.observe(
        _Rt(final), {"id": "a1", "salad_verdict": "failed", "salad_evidence_digest": "d" * 64}
    )


@pytest.mark.parametrize(
    "events",
    [
        None,  # the read did not finish or was never recorded
        dict(outcome="empty", events=[], exit_codes=[], machines=0),
        dict(outcome="log_quota_exhausted", events=[], exit_codes=[], machines=0),
        dict(outcome="unavailable", events=[], exit_codes=[], machines=0),
        dict(outcome="read", events=["instance_started"], exit_codes=[], machines=3),
    ],
)
def test_three_machines_without_a_recorded_exit_is_not_permanent(events):
    final = _final() if events is None else _final(start_events=events)
    observation = _observe_with(final)
    # A healthy instance may be moved across nodes while it allocates or pulls: one fresh
    # attempt may retry (bounded by the reallocation retry limit), never a permanent failure.
    assert observation.outcome == "failed_transient"
    assert observation.failure_class == REALLOCATED_PRECUSTODY
    assert "start_failure_suspected_unconfirmed" in observation.detail


def test_zero_entries_is_recorded_as_empty_not_a_successful_read(world):
    guardian, clock, seen, started, stops = world
    assert (
        summarize_events([])["empty"] is True
        and summarize_events(EVENTS_BODY["items"])["empty"] is False
    )
    f = Factory((200, {"items": []}))
    result = run(f, "read_events")
    assert result.observation["empty"] is True
    guardian.cancel(OPERATION)
    send = lambda row, kind, pid: run(Factory((200, {"items": []})), kind)  # noqa: E731
    read_events(_service(guardian, send))
    assert guardian.status(OPERATION)["start_events"]["outcome"] == "empty"


def test_a_loop_is_still_counted_after_a_brief_running_observation(world):
    guardian, clock, seen, started, stops = world
    started[0] = True  # the service's broad signal: a run was seen
    guardian.custody_work_started = lambda operation: False  # no custody work proper
    seen["state"] = "running"
    for machine in ("m1", "m2", "m3"):
        seen.update(machines=(machine,))
        status = poll(guardian, clock)
    assert status["stop_cause"] == "start_failure_suspected" and stops


def test_custody_work_ends_the_count_even_with_the_narrow_signal(world):
    guardian, clock, seen, started, stops = world
    guardian.custody_work_started = lambda operation: True
    for machine in ("m1", "m2", "m3", "m4"):
        seen.update(machines=(machine,))
        status = poll(guardian, clock)
    assert stops == [] and status["stop_cause"] is None


def test_the_service_custody_work_signal_ignores_a_run_seen_running():
    from tessera_control.salad_safety_service import _machine_ids

    row = dict(
        begun=0,
        output=0,
        input_requests=0,
        output_requests=0,
        seen_running=1,
        first_running_at=5.0,
        allocation_uncertain=0,
        recovery=0,
    )
    service = SimpleNamespace(_row=lambda operation: row, inputs=None)
    assert SaladSafetyService._custody_work_started(service, OPERATION) is False
    assert SaladSafetyService._custody_started(service, OPERATION) is True
    row["begun"] = 1
    assert SaladSafetyService._custody_work_started(service, OPERATION) is True
    # The service reads the machines of a still-unbound group straight from its instance list.
    assert _machine_ids({"instances": [{"machine_id": "a" * 32, "state": "downloading"}]}) == (
        "a" * 32,
    )
    assert _machine_ids({"instances": []}) == () and _machine_ids({}) is None
