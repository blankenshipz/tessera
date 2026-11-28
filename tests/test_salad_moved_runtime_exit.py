"""A non-zero container exit on a provider-moved Salad frame is retryable infrastructure when the
GPU runtime's own failure was a refused custody or input call (or custody never began), and stays
the stage's permanent failure otherwise.

Fakes only: the provider's ``observe`` over a stub row, and the Salad campaign world."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_salad_interruption_retry import CUSTODY_BEGAN, FINAL, interrupted, world_for

from tessera_control.campaign_infra_retry import INFRA_SALAD_REALLOCATED, infra_attempts
from tessera_control.campaign_models import DEFAULT_INFRA_EXIT_CODES
from tessera_control.campaign_provider_salad import (
    RUNTIME_EXIT_SIGNAL,
    SaladRouteProvider,
    _runtime_exit_on_moved_frame,
)
from tessera_control.salad_container_log import (
    RUNTIME_STATUS_SCHEMA,
    runtime_status_lines,
    summarize_container_log,
    unavailable,
)
from tessera_control.salad_exit_watch import FAILURE_CLASS
from tessera_control.salad_guardian import RECONCILE_PROVIDER

DIGEST = "d" * 64
EXIT_1 = dict(
    outcome="read", events=["instance_started", "instance_exited"], exit_codes=[1], machines=2
)


def status_line(reason, **extra):
    body = {"schema": RUNTIME_STATUS_SCHEMA, "status": "failed", "reason": reason, **extra}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def tail(*lines):
    return summarize_container_log([{"text_log": line} for line in lines])


MOVED = dict(FINAL, start_events=EXIT_1)  # stopped on allocation_changed, allocation_uncertain
RECONCILED = dict(MOVED, status="reconciled", reason=RECONCILE_PROVIDER)
NOT_MOVED = dict(MOVED, allocation_uncertain=False, reason="workload_failed:custody")
NOT_BEGUN = dict(CUSTODY_BEGAN, custody_work_begun=False)


class _Rt:
    def __init__(self, final, receipt):
        self.final, self.receipt = final, receipt

    def _one(self, sql, args):
        return {
            "guardian_final_json": json.dumps(self.final),
            "live_receipt_json": None if self.receipt is None else json.dumps(self.receipt),
        }


def observe(final, receipt=CUSTODY_BEGAN):
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    attempt = {"id": "a1", "salad_verdict": "failed", "salad_evidence_digest": DIGEST}
    return provider.observe(_Rt(final, receipt), attempt)


def retryable(observation):
    return (observation.outcome, observation.failure_class) == (
        "failed_transient",
        INFRA_SALAD_REALLOCATED,
    )


def permanent(observation):
    return (observation.outcome, observation.failure_class) == ("failed_permanent", FAILURE_CLASS)


# -- the four live shapes and their fail-closed neighbours ----------------------------------


def test_an_input_fetch_failure_after_an_allocation_change_is_retryable():
    observation = observe(
        dict(MOVED, container_log=tail("x", status_line("input_fetch", step="chunk")))
    )
    assert retryable(observation)
    assert observation.detail.startswith(
        f"{INFRA_SALAD_REALLOCATED}: signal={RUNTIME_EXIT_SIGNAL};"
    )
    assert "input_fetch, step chunk" in observation.detail


def test_a_custody_failure_on_a_provider_confirmed_close_is_retryable():
    assert retryable(observe(dict(RECONCILED, container_log=tail(status_line("custody")))))


@pytest.mark.parametrize("reason", ["entry", "entry_timeout"])
def test_an_entry_failure_stays_the_stages_permanent_failure(reason):
    assert permanent(observe(dict(MOVED, container_log=tail(status_line(reason, exit_code=1)))))


def test_any_entry_line_keeps_it_permanent_even_beside_a_custody_line():
    # The tail is untrusted (the stage writes to the same stream): an entry line anywhere wins.
    for lines in (
        (status_line("custody"), status_line("entry")),
        (status_line("entry"), status_line("custody")),
    ):
        assert permanent(observe(dict(MOVED, container_log=tail(*lines))))


@pytest.mark.parametrize(
    "log", [None, unavailable("read_failed"), unavailable("not_read"), tail("no status here")]
)
def test_an_unknown_cause_with_custody_begun_stays_permanent(log):
    final = dict(MOVED) if log is None else dict(MOVED, container_log=log)
    assert permanent(observe(final, CUSTODY_BEGAN))


@pytest.mark.parametrize(
    "receipt", [CUSTODY_BEGAN, dict(CUSTODY_BEGAN, custody_work_begun=None), None]
)
def test_custody_must_be_known_never_begun_to_retry_without_a_status_line(receipt):
    assert permanent(observe(dict(MOVED, container_log=unavailable("read_failed")), receipt))


@pytest.mark.parametrize(
    "log", [None, unavailable("read_failed"), unavailable("data_class_sealed")]
)
def test_custody_never_begun_on_a_moved_frame_is_retryable_without_a_tail(log):
    final = dict(MOVED) if log is None else dict(MOVED, container_log=log)
    observation = observe(final, NOT_BEGUN)
    assert retryable(observation) and "custody never began" in observation.detail


@pytest.mark.parametrize("reason", ["manifest", "argv", "fetch", "install", "output", "internal"])
def test_a_stage_side_runtime_reason_stays_permanent_even_before_custody(reason):
    # A pre-claim failure is the stage's own; a crash loop is what makes the provider move it.
    assert permanent(observe(dict(MOVED, container_log=tail(status_line(reason))), NOT_BEGUN))


@pytest.mark.parametrize(
    "final",
    [
        NOT_MOVED,
        dict(MOVED, allocation_uncertain=False),
        dict(MOVED, reason="deadline"),
        dict(RECONCILED, reason="operator_attested"),
    ],
)
def test_without_the_guardians_own_move_record_it_stays_permanent(final):
    assert permanent(observe(dict(final, container_log=tail(status_line("custody"))), NOT_BEGUN))


def test_exit_one_is_never_an_infrastructure_exit_code():
    assert 1 not in DEFAULT_INFRA_EXIT_CODES


def test_the_classifier_is_pure_and_repeatable():
    final = dict(MOVED, container_log=tail(status_line("output_plan", step="output_plan")))
    first = _runtime_exit_on_moved_frame(final, CUSTODY_BEGAN)
    assert first is not None and first == _runtime_exit_on_moved_frame(final, CUSTODY_BEGAN)
    assert observe(final).detail == observe(final).detail


# -- the status line through the log tail -----------------------------------------------------


def test_a_parsed_json_log_status_keeps_its_step_and_integer_exit_code():
    body = {
        "schema": RUNTIME_STATUS_SCHEMA,
        "status": "failed",
        "reason": "custody",
        "step": "upload",
        "exit_code": 1,
        "outputs": ["kept out"],
    }
    log = summarize_container_log([{"json_log": body}])
    assert runtime_status_lines(log) == [
        {"status": "failed", "reason": "custody", "step": "upload", "exit_code": 1}
    ]
    assert "kept out" not in json.dumps(log)


def test_an_unknown_step_or_odd_token_is_dropped():
    line = json.dumps(
        {"schema": RUNTIME_STATUS_SCHEMA, "status": "failed", "reason": "X Y", "step": "rm"}
    )
    assert runtime_status_lines(tail(line)) == [{"status": "failed"}]


def test_other_json_log_entries_keep_todays_string_values():
    log = summarize_container_log([{"json_log": {"msg": "hello", "n": 3}}])
    assert log["lines"] == ["hello"]


# -- end to end: retry bound and settlement ---------------------------------------------------


MOVED_INPUT = dict(MOVED, container_log=tail(status_line("input_fetch", step="manifest")))


def _first(world):
    return world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )[0]


def test_it_spends_one_infra_retry_and_settles_at_the_estimate_once(tmp_path):
    world = world_for(tmp_path, infra=1)
    interrupted(world, final=MOVED_INPUT)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_SALAD_REALLOCATED,
    )
    assert RUNTIME_EXIT_SIGNAL in first["failure_detail"]
    # The guardian's estimate (3.5) rounded up, within the hold, provisional.
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert infra_attempts(world.env.query("SELECT * FROM campaign_stage_attempts")) == 1
    for _ in range(3):
        world.tick()
    again = _first(world)
    assert (again["settled_cents"], again["settlement_basis"], again["revision"]) == (
        4,
        "guardian_bounded_hold",
        first["revision"],
    )


def test_with_no_infra_retry_left_the_stage_fails_with_the_infrastructure_class(tmp_path):
    world = world_for(tmp_path, infra=0)
    interrupted(world, final=MOVED_INPUT)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_REALLOCATED,
    )
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1


def test_an_entry_failure_on_a_moved_frame_fails_the_stage_without_a_retry(tmp_path):
    world = world_for(tmp_path, infra=2)
    interrupted(world, final=dict(MOVED, container_log=tail(status_line("entry", exit_code=1))))
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == ("failed_permanent", FAILURE_CLASS)
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1
