"""ADR 0216: every non-final attempt status and Salad phase has an automatic exit and a bound.

The table below is the invariant: it names, for each state an attempt can wait in, the automatic
exit that takes it out and the bound (a named constant) after which that exit applies, plus the
test that drives it. Adding a status or a phase without a row fails here. Rows whose exit is a
clock are driven below with a fake clock past their bound; the others name the focused test that
already drives them (checked to exist), so the table never points at nothing.

``unknown`` is the one state whose exit may still be a person: its automatic exits are listed,
and what is left for a person is the explicit list in ``test_salad_outcome_matrix.py``.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control import campaign_models, campaign_runner, campaign_salad, campaign_schema
from tessera_control import salad_guardian_bound as bound_module
from tessera_control.campaign_salad import SILENT_EXECUTOR_GRACE_SECONDS
from tessera_control.salad_guardian_bound import CHARGE_DEADLINE_SECONDS, CHARGE_OVERDUE

TESTS = Path(__file__).parent
MODULES = {
    "campaign_runner": campaign_runner,
    "campaign_salad": campaign_salad,
    "salad_guardian_bound": bound_module,
}

# Attempt statuses and Salad phases that end an attempt's wait (nothing left to exit).
FINAL_STATUSES = frozenset(
    {"succeeded", "failed_transient", "failed_permanent", "refused_budget", "cancelled"}
)
FINAL_PHASES = frozenset({"settled", "unclaimed"})


def _row(exit: str, bound: str, test: str) -> dict[str, str]:
    return {"exit": exit, "bound": bound, "test": test}


ATTEMPT_STATUSES = {
    "reserved": _row(
        "grant issued, or the reservation released when its snapshot release lapses",
        "campaign_runner.RELEASE_MAX_SECONDS",
        "test_campaign_retry.py::test_t1_expired_unleased_grant_is_retried_once_with_a_fresh_grant",
    ),
    "granted": _row(
        "an unleased grant past its expiry is released and retried (t1_grant_expired_unleased)",
        "campaign_runner.RELEASE_MAX_SECONDS",
        "test_campaign_retry.py::test_t1_expired_unleased_grant_is_retried_once_with_a_fresh_grant",
    ),
    "admitted": _row(
        "a refused launch reconciles as not started (ADR 0046); a silent route goes unknown",
        "campaign_runner.MAX_ATTEMPT_NUMBERS",
        "test_campaign_autoreconcile.py::test_a_refused_launch_is_reconciled_without_a_human",
    ),
    "running": _row(
        "the silent-executor clock: unknown past the route window plus grace",
        "campaign_salad.SILENT_EXECUTOR_GRACE_SECONDS",
        "test_attempt_state_totality.py::test_executing_past_its_window_leaves_by_the_clock",
    ),
    "exited": _row(
        "verified each pass; a confirmed exit resolves (ADR 0053)",
        "campaign_runner.MAX_STEPS_PER_TICK",
        "test_blocked_campaign_leased_reconcile.py::test_a_leased_grant_with_a_confirmed_exit_frees_the_slot",
    ),
    "collected": _row(
        "the settle step; a charge wait is bounded by the charge deadline",
        "salad_guardian_bound.CHARGE_DEADLINE_SECONDS",
        "test_attempt_state_totality.py::test_awaiting_charge_past_its_deadline_settles",
    ),
    "resolved": _row(
        "the settle step; a charge wait is bounded by the charge deadline",
        "salad_guardian_bound.CHARGE_DEADLINE_SECONDS",
        "test_attempt_state_totality.py::test_awaiting_charge_past_its_deadline_settles",
    ),
    "measured": _row(
        "the settle step; a charge wait is bounded by the charge deadline",
        "salad_guardian_bound.CHARGE_DEADLINE_SECONDS",
        "test_attempt_state_totality.py::test_awaiting_charge_past_its_deadline_settles",
    ),
    "reconciled": _row(
        "concluded on the next pass; a reconciled grant's charge wait has the same deadline",
        "salad_guardian_bound.CHARGE_DEADLINE_SECONDS",
        "test_salad_limbo_exits.py::test_a_retryable_reconcile_settles_at_the_hold_and_retries_the_stage",
    ),
    "unknown": _row(
        "re-read every blocked pass (late import, ADR 0212 close, charge overdue), not-started "
        "and unclaimed auto-reconciles, a cancel at the hold; else the human list",
        "campaign_salad.GUARDIAN_FINAL_REFRESH_SECONDS",
        "test_attempt_state_totality.py::test_a_charge_overdue_unknown_leaves_on_the_blocked_pass",
    ),
}

SALAD_PHASES = {
    "awaiting_lease": _row(
        "an unleased grant past its expiry is released and retried",
        "campaign_runner.RELEASE_MAX_SECONDS",
        "test_campaign_retry.py::test_t1_expired_unleased_grant_is_retried_once_with_a_fresh_grant",
    ),
    "leased": _row(
        "reviews proposed; unresolved reviews expire into the unclaimed path",
        "campaign_salad.DECISION_HOURS",
        "test_campaign_salad_runner.py::test_unresolved_reviews_expire_into_the_unclaimed_path_and_retry",
    ),
    "awaiting_review": _row(
        "unresolved reviews expire into the unclaimed path and retry",
        "campaign_salad.DECISION_HOURS",
        "test_campaign_salad_runner.py::test_unresolved_reviews_expire_into_the_unclaimed_path_and_retry",
    ),
    "reviewed": _row(
        "capability requested; an unanswered capability expires into the unclaimed path",
        "campaign_salad.CAPABILITY_HOURS",
        "test_campaign_salad_runner.py::test_unresolved_reviews_expire_into_the_unclaimed_path_and_retry",
    ),
    "capability_ready": _row(
        "route proposed in the same pass; the capability's own expiry bounds it",
        "campaign_salad.CAPABILITY_HOURS",
        "test_campaign_salad_runner.py::test_unresolved_reviews_expire_into_the_unclaimed_path_and_retry",
    ),
    "route_proposed": _row(
        "route approved and started; the capability's own expiry bounds it",
        "campaign_salad.CAPABILITY_HOURS",
        "test_campaign_salad_runner.py::test_unresolved_reviews_expire_into_the_unclaimed_path_and_retry",
    ),
    "route_started": _row(
        "a never-sent route past the window is released and requeued; else silent unknown",
        "campaign_salad.SILENT_EXECUTOR_GRACE_SECONDS",
        "test_salad_silent_never_sent_released.py::test_queued_never_sent_route_past_the_window_is_released_and_requeued",
    ),
    "executing": _row(
        "the silent-executor clock: unknown past the route window plus grace",
        "campaign_salad.SILENT_EXECUTOR_GRACE_SECONDS",
        "test_attempt_state_totality.py::test_executing_past_its_window_leaves_by_the_clock",
    ),
    "outcome_recorded": _row(
        "the unchanged verify step, every pass (also while blocked)",
        "campaign_runner.MAX_STEPS_PER_TICK",
        "test_salad_reconciled_no_output.py::test_a_reconciled_output_less_attempt_retries_and_settles_at_the_estimate",
    ),
    "awaiting_charge": _row(
        "past the charge deadline: settled at the guardian's bound, or one block (ADR 0216)",
        "salad_guardian_bound.CHARGE_DEADLINE_SECONDS",
        "test_attempt_state_totality.py::test_awaiting_charge_past_its_deadline_settles",
    ),
    "unknown": _row(
        "re-read every blocked pass by waiting reason (silent, route unconfirmed, charge "
        "overdue); else the human list",
        "campaign_salad.GUARDIAN_FINAL_REFRESH_SECONDS",
        "test_attempt_state_totality.py::test_a_charge_overdue_unknown_leaves_on_the_blocked_pass",
    ),
}


def _schema_phases() -> set[str]:
    source = Path(campaign_schema.__file__).read_text()
    found = re.search(r"phase TEXT NOT NULL CHECK\(phase IN \(([^)]*)\)\)", source)
    assert found is not None
    return set(re.findall(r"'([a-z_]+)'", found.group(1)))


# -- the table is total ---------------------------------------------------------------------


def test_every_non_final_attempt_status_has_a_row():
    statuses = set(campaign_models.ATTEMPT_STATUSES)
    assert FINAL_STATUSES <= statuses
    assert set(ATTEMPT_STATUSES) == statuses - FINAL_STATUSES


def test_every_non_final_salad_phase_has_a_row():
    phases = _schema_phases()
    assert FINAL_PHASES <= phases
    assert set(SALAD_PHASES) == phases - FINAL_PHASES


@pytest.mark.parametrize(
    "state,row",
    [(f"status:{k}", v) for k, v in ATTEMPT_STATUSES.items()]
    + [(f"phase:{k}", v) for k, v in SALAD_PHASES.items()],
)
def test_each_row_names_a_real_bound_and_a_real_test(state, row):
    assert row["exit"].strip()
    module, attribute = row["bound"].split(".")
    value = getattr(MODULES[module], attribute)
    assert isinstance(value, int | float) and value > 0, state
    path, name = row["test"].split("::")
    tree = ast.parse((TESTS / path).read_text())
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert name in names, f"{state}: {row['test']} does not exist"


# -- the clock rows, driven past their bound --------------------------------------------------


FENCED = dict(
    status="stopped",
    reason="rate_unknown",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=True,
    rate_unknown=True,
    published_hourly_rate_usd=None,
    estimated_exposure_cents=None,
    actual_billed_cost_cents=None,
    observed_at=datetime.now(UTC).isoformat(),
)


def _later(world: SaladWorld, seconds: float) -> None:
    world.runner.now = lambda: datetime.now(UTC) + timedelta(seconds=seconds)


def test_awaiting_charge_past_its_deadline_settles(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.run_to_outcome(guardian_final=FENCED)
    world.tick_until_phase("awaiting_charge")
    _later(world, CHARGE_DEADLINE_SECONDS + 1)
    world.tick_until(lambda: world.phase() == "settled")
    attempt = world.attempt()
    assert (attempt["settled_cents"], attempt["settlement_basis"]) == (
        HOLD,
        "guardian_bounded_hold",
    )


def test_a_charge_overdue_unknown_leaves_on_the_blocked_pass(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    _later(world, CHARGE_DEADLINE_SECONDS + 1)
    world.tick_until(lambda: world.phase() == "unknown")
    assert world.salad()["waiting_reason"] == CHARGE_OVERDUE and world.status() == "blocked"
    world.record_charge(9)
    world.tick_until(lambda: world.phase() == "settled")
    attempt = world.attempt()
    assert (attempt["settled_cents"], attempt["settlement_basis"]) == (9, "human_charge")


def test_executing_past_its_window_leaves_by_the_clock(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    window = world.route_state()["proposal"]["selection"]["resources"]["max_seconds"]
    start = datetime.fromisoformat(world.salad()["updated_at"])
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    world.runner.now = lambda: start + timedelta(seconds=window + SILENT_EXECUTOR_GRACE_SECONDS + 1)
    world.tick(3)
    # Out of ``executing`` by the clock alone, into a state whose own exits are listed above.
    assert world.salad()["phase"] == "unknown"
    assert world.attempt()["status"] == "unknown"
