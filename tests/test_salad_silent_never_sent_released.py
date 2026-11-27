"""A route that was never sent anything is released at 0, never left ``unknown`` (q-2705 F4).

Field shape: four ``route_started`` routes queued behind the executor's own limits (desired
``run``, ``provider_status=not_queried``, no effects, receipts or provider id) whose silent
window passed. Synthetic state only; the unchanged unclaimed proof decides every release.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from salad_campaign_fixture import SaladWorld

from tessera_control.campaign_salad_commands import SILENT_EXECUTOR


def _later(world: SaladWorld, hours: int) -> None:
    world.runner.now = lambda: datetime.now(UTC) + timedelta(hours=hours)


def _intents(world: SaladWorld, attempt: int, step: str) -> list:
    return world.env.query(
        "SELECT status FROM campaign_step_intents WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=? AND step=?",
        world.campaign_id,
        attempt,
        step,
    )


def _first(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=1",
        world.campaign_id,
    )


def test_queued_never_sent_route_past_the_window_is_released_and_requeued(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _later(world, 3)

    world.tick_until(lambda: _first(world)["status"] == "failed_transient", limit=10)

    first = _first(world)
    assert first["failure_class"] == "not_started"
    assert first["settled_cents"] == 0 and first["settlement_basis"] == "unclaimed_zero"
    assert [row["status"] for row in _intents(world, 1, "cancel_route")] == ["done"]
    assert [row["status"] for row in _intents(world, 1, "reconcile_unclaimed_salad")] == ["done"]
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", first["grant_id"])["status"]
        == "reconciled"
    )
    assert world.status() == "running"
    assert world.events("campaign.blocked") == []
    # Requeued as a new attempt number under the stage's own caps (no scientific retry used).
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=2",
                world.campaign_id,
            )
            is not None
        ),
        limit=10,
    )


def test_a_create_claim_keeps_the_route_claimed(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.claim("create")
    _later(world, 3)

    world.tick(3)

    first = _first(world)
    assert first["status"] == "unknown" and first["settled_cents"] is None
    assert _intents(world, 1, "reconcile_unclaimed_salad") == []
    assert world.status() == "blocked"


def test_a_live_executor_queueing_the_route_is_not_silent(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    later = datetime.now(UTC) + timedelta(hours=3)
    _later(world, 3)
    # The executor kept renewing the lease while it deferred the route (executor_route_limit).
    world.env.execute(
        "UPDATE execution_leases SET renewed_at=?, expires_at=? WHERE id=?",
        (later - timedelta(minutes=1)).isoformat(),
        (later + timedelta(minutes=10)).isoformat(),
        world.salad()["lease_id"],
    )

    world.tick(3)

    assert _first(world)["status"] == "running"
    assert world.salad()["phase"] == "route_started"
    assert _intents(world, 1, "cancel_route") == []


def test_already_unknown_never_sent_route_is_cancelled_then_reconciled(tmp_path):
    """The four live attempts: unknown for the silent reason, refusing ``route_wants_run``."""
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.tick()
    attempt = world.attempt()
    world.driver._unknown(attempt, SILENT_EXECUTOR)
    world.runner._stage_unknown(world.runner._load(world.campaign_id), "g1")
    assert world.status() == "blocked"

    world.tick_until(lambda: _first(world)["status"] == "failed_transient", limit=6)

    first = _first(world)
    assert first["failure_class"] == "not_started" and first["settled_cents"] == 0
    assert [row["status"] for row in _intents(world, 1, "cancel_route")] == ["done"]
    world.tick_until(lambda: world.status() == "running", limit=4)
