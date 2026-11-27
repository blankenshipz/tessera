"""An approved review survives the lapsed-lease requeue: no second review stop.

* The field case: the review outlasted the lease's hard expiry, a person approved it, the
  attempt was requeued before its route; the requeued attempt's identical reviews are resolved
  on that approval, it reaches ``route_started`` without a person, and the receipt shows it.
* Never more than was reviewed: a different review core, another reviewer, an expired approval
  or a prior attempt that was not a lapse requeue all leave the review pending for a person,
  and control re-checks the carryover itself inside the resolving transaction.
* A pre-route refusal is classified by the refusal itself (finding 2), and the worker is read
  the way ``require_ready_worker`` reads it (finding 3).

Real stores and runner (``SaladWorld``) on synthetic state; no provider, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control import campaign_review_carryover as carryover
from tessera_control.campaign_autoreconcile import NOT_STARTED
from tessera_control.campaign_delegation import _check_resources
from tessera_control.campaign_salad_lease_lapse import (
    LEASE_LAPSED_BEFORE_ROUTE,
    WORKER_UNAVAILABLE_BEFORE_ROUTE,
    worker_problem,
)
from tessera_control.campaign_steps import StepFailed
from tessera_control.store import Forbidden

REVIEW_STOP = "campaign.stage_awaiting_human"


def _past(minutes: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _lapse_lease(world: SaladWorld) -> None:
    world.env.execute(
        "UPDATE execution_leases SET expires_at=?, hard_expires_at=? WHERE id=?",
        _past(),
        _past(),
        world.salad()["lease_id"],
    )


def _attempt(world: SaladWorld, number: int):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=?",
        world.campaign_id,
        number,
    )


def _review_stops(world: SaladWorld, attempt: int) -> list:
    return [e for e in world.events(REVIEW_STOP) if e.get("attempt") == attempt]


def _requeued_after_approval(world: SaladWorld) -> None:
    """Attempt 1: packaged, its review outlasted the lease, a person approved it."""
    world.to_awaiting_review()
    _lapse_lease(world)
    world.resolve_decisions()
    world.tick_until(lambda: _attempt(world, 1)["status"] == "failed_transient")
    assert _attempt(world, 1)["failure_class"] == NOT_STARTED


def _to_review_of_attempt_two(world: SaladWorld) -> None:
    world.tick_until(
        lambda: (s := world.salad(attempt=2)) is not None and s["phase"] == "awaiting_lease"
    )
    world.lease_and_package()
    world.tick_until(
        lambda: (
            world.salad(attempt=2)["phase"] in {"awaiting_review", "reviewed"}
            or world.salad(attempt=2)["source_decision_id"] is not None
        )
    )


def _decision(world: SaladWorld, decision_id: str):
    return world.row("SELECT * FROM decision_requests WHERE id=?", decision_id)


def test_requeued_attempt_carries_the_approved_review_and_starts_without_a_person(tmp_path):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    assert len(_review_stops(world, 1)) == 1

    _to_review_of_attempt_two(world)
    world.tick_until(lambda: world.salad(attempt=2)["phase"] == "route_started", limit=30)

    second = world.salad(attempt=2)
    first = world.salad(attempt=1)
    for column in ("plan_decision_id", "source_decision_id"):
        assert second[column] != first[column]
        decision = _decision(world, second[column])
        assert decision["status"] == "approved"
        assert "Carried over from approved" in decision["rationale"]
    # No second review stop for the requeued attempt.
    assert _review_stops(world, 2) == []
    events = world.events(carryover.CARRYOVER_EVENT)
    assert sorted(e["kind"] for e in events) == ["plan", "source_use"]
    assert {e["carried_from_decision_id"] for e in events} == {
        first["plan_decision_id"],
        first["source_decision_id"],
    }
    assert all(e["attempt"] == 2 and e["carried_from_attempt"] == 1 for e in events)
    # The receipt records every carryover.
    from tessera_control.campaign_receipts import build_receipt  # noqa: PLC0415

    with world.runner.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _digest = build_receipt(connection, campaign, campaign["status"])
    assert [(c["kind"], c["attempt"]) for c in receipt["review_carryovers"]] in (
        [("plan", 2), ("source_use", 2)],
        [("source_use", 2), ("plan", 2)],
    )


def test_a_different_review_core_is_never_carried(tmp_path):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    world.tick_until(
        lambda: (s := world.salad(attempt=2)) is not None and s["phase"] == "awaiting_lease"
    )
    world.lease_and_package()
    world.tick_until(lambda: world.salad(attempt=2)["plan_decision_id"] is not None)
    plan_id = world.salad(attempt=2)["plan_decision_id"]
    # The new plan asks for something the person did not review.
    row = _decision(world, plan_id)
    scope = json.loads(row["scope_json"])
    scope["reason"] = "a different plan than the one approved"
    world.env.execute(
        "UPDATE decision_requests SET scope_json=? WHERE id=?", json.dumps(scope), plan_id
    )

    world.tick(6)

    assert _decision(world, plan_id)["status"] == "pending"
    declined = world.events(carryover.DECLINED_EVENT)
    assert [(e["kind"], e["reason"]) for e in declined] == [("plan", carryover.DIGEST_MISMATCH)]
    assert world.salad(attempt=2)["phase"] == "awaiting_review"
    # The person is asked again for this attempt.
    assert len(_review_stops(world, 2)) == 1


def test_an_expired_prior_approval_is_not_carried(tmp_path):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    first = world.salad(attempt=1)
    world.env.execute(
        "UPDATE decision_requests SET expires_at=? WHERE id IN (?,?)",
        _past(),
        first["plan_decision_id"],
        first["source_decision_id"],
    )

    _to_review_of_attempt_two(world)
    world.tick(4)

    second = world.salad(attempt=2)
    assert _decision(world, second["plan_decision_id"])["status"] == "pending"
    assert world.events(carryover.CARRYOVER_EVENT) == []
    assert {e["reason"] for e in world.events(carryover.DECLINED_EVENT)} == {carryover.EXPIRED}


def test_a_first_attempt_has_nothing_to_carry(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    row = world.salad()
    with world.runner.store.runner_connection() as connection:
        verdict = carryover.evaluate(
            connection,
            campaign_id=world.campaign_id,
            stage_key="g1",
            kind="plan",
            subject_id=row["plan_decision_id"],
        )
    assert verdict.reason == carryover.NO_PRIOR
    assert world.events(carryover.DECLINED_EVENT) == []
    assert len(_review_stops(world, 1)) == 1


def test_control_rechecks_the_carryover_and_refuses_a_forged_basis(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    # The runner is made to believe a non-lapse prior covers it; control re-checks and refuses.
    real = carryover.evaluate

    def forged(connection, **kwargs):
        verdict = real(connection, **kwargs)
        return carryover.Carryover(None, "decision_forged", 1, 2, "0" * 64) if verdict else verdict

    monkeypatch.setattr("tessera_control.campaign_salad.review_carryover.evaluate", forged)
    _to_review_of_attempt_two(world)
    world.tick(4)

    second = world.salad(attempt=2)
    assert _decision(world, second["plan_decision_id"])["status"] == "pending"
    assert world.events(carryover.CARRYOVER_EVENT) == []
    reasons = {e["reason"] for e in world.events(carryover.DECLINED_EVENT)}
    assert reasons and all(reason.startswith("step_") for reason in reasons)


@pytest.mark.parametrize(
    "resources",
    [
        {"decision_id": "d", "preapproval_id": "p", "carryover_decision_id": "c"},
        {"decision_id": "d"},
    ],
    ids=["both_bases", "no_basis"],
)
def test_a_review_resolution_needs_exactly_one_basis(resources):
    with pytest.raises(Forbidden):
        _check_resources("resolve_decision", {"decision_id": "d"}, resources)


def test_one_carryover_basis_is_accepted_by_the_intent_check():
    _check_resources(
        "resolve_decision", {"decision_id": "d"}, {"decision_id": "d", "carryover_decision_id": "c"}
    )


# -- finding 2: the refusal itself decides --------------------------------------------------


def test_a_local_refusal_is_permanent_even_when_the_worker_looks_lapsed(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until_phase("route_proposed")
    # The store would read the worker as lapsed, but the refusal is the runner's own.
    world.env.execute("UPDATE worker_registry SET last_seen_at=?", _past(80))

    def refused(self, *args, **kwargs):
        raise StepFailed("salad", "step_refused", "salad_image_not_allowlisted")

    monkeypatch.setattr(type(world.driver), "_approve_and_start", refused)

    world.tick_until(lambda: _attempt(world, 1)["status"] == "failed_permanent")

    assert _attempt(world, 1)["failure_class"] == "step_refused"


def test_a_control_conflict_with_a_lapsed_lease_still_requeues(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until_phase("route_proposed")
    _lapse_lease(world)

    def refused(self, *args, **kwargs):
        raise StepFailed("approve_route", "Conflict", "Execution lease is no longer current.")

    monkeypatch.setattr(type(world.driver), "_approve_and_start", refused)
    # The before-handler lease check is skipped so only the refusal path decides; an unwind
    # it started still finishes through the real before-handler on later passes.
    from tessera_control import campaign_salad_lease_lapse as lease_lapse

    real = lease_lapse.before_handler

    def only_finishing(driver, ctx, attempt, row):
        if lease_lapse.recorded_reason(driver, ctx, attempt) is None:
            return None
        return real(driver, ctx, attempt, row)

    monkeypatch.setattr("tessera_control.campaign_salad.lease_lapse.before_handler", only_finishing)

    world.tick_until(lambda: _attempt(world, 1)["status"] == "failed_transient")

    assert f"({LEASE_LAPSED_BEFORE_ROUTE})" in _attempt(world, 1)["failure_detail"]


# -- finding 3: the worker as require_ready_worker reads it ---------------------------------


def _grant(world: SaladWorld):
    return dict(world.row("SELECT * FROM execution_grants WHERE id=?", world.attempt()["grant_id"]))


def test_a_missing_registry_row_is_not_a_transient_lapse(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.env.execute("DELETE FROM worker_registry")

    assert worker_problem(world.driver, _grant(world), datetime.now(UTC)) is None


def test_a_stale_row_of_the_workers_own_credential_is_a_lapse(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.env.execute("UPDATE worker_registry SET last_seen_at=?", _past(80))

    assert (
        worker_problem(world.driver, _grant(world), datetime.now(UTC))
        == WORKER_UNAVAILABLE_BEFORE_ROUTE
    )


def test_a_registry_row_of_another_credential_is_not_read(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.env.execute("UPDATE worker_registry SET last_seen_at=?", _past(80))
    # The row no longer belongs to the worker's own credential (as after a rotation).
    import sqlite3

    connection = sqlite3.connect(world.env.state / "tessera.sqlite3")
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE worker_registry SET credential_id='credential_other'")
        connection.commit()
    finally:
        connection.close()

    assert worker_problem(world.driver, _grant(world), datetime.now(UTC)) is None


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def test_an_approval_given_before_a_grant_bound_deadline_still_carries(tmp_path):
    # The lease lapsed exactly when the pending decision's deadline (the grant) passed: the
    # person had answered in time, so the approval is still good inside its 23 h window.
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    first = world.salad(attempt=1)
    for column in ("plan_decision_id", "source_decision_id"):
        row = _decision(world, first[column])
        assert datetime.fromisoformat(row["resolved_at"]) < datetime.now(UTC)
        world.env.execute(
            "UPDATE decision_requests SET expires_at=? WHERE id=?",
            _iso(timedelta(seconds=-1)),
            first[column],
        )
        world.env.execute(
            "UPDATE decision_requests SET resolved_at=? WHERE id=?",
            _iso(timedelta(seconds=-30)),
            first[column],
        )

    _to_review_of_attempt_two(world)
    world.tick_until(lambda: world.salad(attempt=2)["phase"] == "route_started", limit=30)
    assert _review_stops(world, 2) == []
    assert {e["root_decision_id"] for e in world.events(carryover.CARRYOVER_EVENT)} == {
        first["plan_decision_id"],
        first["source_decision_id"],
    }


def test_an_approval_older_than_its_window_is_not_carried(tmp_path):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    first = world.salad(attempt=1)
    world.env.execute(
        "UPDATE decision_requests SET created_at=? WHERE id IN (?,?)",
        _iso(timedelta(hours=-(carryover.APPROVAL_HOURS + 1))),
        first["plan_decision_id"],
        first["source_decision_id"],
    )
    _to_review_of_attempt_two(world)
    world.tick(4)
    assert world.events(carryover.CARRYOVER_EVENT) == []
    assert {e["reason"] for e in world.events(carryover.DECLINED_EVENT)} == {carryover.EXPIRED}


def test_a_chained_carryover_traces_to_the_root_and_never_extends_its_window(tmp_path):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    first = world.salad(attempt=1)
    _to_review_of_attempt_two(world)
    world.tick_until(lambda: world.salad(attempt=2)["phase"] == "route_started", limit=30)
    second = world.salad(attempt=2)
    now = datetime.now(UTC)
    with world.runner.store.runner_connection() as connection:
        # Attempt 2's decision was resolved by the runner (carried); its root is attempt 1's.
        root, problem = carryover._root_approval(connection, second["plan_decision_id"], now)
        assert problem is None and root["id"] == first["plan_decision_id"]
        assert root["resolved_by_principal_id"] == world.human["id"]
        # The root's window governs even though attempt 2's own decision is fresh.
        late = now + timedelta(hours=carryover.APPROVAL_HOURS, minutes=1)
        assert carryover._root_approval(connection, second["plan_decision_id"], late) == (
            None,
            carryover.EXPIRED,
        )
        # A broken chain (event names a decision that is gone) fails closed.
        assert carryover._root_approval(connection, "missing", now) == (
            None,
            carryover.PRIOR_NOT_APPROVED,
        )
    events = [e for e in world.events(carryover.CARRYOVER_EVENT) if e["kind"] == "plan"]
    assert events[0]["root_decision_id"] == first["plan_decision_id"]


def test_the_root_resolver_must_be_the_reviewer(tmp_path):
    world = SaladWorld(tmp_path)
    _requeued_after_approval(world)
    first = world.salad(attempt=1)
    other = world.row("SELECT id FROM principals WHERE id<>? LIMIT 1", world.human["id"])
    assert other is not None
    world.env.execute(
        "UPDATE decision_requests SET resolved_by_principal_id=? WHERE id IN (?,?)",
        other["id"],
        first["plan_decision_id"],
        first["source_decision_id"],
    )
    _to_review_of_attempt_two(world)
    world.tick(4)
    assert world.events(carryover.CARRYOVER_EVENT) == []
    assert {e["reason"] for e in world.events(carryover.DECLINED_EVENT)} == {
        carryover.OTHER_RESOLVER
    }
