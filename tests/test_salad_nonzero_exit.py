"""A Salad container that exits non-zero is the workload's failure, and its group is stopped.

Fakes only: no provider, no network, no credentials."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_salad_start_failure import OPERATION, world  # noqa: E402,F401

from tessera_control.campaign_infra_retry import (
    REALLOCATED_PRECUSTODY,
    SALAD_INTERRUPTED,
    is_infra_class,
)
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.salad_custody import CustodyBinding
from tessera_control.salad_exit_watch import (
    FAILURE_CLASS,
    NONZERO_EXIT_CAUSE,
    WATCH_MAX_READS,
    ContainerExitWatch,
    container_exit_receipt,
    nonzero_exit_codes,
)
from tessera_control.salad_guardian import SERVICE_STOP_CAUSES
from tessera_control.salad_live_check import _note_start_evidence

DIGEST = "d" * 64


def _events(codes, names=("instance_started", "instance_exited"), outcome="read"):
    return dict(outcome=outcome, events=list(names), exit_codes=list(codes), machines=2)


class _Rt:
    def __init__(self, final):
        self.final = final

    def _one(self, sql, args):
        return {"guardian_final_json": json.dumps(self.final)}


def _observe(final, **attempt):
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    base = {"id": "a1", "salad_verdict": "failed", "salad_evidence_digest": DIGEST}
    return provider.observe(_Rt(final), {**base, **attempt})


INTERRUPTED = dict(
    failure_class=SALAD_INTERRUPTED,
    failure_detail="salad:salad_interrupted: signal=guardian_evidence; moved",
)


def test_a_nonzero_exit_is_a_permanent_workload_failure_not_an_interruption():
    # The stored class (and detail) say salad_interrupted: the recorded exit wins.
    observation = _observe({"start_events": _events([1])}, **INTERRUPTED)
    assert observation.outcome == "failed_permanent"
    assert observation.failure_class == FAILURE_CLASS == "salad_container_exited_nonzero"
    assert "code 1" in observation.detail and len(observation.detail) <= 500
    assert observation.failure_class in FAILURE_CLASSES
    # Not an infra class: it never consumes retry.infra_retries, nor the reallocation budget.
    assert not is_infra_class(observation.failure_class)
    assert observation.failure_class not in {SALAD_INTERRUPTED, REALLOCATED_PRECUSTODY}


def test_a_nonzero_exit_beats_a_precustody_reallocation_detail_too():
    observation = _observe(
        {"start_events": _events([2])},
        failure_detail=f"salad:{REALLOCATED_PRECUSTODY}: signal=guardian_evidence; moved",
    )
    assert observation.outcome == "failed_permanent"
    assert observation.failure_class == FAILURE_CLASS


@pytest.mark.parametrize(
    "start_events",
    [
        None,  # no read recorded
        _events([], names=("instance_started",)),  # moved, no process exit
        _events([], names=("instance_allocated",), outcome="empty"),
        _events([0]),  # a clean exit
        _events([], outcome="unavailable"),
        _events([1], outcome="log_quota_exhausted"),  # not a successful read: not evidence
    ],
)
def test_a_reallocation_without_a_nonzero_process_exit_classifies_as_before(start_events):
    final = {} if start_events is None else {"start_events": start_events}
    interrupted = _observe(final, **INTERRUPTED)
    assert interrupted.outcome == "failed_transient"
    assert interrupted.failure_class == SALAD_INTERRUPTED
    precustody = _observe(
        final,
        failure_class=REALLOCATED_PRECUSTODY,
        failure_detail=f"salad:{REALLOCATED_PRECUSTODY}: signal=guardian_evidence",
    )
    assert precustody.outcome == "failed_transient"
    assert precustody.failure_class == REALLOCATED_PRECUSTODY


# First-seen order: a kill or platform stop that came first. (A non-zero exit before the kill,
# ``[1, 137]``, is the workload's failure: test_salad_exit_watch_followups.py.)
@pytest.mark.parametrize("codes", [[137], [143], [137, 1], [137, 143]])
def test_a_kill_or_platform_stop_is_unchanged(codes):
    assert nonzero_exit_codes(_events(codes)) == []
    observation = _observe({"start_events": _events(codes)}, **INTERRUPTED)
    assert observation.outcome == "failed_transient"
    assert observation.failure_class == SALAD_INTERRUPTED


def test_the_start_failure_class_is_unchanged_by_an_exit_code():
    final = {"start_failure": {"machines": 3}, "start_events": _events([127])}
    assert _observe(final).failure_class == "start_failure_suspected"


def test_the_receipt_names_the_exit_and_says_stderr_was_unavailable():
    receipt = {}
    _note_start_evidence(receipt, {"start_events": _events([1])})
    assert receipt["guardian_container_exit"] == {
        "exit_codes": [1],
        "stderr_tail": {"outcome": "unavailable", "reason": "not_read"},
    }
    # Closed tokens only: no text, no identifier, bounded.
    assert len(json.dumps(receipt)) < 2048
    quiet = {}
    _note_start_evidence(quiet, {"start_events": _events([], names=("instance_started",))})
    assert "guardian_container_exit" not in quiet
    assert container_exit_receipt(None) is None


def test_the_campaign_receipt_carries_the_exit(monkeypatch):
    monkeypatch.setattr(SaladRouteProvider, "_late_bound_inputs", staticmethod(lambda c, a: None))
    monkeypatch.setattr(SaladRouteProvider, "_termination", staticmethod(lambda c, a, r: None))
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    final = {"start_events": _events([1])}
    row = {"phase": "done", "route_id": None, "guardian_final_json": json.dumps(final)}
    connection = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: row))
    attempt = {"id": 1, "salad_verdict": "failed", "salad_evidence_digest": DIGEST}
    fields = provider.receipt_fields(connection, attempt)
    assert fields["guardian_container_exit"]["exit_codes"] == [1]


# -- the group stop ------------------------------------------------------------------------


def test_the_stop_cause_is_a_closed_service_cause():
    assert NONZERO_EXIT_CAUSE in SERVICE_STOP_CAUSES


class _Service(ContainerExitWatch):
    """Just enough of the safety service for the watch, with recorded effects."""

    def __init__(self, guardian, records, row=None, clock=lambda: 100.0):
        self.guardian = guardian
        self.clock = clock
        self.diagnostic_write_failures = 0
        self.inputs = None
        self.row = row or dict(
            provider_id="p" * 32,
            operation="op",
            recovery=0,
            output=0,
            seen_running=1,
            binding=json.dumps(asdict(CustodyBinding("t", "p", "op", 1, "a"))),
        )
        self.effects = []
        self.records = records
        self.db = _Db(self)
        self._init_exit_watch()

    def _row(self, reservation):
        return self.row

    def _bounded_send(self, row, action, provider_id):
        assert action == "read_events"
        self.effects.append("read")
        return SimpleNamespace(observation=self.records.pop(0))

    def _first_recovery_cause(self, reservation, cause):
        self.effects.append(("cause", cause))

    def _frame_note(self, reservation, kind, detail=None, *, once=False):
        self.effects.append(("note", kind, detail))

    @property
    def custody(self):
        return SimpleNamespace(close_writers=lambda b: self.effects.append("close_writers"))

    def _assignment(self, row):
        return "assignment"

    def _cancel(self, operation):
        self.effects.append(("cancel", operation))

    def finish(self):
        state = self._exit_watch["r"]
        if state["thread"] is not None:
            state["thread"].join(5)


class _Db:
    def __init__(self, service):
        self.service = service

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=()):
        if sql.startswith("UPDATE reservations SET recovery=1"):
            self.service.row["recovery"] = 1
            self.service.effects.append("recovery")


class _Guardian:
    def __init__(self, status="armed", reason="armed"):
        self.state = {"status": status, "reason": reason}
        self.armed = []
        self.recorded = []

    def status(self, operation):
        return self.state

    def arm(self, assignment, *, stop_only=False, cause=None):
        self.armed.append(stop_only)

    def record_start_events(self, operation, record):
        self.recorded.append(record)

    def claim_container_log_read(self, operation):
        return False  # the container-log tail is covered in test_salad_exit_watch_followups.py

    def record_container_log(self, operation, value):
        pass


def _observation(codes, names=("instance_started", "instance_exited")):
    return dict(events=list(names), exit_codes=list(codes), machines=1, empty=not names)


def _run(service):
    """One watch pass that reads, then one that acts on the answer."""
    service._watch_container_exit("r")
    service.finish()
    service._watch_container_exit("r")


def test_a_nonzero_exit_stops_the_group_with_its_cause():
    guardian = _Guardian()
    service = _Service(guardian, [_observation([1])])
    _run(service)
    assert ("cause", NONZERO_EXIT_CAUSE) in service.effects
    assert ("cancel", "op") in service.effects and "close_writers" in service.effects
    assert guardian.armed == [True]  # stop-only arm: the exact-stop path, nothing inferred
    assert guardian.recorded and guardian.recorded[0]["exit_codes"] == [1]
    assert service.row["recovery"] == 1
    assert service.diagnostic_write_failures == 0


@pytest.mark.parametrize(
    "records",
    [
        [_observation([])],  # no exit seen
        [_observation([0])],  # a clean exit
        [_observation([137])],  # kill: today's handling
        [_observation([143])],
        [_observation([1], names=("instance_started",))],  # no exit event: not evidence
    ],
)
def test_no_stop_without_a_nonzero_process_exit(records):
    guardian = _Guardian()
    service = _Service(guardian, records)
    _run(service)
    assert guardian.armed == [] and ("cancel", "op") not in service.effects
    assert service.row["recovery"] == 0


def test_an_unreadable_read_never_stops_and_never_raises():
    guardian = _Guardian()
    service = _Service(guardian, [])  # the fake read raises (empty list pop)
    _run(service)
    assert guardian.armed == [] and service.row["recovery"] == 0


@pytest.mark.parametrize(
    "override",
    [
        dict(recovery=1),
        dict(output=1),  # an output was retained: the run succeeded
        dict(seen_running=0),
        dict(provider_id=None),
    ],
)
def test_the_watch_does_not_read_for_a_frame_that_is_not_a_running_one(override):
    service = _Service(_Guardian(), [_observation([1])])
    service.row.update(override)
    service._watch_container_exit("r")
    assert "read" not in service.effects


def test_the_watch_does_not_read_for_a_group_already_stopping():
    service = _Service(_Guardian(status="armed", reason="workload_failed"), [_observation([1])])
    service._watch_container_exit("r")
    assert "read" not in service.effects


def test_the_watch_reads_at_most_once_per_interval_and_a_bounded_number_of_times():
    now = [100.0]
    service = _Service(
        _Guardian(), [_observation([]) for _ in range(WATCH_MAX_READS + 5)], clock=lambda: now[0]
    )
    for _ in range(3):  # within one interval: a single read
        service._watch_container_exit("r")
        service.finish()
        service._watch_container_exit("r")
    assert service.effects.count("read") == 1
    for _ in range(WATCH_MAX_READS + 10):
        now[0] += 241  # past the longest backed-off gap
        service._watch_container_exit("r")
        service.finish()
        service._watch_container_exit("r")
    assert service.effects.count("read") == WATCH_MAX_READS


def test_the_real_guardian_keeps_the_cause_as_its_stop_cause(world):  # noqa: F811
    guardian, clock, seen, started, stops = world
    guardian.service_stop_cause = lambda operation: NONZERO_EXIT_CAUSE
    guardian.cancel(OPERATION)
    assert guardian.status(OPERATION)["stop_cause"] == NONZERO_EXIT_CAUSE


@pytest.mark.parametrize("running_seconds", [0, 30, 60, 600])
def test_a_nonzero_exit_that_also_hit_the_route_limit_is_the_workload_failure(running_seconds):
    # q-2711 x ADR 0206: a container that exits non-zero within its first minute and whose route
    # also reached its limit (a provisioning-overrun record when nothing else is known). The
    # exit watch normally stops the group first; when both are recorded, the recorded non-zero
    # exit wins: permanent, never an infra retry that would rerun a crashing workload.
    final = {
        "start_events": _events([1]),
        "route_deadline": {
            "limit": "route_deadline",
            "running_seconds": running_seconds,
            "custody_work_begun": False,
        },
    }
    observation = _observe(final)
    assert observation.outcome == "failed_permanent"
    assert observation.failure_class == FAILURE_CLASS
    assert not is_infra_class(observation.failure_class)
    assert "code 1" in observation.detail
