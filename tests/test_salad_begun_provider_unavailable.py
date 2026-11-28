"""A begun Salad run the guardian stops because its provider reads failed for the post-start window
(stop cause ``provider_unavailable``) is retryable infrastructure: ``failed_transient /
infra_salad_safety_stop``, counted against ``retry.infra_retries`` and settled at the whole hold.

The one recorded gap: when the GPU runtime notices its custody calls being refused after that
stop and exits 1 before the platform's kill (``[1, 143]``), the non-zero-exit branch of
``observe`` runs first and classifies the attempt as the stage's permanent failure.

Fakes only: the guardian double, the provider's ``observe`` over a stub row and the Salad
campaign world. No provider, no network, no credentials."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_guardian_poststart_read_retry import poll, world  # noqa: F401
from test_salad_guardian_prestart_read import Unknown
from test_salad_runtime_settle_classify import CLEAN_STOPPED, STATUS

from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_SAFETY_STOP,
    SAFETY_STOP_CAUSES,
    infra_attempts,
    is_infra_class,
)
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.salad_container_log import RUNTIME_STATUS_SCHEMA, summarize_container_log
from tessera_control.salad_exit_watch import FAILURE_CLASS, nonzero_exit_codes
from tessera_control.salad_guardian import POSTSTART_READ_RETRY_LIMIT

DIGEST = "d" * 64
CAUSE = "provider_unavailable"


def _events(codes, names=("instance_started", "instance_exited")):
    return dict(outcome="read", events=list(names), exit_codes=list(codes), machines=1)


def status_line(reason, **extra):
    body = {"schema": RUNTIME_STATUS_SCHEMA, "status": "failed", "reason": reason, **extra}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def tail(*lines):
    return summarize_container_log([{"text_log": line} for line in lines])


# The executor's recorded final for such a stop: clean, the guardian's own first stop cause
# projected as ``safety_stop_cause`` (``project_guardian_final``).
STOPPED = dict(CLEAN_STOPPED, reason=CAUSE, safety_stop_cause=CAUSE)
BEGUN_RECEIPT = dict(
    status="inconclusive",
    reason="journey_incomplete",
    stop_confirmed=True,
    guardian_stop_cause=CAUSE,
    custody_work_begun=True,
)


class _Rt:
    def __init__(self, final, receipt=BEGUN_RECEIPT):
        self.final, self.receipt = final, receipt

    def _one(self, sql, args):
        return {
            "guardian_final_json": json.dumps(self.final),
            "live_receipt_json": None if self.receipt is None else json.dumps(self.receipt),
        }


def _observe(final, receipt=BEGUN_RECEIPT, **attempt):
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    base = {"id": "a1", "salad_verdict": "failed", "salad_evidence_digest": DIGEST}
    return provider.observe(_Rt(final, receipt), {**base, **attempt})


def _safety_stop(observation):
    return (observation.outcome, observation.failure_class) == (
        "failed_transient",
        INFRA_SALAD_SAFETY_STOP,
    )


# -- 1. the guardian records it as the first stop cause; the executor projects it -----------


def test_a_begun_run_stopped_on_failed_reads_records_provider_unavailable_first(world):  # noqa: F811
    guardian, clock, seen, started, stops = world
    assert started[0] is True  # custody began
    seen["fail"] = Unknown()
    for _ in range(POSTSTART_READ_RETRY_LIMIT):
        status = poll(guardian, clock)
    assert status["stop_cause"] == CAUSE and len(stops) == 1
    # Its first stop cause survives later causes (the stop's own heartbeat silence, etc.).
    status = poll(guardian, clock, 60, heartbeat=False)
    assert status["stop_cause"] == CAUSE
    # The executor's projection carries it, and it is in the closed safety-stop table.
    assert CAUSE in SAFETY_STOP_CAUSES
    projected = project_guardian_final(dict(STATUS, stop_cause=status["stop_cause"]))
    assert projected is not None and projected["safety_stop_cause"] == CAUSE
    assert is_infra_class(INFRA_SALAD_SAFETY_STOP)


# -- 2. observe: no earlier branch catches it -------------------------------------------------


@pytest.mark.parametrize(
    "start_events",
    [
        None,  # no exit read recorded
        _events([], names=("instance_started",)),  # no process exit seen
        _events([143]),  # the stop's SIGTERM exit
        _events([137]),  # the stop's SIGKILL
        _events([143, 137]),
        _events([137, 1]),  # a kill first: not the workload's exit
        _events([0]),
    ],
)
def test_a_begun_provider_unavailable_stop_with_a_platform_or_no_exit_is_a_safety_stop(
    start_events,
):
    final = dict(STOPPED) if start_events is None else dict(STOPPED, start_events=start_events)
    if start_events is not None:
        assert nonzero_exit_codes(start_events) == []
    observation = _observe(final)
    assert _safety_stop(observation)
    assert CAUSE in observation.detail and len(observation.detail) <= 500
    # Repeated observe (stored class and detail present) gives the same answer.
    again = _observe(
        final, failure_class=observation.failure_class, failure_detail=observation.detail
    )
    assert _safety_stop(again)


def test_a_platform_exit_with_a_custody_status_line_is_still_a_safety_stop():
    final = dict(STOPPED, start_events=_events([143]), container_log=tail(status_line("custody")))
    assert _safety_stop(_observe(final))


def test_only_the_guardians_recorded_cause_selects_it_never_the_receipt():
    # The receipt names the cause but the recorded final does not: not a safety stop.
    observation = _observe(dict(CLEAN_STOPPED, reason=CAUSE))
    assert observation.failure_class != INFRA_SALAD_SAFETY_STOP


# -- 3. the runtime exits 1 on refused custody after the stop: today's gap ---------------------

# The GPU runtime is PID 1 (``run`` execs python) and installs a SIGTERM handler only while it
# lingers after a retained/reported result, so during the run the stop's SIGTERM does not end
# it; its next custody call is refused, ``Failure("custody")`` prints a status line with reason
# ``custody`` and the process exits 1 before the platform's kill: start events ``[1, 143]``.
CUSTODY_EXIT = dict(
    STOPPED,
    start_events=_events([1, 143]),
    container_log=tail(status_line("custody", step="output")),
)


def test_the_custody_exit_reads_as_a_workload_exit_code():
    assert nonzero_exit_codes(CUSTODY_EXIT["start_events"]) == [1]


def test_a_custody_refusal_exit_after_a_provider_unavailable_stop_is_a_safety_stop():
    observation = _observe(CUSTODY_EXIT)
    assert _safety_stop(observation)
    assert "exited non-zero on a refused custody call" in observation.detail


def test_without_a_status_line_the_exit_after_the_stop_stays_permanent():
    final = dict(CUSTODY_EXIT)
    final.pop("container_log")
    observation = _observe(final)
    assert (observation.outcome, observation.failure_class) == ("failed_permanent", FAILURE_CLASS)


@pytest.mark.parametrize("reason", ["entry", "entry_timeout"])
def test_an_entry_status_line_stays_the_stages_permanent_failure(reason):
    final = dict(
        STOPPED,
        start_events=_events([1, 143]),
        container_log=tail(status_line(reason, exit_code=1)),
    )
    observation = _observe(final)
    assert (observation.outcome, observation.failure_class) == ("failed_permanent", FAILURE_CLASS)
    assert not is_infra_class(observation.failure_class)


# -- 4. end to end: one infra retry, settled at the hold; the cap still stops -----------------


def _world(tmp_path, *, infra):
    return SaladWorld(
        tmp_path,
        stages=lambda env: [salad_stage(env, retry={"max_attempts": 1, "infra_retries": infra})],
    )


def _stopped_run(salad_world, final=STOPPED):
    salad_world.to_route_started()
    salad_world.run_to_outcome(
        passed=False, outputs=False, guardian_final=final, receipt_extra=BEGUN_RECEIPT
    )


def _attempts(salad_world):
    return salad_world.env.query("SELECT * FROM campaign_stage_attempts ORDER BY attempt")


@pytest.mark.parametrize("start_events", [None, _events([143])])
def test_it_spends_one_infra_retry_and_settles_at_the_whole_hold(tmp_path, start_events):
    salad_world = _world(tmp_path, infra=1)
    final = STOPPED if start_events is None else dict(STOPPED, start_events=start_events)
    _stopped_run(salad_world, final)
    salad_world.tick_until(lambda: len(_attempts(salad_world)) >= 2)
    first, second = _attempts(salad_world)[:2]
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_SALAD_SAFETY_STOP,
    )
    assert CAUSE in first["failure_detail"]
    assert first["salad_verdict"] != "ok"
    assert first["settled_cents"] == first["hold_cents"]  # a safety stop spends the hold
    assert infra_attempts(_attempts(salad_world)) == 1
    assert second["attempt"] == 2 and second["grant_id"] != first["grant_id"]


def test_with_no_infra_retry_left_the_stage_fails_under_the_safety_stop_class(tmp_path):
    salad_world = _world(tmp_path, infra=0)
    _stopped_run(salad_world)
    salad_world.tick_until(lambda: salad_world.status() == "failed")
    attempt = salad_world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_SAFETY_STOP,
    )
    stop = salad_world.row("SELECT stop_reason FROM campaigns WHERE id=?", salad_world.campaign_id)
    assert stop["stop_reason"] == f"stage_failed:g1:{INFRA_SALAD_SAFETY_STOP}"
    assert len(_attempts(salad_world)) == 1
