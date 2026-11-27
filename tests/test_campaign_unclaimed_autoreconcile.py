"""Automatic zero reconcile of unknown attempts whose provider claimed no create (ADR 0157).

Real stores and runner; the Salad executor and the human are played by the test. No provider
is ever called: claims, routes and observations are the control-plane records alone."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
)

from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.campaign_providers import CreateClaim, get_provider
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_unclaimed import Unclaimed
from tessera_control.execution import ExecutionStore
from tessera_control.salad_unclaimed import SaladUnclaimedStore
from tessera_control.store import Forbidden, canonical_digest


def world_with(tmp_path, limit: int | None = None) -> SaladWorld:
    world = SaladWorld(tmp_path)
    if limit is not None:
        # Before any tick, so this runner is the one that takes the lease.
        world.runner = CampaignRunner(world.env.state, unclaimed_autoreconcile_limit=limit)
    return world


def block_unknown(world: SaladWorld, detail: str = "leased grant expired without an operation"):
    """The attempt goes unknown and the campaign blocks, exactly as the runner does it."""
    world.tick()  # the runner holds the lease, so its own transitions may commit
    attempt = world.attempt()
    world.driver._unknown(attempt, detail)
    ctx = world.runner._load(world.campaign_id)
    world.runner._stage_unknown(ctx, "g1")
    assert world.status() == "blocked"
    assert world.attempt()["status"] == "unknown"
    return world.attempt()


def held(world: SaladWorld) -> int:
    return world.row("SELECT held_cents FROM execution_project_limits")["held_cents"]


def grant(world: SaladWorld, grant_id: str):
    return world.row("SELECT * FROM execution_grants WHERE id=?", grant_id)


def refusals(world: SaladWorld) -> list[dict]:
    return world.events("campaign.unclaimed_autoreconcile_refused")


def stays_blocked(world: SaladWorld, attempt: dict, reason: str, ticks: int = 3) -> dict:
    world.tick(ticks)
    assert world.status() == "blocked"
    now = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert now["status"] == "unknown" and now["settled_cents"] is None
    assert world.events("campaign.attempt_unclaimed_reconciled") == []
    assert world.events("execution.grant_reconciled_unclaimed") == []
    assert grant(world, attempt["grant_id"])["status"] in {"active", "cancel_requested"}
    found = [event for event in refusals(world) if event["attempt_id"] == attempt["id"]]
    assert [event["reason"] for event in found] == [reason], found
    assert found[0]["readable"].startswith("not auto-reconciled: ")
    return found[0]


# -- the provably unclaimed case --------------------------------------------------------------


def test_a_blocked_attempt_with_no_claimed_create_is_reconciled_and_readmitted(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    before = held(world)
    attempt = block_unknown(world)
    operation_id = world.salad()["operation_id"]

    world.tick()

    settled = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert settled["status"] == "failed_transient"
    assert settled["failure_class"] == "not_started"
    assert settled["settled_cents"] == 0
    reconciled = grant(world, attempt["grant_id"])
    assert reconciled["status"] == "reconciled" and reconciled["settlement_cents"] == 0
    assert held(world) == before - reconciled["max_exposure_cents"]
    assert (
        world.row("SELECT phase FROM campaign_salad_attempts WHERE attempt_id=?", attempt["id"])[
            "phase"
        ]
        == "unclaimed"
    )

    (receipt,) = world.events("campaign.attempt_unclaimed_reconciled")
    assert receipt["attempt_id"] == attempt["id"] and receipt["grant_id"] == attempt["grant_id"]
    assert receipt["reason"] == "no_create_claimed"
    assert receipt["proof"] == "no_create_or_start_claim"
    assert len(receipt["evidence_digest"]) == 64
    assert receipt["automatic"] is True and receipt["is_evidence"] is False
    assert receipt["released_hold_cents"] == reconciled["max_exposure_cents"]
    (accounting,) = world.events("execution.grant_reconciled_unclaimed")
    assert accounting["operation_ids"] == [operation_id] and accounting["automatic"] is True
    assert accounting["settlement_cents"] == 0
    assert accounting["provider_termination_inferred"] is False
    # The platform revoked the grant through its own reduce-only step first.
    assert world.events("execution.grant_cancel_requested")
    assert world.events("campaign.unblocked")
    assert refusals(world) == []

    # The stage re-admits under its own caps: a single-attempt stage gets a second attempt,
    # because a never-started attempt uses an attempt number, not a scientific retry.
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    assert world.status() == "running"


def test_the_same_attempt_unknown_in_a_running_campaign_is_reconciled_before_blocking(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    world.tick()
    attempt = world.attempt()
    world.driver._unknown(attempt, "leased grant expired without an operation")
    world.tick()
    assert world.events("campaign.blocked") == []
    assert (
        world.row("SELECT status FROM campaign_stage_attempts WHERE id=?", attempt["id"])["status"]
        == "failed_transient"
    )
    assert len(world.events("campaign.attempt_unclaimed_reconciled")) == 1


def test_a_replay_never_releases_or_counts_twice(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    attempt = block_unknown(world)
    world.tick()
    after = held(world)
    settled_before = world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)
    ctx = world.runner._load(world.campaign_id)
    # The stale attempt row (still "unknown") is offered again, directly and through a tick.
    assert world.runner._auto_reconcile_unclaimed(ctx, "g1", attempt) is False
    # The in-transaction apply, offered the same attempt again, re-reads and writes nothing.
    verdict = Unclaimed("no_create_or_start_claim", {"attempt_id": attempt["id"]})
    for _ in range(2):
        applied = world.runner._apply_unclaimed(ctx, "g1", attempt, verdict, get_provider("salad"))
        assert applied is not True
    world.tick(2)
    assert held(world) == after
    assert (
        world.row("SELECT settled_cents FROM campaigns WHERE id=?", world.campaign_id)
        == settled_before
    )
    assert len(world.events("campaign.attempt_unclaimed_reconciled")) == 1
    assert len(world.events("execution.grant_reconciled_unclaimed")) == 1
    assert refusals(world) == []


# -- fail closed: each guard leaves the human path and a readable reason ----------------------


def _claim_on_operation(world: SaladWorld, action: str = "create") -> None:
    row = world.salad()
    world._append(
        "salad_proxy.claim",
        dict(operation_id=row["operation_id"], action=action, route_id="route_x", nonce="n" * 8),
    )


@pytest.mark.parametrize("action", ["create", "start"])
def test_a_claimed_create_or_start_stays_blocked(tmp_path, action):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    _claim_on_operation(world, action)
    attempt = block_unknown(world)
    event = stays_blocked(world, attempt, "create_claimed")
    assert event["provider_reason"] == "create_claimed"


def test_a_route_that_still_wants_to_run_is_an_unknown_claim_state(tmp_path):
    # Any unknown other than the silent-executor one keeps the route wanting to run: no
    # automated cancel, the proof refuses. (The silent, never-sent shape gets the reduce-only
    # cancel since v0.104.0: tests/test_salad_silent_never_sent_released.py.)
    world = world_with(tmp_path)
    world.to_route_started()
    attempt = block_unknown(world, "observed unknown, termination confirmed=False")
    event = stays_blocked(world, attempt, "claim_state_unknown")
    assert event["provider_reason"] == "route_wants_run"


def test_a_route_create_effect_stays_blocked(tmp_path):
    world = world_with(tmp_path)
    world.to_route_started()
    state = world.route_state()
    state.update(desired="stop", effects=[{"action": "create", "id": "e1"}])
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])
    attempt = block_unknown(world)
    event = stays_blocked(world, attempt, "create_claimed")
    assert event["provider_reason"] == "route_effect"


def test_a_provider_id_on_the_operation_stays_blocked(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    world.env.execute(
        "UPDATE external_operations SET provider_id='provider-resource-1' WHERE operation_id=?",
        world.salad()["operation_id"],
    )
    attempt = block_unknown(world)
    stays_blocked(world, attempt, "provider_reference")


def test_a_provider_id_on_the_salad_record_stays_blocked(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    world.env.execute(
        "UPDATE campaign_salad_attempts SET provider_id='provider-resource-1' WHERE attempt_id=?",
        world.attempt()["id"],
    )
    attempt = block_unknown(world)
    event = stays_blocked(world, attempt, "create_claimed")
    assert event["provider_reason"] == "provider_reference"


def _observe(world: SaladWorld, state: str, *, confirmed: int = 0, provider_id=None) -> None:
    attempt = world.attempt()
    lease = world.row("SELECT * FROM execution_leases WHERE grant_id=?", attempt["grant_id"])
    now = datetime.now(UTC).isoformat()
    world.env.execute(
        "INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id, epoch, "
        "desired_state, observed_state, observed_at, provider_id, termination_confirmed, "
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        "obs_" + uuid.uuid4().hex,
        attempt["grant_id"],
        lease["id"],
        lease["worker_principal_id"],
        lease["epoch"],
        "admitted",
        state,
        now,
        provider_id,
        confirmed,
        now,
    )


@pytest.mark.parametrize(
    ("state", "confirmed", "provider_id", "reason"),
    [
        ("running", 0, None, "provider_observation"),
        ("exited", 0, None, "provider_observation"),
        ("unknown", 1, None, "provider_observation"),
        ("unknown", 0, "provider-resource-1", "provider_reference"),
    ],
)
def test_running_exited_terminated_or_referenced_observations_stay_blocked(
    tmp_path, state, confirmed, provider_id, reason
):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    _observe(world, state, confirmed=confirmed, provider_id=provider_id)
    attempt = block_unknown(world)
    stays_blocked(world, attempt, reason)


def test_a_provider_without_the_hook_is_never_reconciled(tmp_path, monkeypatch):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    monkeypatch.delattr(SaladRouteProvider, "create_claim_state")
    attempt = block_unknown(world)
    stays_blocked(world, attempt, "no_claim_hook")


@pytest.mark.parametrize(
    "answer",
    [
        CreateClaim("unknown", "cannot_tell"),
        CreateClaim("none", "no_claim"),  # no proof code
        CreateClaim("none", "Not A Code", proof="p"),
        {"state": "none", "reason": "x", "proof": "p"},  # not a CreateClaim
        RuntimeError("provider read failed"),
    ],
)
def test_an_unknown_or_malformed_claim_state_stays_blocked(tmp_path, monkeypatch, answer):
    world = world_with(tmp_path)
    world.to_awaiting_review()

    def read(self, connection, attempt):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(SaladRouteProvider, "create_claim_state", read)
    attempt = block_unknown(world)
    stays_blocked(world, attempt, "claim_state_unknown")


# -- the per-campaign bound -------------------------------------------------------------------


def test_past_the_campaign_limit_it_stays_blocked_with_a_reason(tmp_path):
    world = world_with(tmp_path, limit=1)
    world.to_awaiting_review()
    first = block_unknown(world)
    world.tick()
    assert (
        world.row("SELECT status FROM campaign_stage_attempts WHERE id=?", first["id"])["status"]
        == "failed_transient"
    )
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    second = block_unknown(world)
    assert second["attempt"] == 2
    world.tick(3)
    assert world.status() == "blocked"
    assert world.attempt()["status"] == "unknown"
    assert len(world.events("campaign.attempt_unclaimed_reconciled")) == 1
    (stopped,) = world.events("campaign.autoreconcile_stopped")
    assert stopped["kind"] == "unclaimed" and stopped["attempt"] == 2
    assert stopped["reason"] == "not auto-reconciled: 1 of 1 automatic reconciles used"
    assert stopped["limit"] == 1 and stopped["used"] == 1
    # The person's route still settles it.
    grant_id = second["grant_id"]
    revision = grant(world, grant_id)["revision"]
    body = {"expected_revision": revision}
    assert grant(world, grant_id)["status"] == "active"
    ExecutionStore(world.env.state).revoke_execution_grant(
        world.human, grant_id, world.key("revoke"), "d", revision
    )
    body = {"expected_revision": grant(world, grant_id)["revision"]}
    status, _ = SaladUnclaimedStore(world.env.state).reconcile(
        world.human, grant_id, world.key("unclaimed"), canonical_digest(body), body
    )
    assert status == 200


def test_a_zero_limit_disables_it(tmp_path):
    world = world_with(tmp_path, limit=0)
    world.to_awaiting_review()
    attempt = block_unknown(world)
    world.tick(3)
    assert world.status() == "blocked"
    assert world.attempt()["status"] == "unknown"
    assert grant(world, attempt["grant_id"])["status"] == "active"  # nothing was revoked
    (stopped,) = world.events("campaign.autoreconcile_stopped")
    assert "disabled" in stopped["reason"]


def test_the_limit_is_validated():
    for bad in (-1, 11, True, "2"):
        with pytest.raises(ValueError):
            CampaignRunner("/nonexistent", unclaimed_autoreconcile_limit=bad)


# -- the operator route is unchanged ----------------------------------------------------------


def test_the_operator_route_stays_human_only(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    grant_id = world.attempt()["grant_id"]
    revision = grant(world, grant_id)["revision"]
    ExecutionStore(world.env.state).revoke_execution_grant(
        world.human, grant_id, world.key("revoke"), "d", revision
    )
    body = {"expected_revision": grant(world, grant_id)["revision"]}
    with pytest.raises(Forbidden):
        SaladUnclaimedStore(world.env.state).reconcile(
            world.worker, grant_id, world.key("unclaimed"), canonical_digest(body), body
        )
    status, response = SaladUnclaimedStore(world.env.state).reconcile(
        world.human, grant_id, world.key("unclaimed"), canonical_digest(body), body
    )
    assert status == 200 and response["reconciled_by_principal_id"] == world.human["id"]
    (event,) = world.events("execution.grant_reconciled_unclaimed")
    assert "automatic" not in event and event["proof"] == "no_create_or_start_claim"
    assert json.dumps(event)  # closed, serializable values only


def test_the_salad_provider_reads_its_claim_state_without_writing(tmp_path):
    world = world_with(tmp_path)
    world.to_awaiting_review()
    provider = get_provider("salad")
    attempt = world.attempt()
    with world.env.store.runner_connection() as connection:
        before = connection.total_changes
        claim = provider.create_claim_state(connection, attempt)
        assert connection.total_changes == before
    assert claim.state == "none" and claim.proof == "no_create_or_start_claim"
    assert claim.evidence["operation_id"] == world.salad()["operation_id"]
