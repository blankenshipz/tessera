"""ADR 0216, review round 2.

F1: a charge-overdue attempt resumed by the guardian's bound settles; it never ping-pongs
between blocked and resumed (a throttled guardian read is never "no bound").
F2: the human retryable route needs control's own read of the guardian record; an
executor-recorded final (or an operator's attestation) never replaces it.
F3: a ``stopped`` final with an uncertain allocation and no provider-confirmed close is not a
bound (ADR 0212's human case).

Fakes only (the Salad campaign world, its guardian reader and executor doubles; no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import HOLD, salad_provider_reset  # noqa: F401
from test_salad_limbo_exits import (
    FENCED,
    STOPPED,
    _attempts,
    _awaiting_charge,
    _first,
    _held_unknown,
    _reconcile_retryable,
)

from tessera_control.campaign_infra_retry import RETRYABLE_RECONCILE_EVENT
from tessera_control.salad_guardian import RECONCILE_PROVIDER
from tessera_control.salad_guardian_bound import (
    CHARGE_DEADLINE_SECONDS,
    CHARGE_OVERDUE_RESUMED_EVENT,
    guardian_bound_refusal,
)
from tessera_control.store import Conflict

FINALS = {
    "fenced": FENCED,
    "rate_unknown": dict(FENCED, budget_fenced=False),
    "no_estimate": dict(
        FENCED,
        reason="cost_cap",
        budget_fenced=False,
        rate_unknown=False,
        published_hourly_rate_usd="0.5",
        estimated_exposure_cents=None,
    ),
    "provider_closed": dict(
        FENCED,
        status="reconciled",
        reason=RECONCILE_PROVIDER,
        allocation_uncertain=True,
        budget_fenced=False,
    ),
}


# -- F1 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FINALS))
def test_a_bound_resume_settles_and_never_ping_pongs(tmp_path, name):
    world = _awaiting_charge(tmp_path, FINALS[name])
    clock = {"t": datetime.now(UTC) + timedelta(seconds=CHARGE_DEADLINE_SECONDS + 60)}
    world.runner.now = lambda: clock["t"]
    # At the deadline control cannot read the guardian: the campaign blocks once.
    world.guardian.unreachable = True
    for _ in range(20):
        world.runner.tick()
        clock["t"] += timedelta(seconds=10)
        if world.status() == "blocked":
            break
    assert world.status() == "blocked"
    world.guardian.unreachable = False
    statuses = []
    for _ in range(60):  # ten simulated minutes, one pass every 10 s
        world.runner.tick()
        clock["t"] += timedelta(seconds=10)
        statuses.append(world.status())
    first = _first(world)
    assert (first["settled_cents"], first["settlement_basis"]) == (
        HOLD,
        "guardian_bounded_hold",
    ), statuses
    assert world.status() == "succeeded"
    assert len(world.events("campaign.blocked")) == 1, statuses
    (resumed,) = world.events(CHARGE_OVERDUE_RESUMED_EVENT)
    assert resumed["basis"] == "guardian_bounded_hold" and resumed["settlement_cents"] == HOLD


# -- F2 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("guardian", ["open", "unreachable"])
def test_the_retryable_route_needs_controls_own_guardian_read(tmp_path, guardian):
    world = _held_unknown(tmp_path, STOPPED)  # the executor's final says stopped
    if guardian == "open":
        world.guardian.verdict = "open"
    else:
        world.guardian.unreachable = True
    grant_before = world.row("SELECT * FROM execution_grants WHERE id=?", _first(world)["grant_id"])
    with pytest.raises(Conflict):
        _reconcile_retryable(world)
    assert world.events(RETRYABLE_RECONCILE_EVENT) == []
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", grant_before["id"])
    assert (grant["status"], grant["revision"]) == (
        grant_before["status"],
        grant_before["revision"],
    )
    world.tick(3)
    # No retry, so no second hold.
    assert len(_attempts(world)) == 1 and _first(world)["status"] == "unknown"
    assert world.status() == "blocked"


def test_a_closed_guardian_read_is_recorded_on_the_route_event(tmp_path):
    world = _held_unknown(tmp_path, STOPPED)
    assert _reconcile_retryable(world)[0] == 200
    (event,) = world.events(RETRYABLE_RECONCILE_EVENT)
    assert event["guardian_read"]["verdict"] == "stopped"
    assert len(event["guardian_read"]["record_digest"]) == 64


# -- F3 -----------------------------------------------------------------------------------------


def test_a_stopped_final_with_an_uncertain_allocation_is_not_a_bound():
    import json

    row = {"guardian_final_json": json.dumps(dict(STOPPED, allocation_uncertain=True))}
    attempt = {"hold_cents": HOLD}
    assert guardian_bound_refusal(attempt, row) == "allocation_uncertain"
    assert guardian_bound_refusal(attempt, row, attested=True) == "allocation_uncertain"
    closed = dict(FINALS["provider_closed"])
    assert guardian_bound_refusal(attempt, {"guardian_final_json": json.dumps(closed)}) is None


def test_the_retryable_route_refuses_a_stopped_uncertain_final(tmp_path):
    world = _held_unknown(tmp_path, dict(STOPPED, allocation_uncertain=True))
    with pytest.raises(Conflict, match="uncertain"):
        _reconcile_retryable(world)


def test_past_the_deadline_a_stopped_uncertain_final_blocks_instead_of_settling(tmp_path):
    world = _awaiting_charge(tmp_path, dict(FENCED, allocation_uncertain=True))
    world.runner.now = lambda: datetime.now(UTC) + timedelta(seconds=CHARGE_DEADLINE_SECONDS + 60)
    world.tick_until(lambda: world.status() == "blocked")
    assert _first(world)["settled_cents"] is None
    (blocked,) = world.events("campaign.blocked")
    assert blocked["refusal"] == "allocation_uncertain"
