"""A stage's status follows its attempts (ADR 0218).

* The runner repairs a stage left ``blocked_unknown`` once none of its attempts is live or
  unknown (no stage is non-final while every attempt is terminal), for a cancel-requested
  campaign too.
* A recorded non-zero container exit stays a permanent workload failure after a provider
  allocation change (classification is unchanged).

Fakes only (the Salad campaign world and its doubles; no network, no credentials).
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_salad_nonzero_exit import _events, _observe
from test_salad_reconciled_no_output import (
    FINAL,
    _attempts,
    _first,
    _ran_uncertain,
    _world,
)

from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_RECONCILED_NO_OUTPUT,
    is_infra_class,
)
from tessera_control.campaign_runner import TERMINAL_ATTEMPT
from tessera_control.salad_exit_watch import FAILURE_CLASS
from tessera_control.salad_guardian import RECONCILE_OPERATOR

# The s17 shape: a recorded exit 1 on a frame the guardian closed after the provider's move.
RECONCILED = dict(FINAL, start_events=_events([1]))


def _stage_status(world: SaladWorld) -> str:
    return world.row("SELECT status FROM campaign_stages WHERE campaign_id=?", world.campaign_id)[
        "status"
    ]


# -- classification is unchanged --


@pytest.mark.parametrize(
    "extra", [{}, {"provider_stop_cause": "allocation_changed"}], ids=["plain", "stop_cause_key"]
)
def test_a_recorded_exit_stays_permanent_whatever_else_the_record_says(extra):
    observation = _observe(
        {"start_events": _events([1]), **extra},
        failure_class=INFRA_SALAD_RECONCILED_NO_OUTPUT,
        failure_detail=f"{INFRA_SALAD_RECONCILED_NO_OUTPUT}: provider_reconciled_no_output",
    )
    assert observation.outcome == "failed_permanent"
    assert observation.failure_class == FAILURE_CLASS
    assert not is_infra_class(observation.failure_class)
    assert "code 1" in observation.detail


def test_a_recorded_exit_fails_the_stage_without_a_retry_and_the_campaign_finishes(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world, RECONCILED)
    world.tick_until(lambda: world.status() == "failed")
    first = _first(world)
    assert (first["status"], first["failure_class"]) == ("failed_permanent", FAILURE_CLASS)
    assert len(_attempts(world)) == 1  # no retry of a workload failure
    assert _stage_status(world) == "failed"


# -- the runner repairs a stranded stage --


def _strand(world: SaladWorld) -> None:
    """The stuck row of the field: the attempt ended and settled, the stage still blocked."""
    with world.env.store.runner_connection() as connection:
        connection.execute(
            "UPDATE campaign_stages SET status='blocked_unknown' WHERE campaign_id=?",
            (world.campaign_id,),
        )


def test_the_held_stage_of_a_terminal_attempt_becomes_failed_and_the_campaign_ends(tmp_path):
    """ADR 0169 held the stage ``blocked_unknown`` on a terminal ``failed_permanent`` attempt,
    and nothing ever advanced it again: the field state. It now follows its attempt."""
    from test_salad_profile_fallback import (
        _ended_and_charged,
        _first_attempt_started,
        _ran_unstable,
    )
    from test_salad_profile_fallback import _world as _alternates_world

    world = _alternates_world(tmp_path)
    _first_attempt_started(world)
    _ran_unstable(world, within=False)
    _ended_and_charged(world)
    [stop] = world.events("campaign.salad_profile_fallback_stopped")
    assert stop["outcome"] == "post_start_switch_refused"
    world.tick_until(lambda: _stage_status(world) != "blocked_unknown")
    assert _stage_status(world) == "failed"
    [event] = world.events("campaign.stage_status_repaired")
    assert event["from_status"] == "blocked_unknown" and event["to_status"] == "failed"
    assert event["attempt"] == 1 and event["attempt_status"] == "failed_permanent"
    world.tick(4)
    assert len(world.events("campaign.stage_status_repaired")) == 1
    assert len(world.events("campaign.salad_profile_fallback_stopped")) == 1
    assert world.events("campaign.salad_profile_switched") == []
    assert world.attempt()["attempt"] == 1  # no new attempt, no profile move
    assert world.status() == "failed"
    reason = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert reason["stop_reason"].startswith("stage_failed:g1:")


def test_a_stage_with_a_live_attempt_is_never_repaired(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world)
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    assert _attempts(world)[1]["status"] in {"reserved", "granted", "admitted", "running"}
    _strand(world)
    world.tick(2)
    assert world.events("campaign.stage_status_repaired") == []


def test_a_stage_with_an_unknown_attempt_is_never_repaired(tmp_path):
    world = _world(tmp_path)
    final = dict(FINAL, reason=RECONCILE_OPERATOR)  # fails the ADR 0212 rule: stays unknown
    _ran_uncertain(world, final)
    world.tick_until(lambda: world.status() == "blocked")
    world.tick(3)
    assert _first(world)["status"] == "unknown"
    assert _stage_status(world) == "blocked_unknown"
    assert world.events("campaign.stage_status_repaired") == []
    assert world.status() == "blocked"


def test_a_cancel_requested_campaign_with_a_stranded_stage_ends_cancelled(tmp_path):
    """``_drive_cancel`` waits on a ``blocked_unknown`` stage: the repair runs for it too."""
    world = _world(tmp_path, infra=1)
    _ran_uncertain(world)
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    _end_latest(world, "failed_permanent", settled=4)
    _strand(world)
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert _stage_status(world) in {"failed", "cancelled", "skipped"}
    assert world.events("campaign.stage_status_repaired")


# -- the target of every terminal attempt status --

# attempt status -> (settled cents, stage status the repair gives)
TARGETS = {
    "succeeded": (1, "preparing"),
    "failed_transient": (1, "preparing"),
    "failed_permanent": (1, "failed"),
    "refused_budget": (None, "refused_budget"),
    "cancelled": (None, "skipped"),
}


def _end_latest(world: SaladWorld, status: str, *, settled) -> None:
    with world.env.store.runner_connection() as connection:
        connection.execute(
            "UPDATE campaign_stage_attempts SET status=?, settled_cents=? "
            "WHERE campaign_id=? AND attempt=(SELECT MAX(attempt) FROM campaign_stage_attempts "
            "WHERE campaign_id=?)",
            (status, settled, world.campaign_id, world.campaign_id),
        )


def test_the_table_covers_every_terminal_attempt_status_but_unknown():
    assert set(TARGETS) == set(TERMINAL_ATTEMPT) - {"unknown"}


@pytest.mark.parametrize("status", sorted(TARGETS))
def test_a_stranded_stage_follows_its_latest_attempt_status(tmp_path, status):
    settled, expected = TARGETS[status]
    world = _world(tmp_path, infra=1)
    _ran_uncertain(world)
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    _end_latest(world, status, settled=settled)
    _strand(world)
    world.tick_until(lambda: bool(world.events("campaign.stage_status_repaired")))
    [event] = world.events("campaign.stage_status_repaired")
    assert (event["attempt_status"], event["to_status"]) == (status, expected)
    assert _stage_status(world) != "blocked_unknown"
    if expected in {"failed", "refused_budget", "skipped"}:
        world.tick(3)  # a final stage is never moved to ``preparing`` again
        assert _stage_status(world) == expected
