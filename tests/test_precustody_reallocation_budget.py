"""The stage task budget covers the pre-custody reallocation retries (ADR 0179).

An interrupted attempt is billed, so the retry's grant must fit under the task budget on top of
the settled charge. Fakes only; the billing is simulated by settling every reconciled grant at
its full hold, as a billable provider would."""

from __future__ import annotations

# ruff: noqa: F811
import pytest
from campaign_runner_harness import World, dump, stage_spec
from test_campaign_provider_salad import (  # noqa: F401 - fixtures
    connection,
    first_stage,
    provider,
    salad_provider_reset,
    world,
)
from test_precustody_reallocation_retry import KILLED, METRIC, OK, reallocate  # noqa: F401

from tessera_control.campaign_infra_retry import REALLOCATED_PRECUSTODY

HOLD = 6


def _bill_every_attempt(world):
    world.env.execute(
        f"""CREATE TRIGGER bill_interrupted AFTER UPDATE OF status ON execution_grants
        WHEN NEW.status = 'reconciled' AND NEW.settlement_cents = 0
        BEGIN UPDATE execution_grants SET settlement_cents = {HOLD} WHERE id = NEW.id; END"""
    )


def _world(tmp_path, retry=None, cap=100):
    world = World(tmp_path)
    spec = world.env.spec(budget_cap_cents=cap)
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRIC,
            max_exposure_cents=HOLD,
            retry={"max_attempts": 1, **(retry or {})},
        )
    ]
    return world, world.campaign(spec)["id"]


def _budgets(world, cid):
    rows = world.env.query(
        "SELECT budget_cents FROM tasks WHERE purpose LIKE ?", f"Campaign {cid} stage s1 %"
    )
    return {r["budget_cents"] for r in rows}


def test_the_retry_of_a_billed_interrupted_attempt_is_granted_under_default_settings(
    tmp_path,
    reallocate,  # noqa: F811
):
    reallocate["left"] = 1
    world, cid = _world(tmp_path)
    _bill_every_attempt(world)
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [a["failure_class"] for a in world.attempts(cid)] == [REALLOCATED_PRECUSTODY, None]
    settled = world.row(
        "SELECT sum(settlement_cents) AS s FROM execution_grants WHERE status='reconciled'"
    )["s"]
    assert settled >= HOLD  # the interrupted attempt really was billed
    assert _budgets(world, cid) == {HOLD * (1 + 0 + 2)}


def test_every_default_retry_fits_when_each_interrupted_attempt_is_billed(
    tmp_path,
    reallocate,  # noqa: F811
):
    world, cid = _world(tmp_path)
    _bill_every_attempt(world)
    world.scenarios = [(KILLED, OK)] * 5
    world.tick_all(cid)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert len(world.attempts(cid)) == 3, dump(world, cid)
    assert stop == "stage_failed:s1:provider_reallocated_before_custody_exhausted"


def test_a_small_campaign_cap_still_stops_the_retries(tmp_path, reallocate):  # noqa: F811
    # The campaign cap and open-exposure rule are untouched: the widened task budget only lifts
    # the per-task ceiling, never the campaign's.
    world, cid = _world(tmp_path, cap=7)
    world.scenarios = [(KILLED, OK)] * 5
    world.tick_all(cid)
    row = world.row("SELECT budget_cap_cents, held_cents, settled_cents FROM campaigns")
    assert row["held_cents"] + row["settled_cents"] <= row["budget_cap_cents"]
    assert world.status(cid) != "succeeded", dump(world, cid)
    assert len(world.attempts(cid)) <= 3, dump(world, cid)


@pytest.mark.parametrize(("retries", "infra"), [(0, 0), (1, 0), (2, 1), (3, 3)])
def test_the_task_budget_is_exposure_times_every_attempt_bound(tmp_path, retries, infra):
    world, cid = _world(
        tmp_path, retry={"max_reallocation_retries": retries, "infra_retries": infra}
    )
    world.tick_all(cid, limit=20)
    assert _budgets(world, cid) == {HOLD * (1 + infra + retries)}


def test_zero_reallocation_retries_is_the_old_budget(tmp_path):
    world, cid = _world(tmp_path, retry={"max_reallocation_retries": 0})
    world.tick_all(cid)
    assert _budgets(world, cid) == {HOLD * 1}
    assert world.status(cid) == "succeeded", dump(world, cid)


def test_the_budget_is_still_capped_at_the_experiment_ceiling(tmp_path):
    from tessera_control.campaign_models import EXPERIMENT_CEILING_CENTS

    world, cid = _world(tmp_path)
    world.tick_all(cid, limit=20)
    assert max(_budgets(world, cid)) <= EXPERIMENT_CEILING_CENTS


def test_the_salad_per_task_ceiling_counts_the_reallocation_bound(world, provider):  # noqa: F811
    from tessera_control.campaign_models import RetryPolicy
    from tessera_control.store import Conflict

    stage = first_stage(world)
    db = connection(world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        ceiling = 100

        def with_retry(exposure, **retry):
            return stage.model_copy(
                update={
                    "max_exposure_cents": exposure,
                    "retry": RetryPolicy(max_attempts=1, **retry),
                }
            )

        provider.check_stage(db, project, with_retry(ceiling, max_reallocation_retries=0))
        provider.check_stage(db, project, with_retry(ceiling // 3, max_reallocation_retries=2))
        with pytest.raises(Conflict, match="per-task ceiling"):
            provider.check_stage(
                db, project, with_retry(ceiling // 3 + 1, max_reallocation_retries=2)
            )
        with pytest.raises(Conflict, match="per-task ceiling"):
            provider.check_stage(db, project, with_retry(ceiling, max_reallocation_retries=1))
    finally:
        db.close()
