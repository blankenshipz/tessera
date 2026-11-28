"""A non-zero exit at the input read or a custody call, on a frame the provider did not move, is
retryable infrastructure when the guardian refused the run's calls on its own failed provider
reads (ADR 0227). Entry failures, other runtime reasons and unknown causes stay permanent.

Fakes only: the guardian service with a fake provider, the provider's ``observe`` over a stub
row, and the Salad campaign world."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_guardian_transient_read_tolerance import OUTAGE, long_case  # noqa: F401
from test_salad_interruption_retry import CUSTODY_BEGAN, FINAL, interrupted, world_for

from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_PROVIDER_READ_FAILED,
    PLACEMENT_RETRY_CLASSES,
    infra_attempts,
    is_infra_class,
)
from tessera_control.campaign_models import DEFAULT_INFRA_EXIT_CODES, FAILURE_CLASSES
from tessera_control.campaign_provider_salad import (
    READ_FAILURE_SIGNAL,
    SaladRouteProvider,
    _runtime_exit_on_read_failures,
)
from tessera_control.campaign_report import _RETRY_ENDED
from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.campaign_store import _GuardianFinal
from tessera_control.salad_container_log import (
    RUNTIME_STATUS_SCHEMA,
    summarize_container_log,
    unavailable,
)
from tessera_control.salad_exit_watch import FAILURE_CLASS
from tessera_control.salad_safety_service import OUTPUT_PLAN_PATH

DIGEST = "d" * 64
EXIT_1 = dict(
    outcome="read", events=["instance_started", "instance_exited"], exit_codes=[1], machines=1
)


def status_line(reason, **extra):
    body = {"schema": RUNTIME_STATUS_SCHEMA, "status": "failed", "reason": reason, **extra}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def tail(*lines):
    return summarize_container_log([{"text_log": line} for line in lines])


# The guardian stopped the exited container (no move): clean ``stopped`` final.
STOPPED = dict(
    FINAL,
    reason="container_exited_nonzero",
    allocation_uncertain=False,
    start_events=EXIT_1,
)
READ_FAILED = dict(STOPPED, workload_read_failures=3, last_refusal_read_failure=True)


class _Rt:
    def __init__(self, final, receipt=CUSTODY_BEGAN):
        self.final, self.receipt = final, receipt

    def _one(self, sql, args):
        return {
            "guardian_final_json": json.dumps(self.final),
            "live_receipt_json": json.dumps(self.receipt),
        }


def observe(final):
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    attempt = {"id": "a1", "salad_verdict": "failed", "salad_evidence_digest": DIGEST}
    return provider.observe(_Rt(final), attempt)


def outcome(observation):
    return observation.outcome, observation.failure_class


RETRYABLE = ("failed_transient", INFRA_SALAD_PROVIDER_READ_FAILED)
PERMANENT = ("failed_permanent", FAILURE_CLASS)


# -- classification -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "step"),
    [
        ("input_fetch", "chunk"),
        ("input_fetch", "manifest"),
        ("input_fetch", None),
        ("input_unavailable", "chunk"),
    ],
)
def test_an_input_read_exit_on_refused_reads_is_retryable(reason, step):
    extra = {} if step is None else {"step": step}
    observation = observe(dict(READ_FAILED, container_log=tail(status_line(reason, **extra))))
    assert outcome(observation) == RETRYABLE
    assert observation.detail.startswith(
        f"{INFRA_SALAD_PROVIDER_READ_FAILED}: signal={READ_FAILURE_SIGNAL};"
    )
    assert f"reported {reason}" in observation.detail


@pytest.mark.parametrize("reason", ["entry", "entry_timeout"])
def test_an_entry_exit_stays_permanent_even_with_refused_reads(reason):
    assert outcome(observe(dict(READ_FAILED, container_log=tail(status_line(reason))))) == PERMANENT


@pytest.mark.parametrize(
    "line",
    [
        status_line("custody"),  # an old image: no step, so claim, plan or upload alike
        status_line("custody", step="upload"),
        status_line("custody", step="claim"),
        status_line("custody", step="output_plan"),
        status_line("output_plan", step="output_plan"),
        status_line("input_fetch", step="upload"),  # never an input read
    ],
)
def test_a_custody_or_upload_exit_stays_permanent_even_after_a_read_failure(line):
    # A counted read failure earlier in the run never explains a later custody, plan or upload
    # failure (an output budget, a runtime-local deadline, an incomplete upload).
    assert outcome(observe(dict(READ_FAILED, container_log=tail(line)))) == PERMANENT


def test_a_later_refusal_for_another_reason_keeps_it_permanent():
    # The guardian counted read failures, but the last call it refused was refused for another
    # reason: the flag is absent from the recorded final.
    final = dict(READ_FAILED, container_log=tail(status_line("input_fetch", step="chunk")))
    final.pop("last_refusal_read_failure")
    assert outcome(observe(final)) == PERMANENT


def test_an_entry_line_anywhere_wins_over_a_custody_line():
    for lines in (
        (status_line("custody"), status_line("entry")),
        (status_line("entry"), status_line("input_fetch")),
    ):
        assert outcome(observe(dict(READ_FAILED, container_log=tail(*lines)))) == PERMANENT


@pytest.mark.parametrize(
    "reason", ["output_plan", "output", "manifest", "fetch", "install", "argv", "internal"]
)
def test_any_other_runtime_reason_stays_permanent(reason):
    assert outcome(observe(dict(READ_FAILED, container_log=tail(status_line(reason))))) == PERMANENT


@pytest.mark.parametrize(
    "log", [None, unavailable("read_failed"), unavailable("data_class_sealed"), tail("no status")]
)
def test_an_unknown_cause_stays_permanent(log):
    final = dict(READ_FAILED) if log is None else dict(READ_FAILED, container_log=log)
    assert outcome(observe(final)) == PERMANENT


@pytest.mark.parametrize("count", [None, 0])
def test_without_the_guardians_read_failure_record_it_stays_permanent(count):
    final = dict(STOPPED, container_log=tail(status_line("input_fetch")))
    if count is not None:
        final["workload_read_failures"] = count
    assert outcome(observe(final)) == PERMANENT


def test_exit_codes_are_never_consulted():
    assert 1 not in DEFAULT_INFRA_EXIT_CODES
    final = dict(READ_FAILED, container_log=tail(status_line("input_fetch")))
    assert _runtime_exit_on_read_failures(final) == _runtime_exit_on_read_failures(final)


def test_the_class_is_a_known_retryable_infrastructure_class():
    assert INFRA_SALAD_PROVIDER_READ_FAILED in FAILURE_CLASSES
    assert INFRA_SALAD_PROVIDER_READ_FAILED in PLACEMENT_RETRY_CLASSES
    assert INFRA_SALAD_PROVIDER_READ_FAILED in _RETRY_ENDED
    assert is_infra_class(INFRA_SALAD_PROVIDER_READ_FAILED)


# -- the guardian's record and its projection ----------------------------------------------------


def _guardian_status(s, b):
    return s.admin(dict(operation="status", reservation=b["reservation"]))


INPUT = "/v1/workload/input"


def test_the_guardian_counts_an_input_read_refused_on_a_failed_provider_read(long_case):  # noqa: F811
    s, b, request, token, clock, provider = long_case
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    status = _guardian_status(s, b)
    assert status["workload_read_failures"] == 0 and status["last_refusal_read_failure"] is None
    provider.read_failures = OUTAGE
    for _ in range(2):
        with pytest.raises((ValueError, TimeoutError)):
            s.workload(INPUT, dict(binding=request, request="manifest"), token)
    provider.read_failures = 0
    status = _guardian_status(s, b)
    assert status["workload_read_failures"] == 2 and status["last_refusal_read_failure"] is True


def test_a_later_refusal_for_another_reason_clears_the_last_refusal_flag(long_case):  # noqa: F811
    s, b, request, token, clock, provider = long_case
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    provider.read_failures = OUTAGE
    with pytest.raises((ValueError, TimeoutError)):
        s.workload(INPUT, dict(binding=request, request="manifest"), token)
    provider.read_failures = 0
    with pytest.raises(ValueError):  # consumed: refused after a good provider read
        s.workload("/v1/workload/begin-once", dict(binding=request), token)
    status = _guardian_status(s, b)
    assert status["workload_read_failures"] == 1 and status["last_refusal_read_failure"] is False
    assert "last_refusal_read_failure" not in project_guardian_final(
        _status_for_projection(workload_read_failures=1, last_refusal_read_failure=False)
    )


def test_only_input_reads_are_counted(long_case):  # noqa: F811
    # A claim, output or failure-report call is refused on a failed read too, but its token is
    # only checked after the read: it is not counted, and it clears the flag.
    s, b, request, token, clock, provider = long_case
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    provider.read_failures = OUTAGE
    with pytest.raises((ValueError, TimeoutError)):
        s.workload(OUTPUT_PLAN_PATH, dict(binding=request), token)
    provider.read_failures = 0
    status = _guardian_status(s, b)
    assert status["workload_read_failures"] == 0 and status["last_refusal_read_failure"] is False


def test_a_refusal_for_any_other_reason_is_not_counted(long_case):  # noqa: F811
    s, b, request, token, clock, provider = long_case
    with pytest.raises(ValueError):  # binding mismatch: not this frame's call, not recorded
        s.workload("/v1/workload/begin-once", dict(binding=dict(request, epoch=999)), token)
    assert _guardian_status(s, b)["last_refusal_read_failure"] is None
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    with pytest.raises(ValueError):  # consumed: refused after a good provider read
        s.workload("/v1/workload/begin-once", dict(binding=request), token)
    status = _guardian_status(s, b)
    assert status["workload_read_failures"] == 0 and status["last_refusal_read_failure"] is False


def _status_for_projection(**extra):
    return dict(
        status="stopped",
        reason="container_exited_nonzero",
        orphan=False,
        allocation_uncertain=False,
        escalation_pending=False,
        budget_fenced=False,
        rate_unknown=False,
        published_hourly_rate_usd="0.5",
        estimated_exposure_cents="3.5",
        **extra,
    )


@pytest.mark.parametrize(("value", "kept"), [(2, 2), (0, None), (True, None), ("2", None)])
def test_the_projection_keeps_only_a_positive_integer_count(value, kept):
    projected = project_guardian_final(_status_for_projection(workload_read_failures=value))
    assert projected is not None
    assert projected.get("workload_read_failures") == kept
    _GuardianFinal.model_validate(projected)


def test_an_older_guardian_without_the_count_projects_as_before():
    projected = project_guardian_final(_status_for_projection())
    assert "workload_read_failures" not in projected
    assert "last_refusal_read_failure" not in projected


def test_the_stored_final_keeps_its_shape_without_the_record():
    from tessera_control.campaign_salad_commands import _stored_final

    plain = _stored_final(
        _GuardianFinal.model_validate(project_guardian_final(_status_for_projection()))
    )
    assert "workload_read_failures" not in plain and "last_refusal_read_failure" not in plain
    kept = _stored_final(
        _GuardianFinal.model_validate(
            project_guardian_final(
                _status_for_projection(workload_read_failures=2, last_refusal_read_failure=True)
            )
        )
    )
    assert '"workload_read_failures":2' in kept and '"last_refusal_read_failure":true' in kept


# -- end to end: the retry cap, each retry in its own hold ---------------------------------------


READ_FAILED_INPUT = dict(READ_FAILED, container_log=tail(status_line("input_fetch", step="chunk")))


def _first(world):
    return world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )[0]


def test_it_spends_one_infra_retry_and_the_retry_has_its_own_hold(tmp_path):
    world = world_for(tmp_path, infra=1)
    interrupted(world, final=READ_FAILED_INPUT)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == RETRYABLE
    assert READ_FAILURE_SIGNAL in first["failure_detail"]
    assert first["settled_cents"] is not None and first["settled_cents"] <= first["hold_cents"]
    assert infra_attempts(world.env.query("SELECT * FROM campaign_stage_attempts")) == 1
    second = world.attempt()
    assert second["attempt"] == 2 and second["grant_id"] != first["grant_id"]
    assert second["settled_cents"] is None


def test_with_no_infra_retry_left_the_stage_fails_under_the_class(tmp_path):
    world = world_for(tmp_path, infra=0)
    interrupted(world, final=READ_FAILED_INPUT)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_PROVIDER_READ_FAILED,
    )
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1


def test_an_entry_exit_on_refused_reads_fails_the_stage_without_a_retry(tmp_path):
    world = world_for(tmp_path, infra=2)
    interrupted(world, final=dict(READ_FAILED, container_log=tail(status_line("entry"))))
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == PERMANENT
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1
