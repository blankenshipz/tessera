"""Campaign runner execution frames (ADR 0041 stages 1, 3 and 4-lite).

The in-process control app and a fake worker adapter (no Docker, no network). Each stage
attempt opens a frame when its hold is reserved, follows the attempt, and closes through
settle_frame in the settlement transaction, so a finished or errored attempt releases its
campaign hold at once. An unregistered provider never settles.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import execution_frames as frames
from tessera_control.campaign_steps import StepFailed

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _campaign(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return world.campaign(spec)


def _frames(world: World, campaign_id: str) -> list:
    return world.env.query(
        """SELECT f.* FROM execution_frames f JOIN campaign_stage_attempts a
        ON a.id=f.campaign_attempt_id WHERE a.campaign_id=? ORDER BY a.attempt""",
        campaign_id,
    )


def _states(world: World, frame_id: str) -> list[str]:
    return [
        row["to_state"]
        for row in world.env.query(
            "SELECT to_state FROM execution_frame_events WHERE frame_id=? ORDER BY sequence",
            frame_id,
        )
    ]


def _ledger(world: World, campaign_id: str) -> tuple[int, int]:
    row = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", campaign_id)
    return row["held_cents"], row["settled_cents"]


def test_a_reservation_opens_a_reserved_frame_with_a_deterministic_id(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)
    world.tick_until(lambda: world.attempts(campaign["id"]), worker=False)
    (attempt,) = world.attempts(campaign["id"])
    (frame,) = _frames(world, campaign["id"])
    parent = frames.campaign_frame_id(campaign["id"])
    assert frame["frame_id"] == frames.stage_frame_id(parent, "s1", 1)
    assert frame["parent_frame_id"] == parent and frame["kind"] == "stage"
    assert frame["hold_cents"] == attempt["hold_cents"]
    assert frame["provider_id"] == "local-docker" and frame["shape_digest"]
    assert (frame["org_id"], frame["project_id"]) == (
        campaign["org_id"],
        campaign["project_id"],
    )
    assert _states(world, frame["frame_id"])[:3] == ["requested", "validated", "reserved"]


def test_a_finished_attempt_closes_its_frame_and_releases_the_hold_in_one_step(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)

    def reconciled() -> bool:
        return any(a["status"] == "reconciled" for a in world.attempts(campaign["id"]))

    world.tick_until(reconciled)
    # The same transaction that recorded the reconcile settled the frame: nothing held.
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["settled_cents"] == 0 and attempt["settlement_basis"] == "local_zero"
    assert _ledger(world, campaign["id"]) == (0, 0)
    (frame,) = _frames(world, campaign["id"])
    assert frame["state"] == "closed" and frame["outcome"] == "succeeded"
    assert frame["create_intent_id"] == attempt["grant_id"]
    assert frame["provider_handle"] == attempt["operation_id"]
    assert _states(world, frame["frame_id"]) == [
        "requested",
        "validated",
        "reserved",
        "creating",
        "running",
        "succeeded",
        "settling",
        "closed",
    ]
    settlement = world.row(
        "SELECT * FROM execution_frame_settlements WHERE frame_id=?", frame["frame_id"]
    )
    receipt = json.loads(settlement["receipt_json"])
    assert settlement["receipt_digest"] == frame["receipt_digest"]
    assert receipt["outcome"] == "succeeded" and receipt["basis"] == "local_zero"
    assert receipt["final"] is True and receipt["hold_cents"] == frame["hold_cents"]
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert len(world.events_of("frame.closed")) == 1


def test_an_errored_attempt_closes_as_failed_and_releases_at_once(tmp_path):
    world = World(tmp_path)
    world.scenarios = [(3, {"data.json": "{}"})] * 4
    campaign = _campaign(world)
    world.tick_all(campaign["id"])
    attempts = world.attempts(campaign["id"])
    assert attempts and all(a["settled_cents"] is not None for a in attempts), dump(
        world, campaign["id"]
    )
    assert _ledger(world, campaign["id"])[0] == 0
    found = _frames(world, campaign["id"])
    assert len(found) == len(attempts)
    for frame in found:
        assert frame["state"] == "closed" and frame["outcome"] == "failed"
        assert "failed" in _states(world, frame["frame_id"])


def test_cancel_before_any_grant_closes_the_frame_as_cancelled(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)
    world.tick_until(lambda: world.attempts(campaign["id"]), worker=False)
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["grant_id"] is None and attempt["status"] == "reserved"
    world.cancel(campaign)
    world.tick_all(campaign["id"], worker=False)
    (frame,) = _frames(world, campaign["id"])
    assert frame["outcome"] == "cancelled" and frame["create_intent_id"] is None
    assert frame["state"] == "closed" and _ledger(world, campaign["id"]) == (0, 0)


def test_a_second_settle_of_the_same_attempt_is_a_no_op(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)
    world.tick_until(
        lambda: any(a["status"] == "reconciled" for a in world.attempts(campaign["id"]))
    )
    (attempt,) = world.attempts(campaign["id"])
    before = world.env.query("SELECT count(*) AS n FROM execution_frame_events")[0]["n"]
    runner = world.runner
    with runner.store.runner_connection() as connection:
        assert runner._lease(connection)
        runner._settle(connection, attempt["id"], 0, "local_zero")
    assert world.env.query("SELECT count(*) AS n FROM execution_frame_events")[0]["n"] == before
    from tessera_control.store import Conflict

    with pytest.raises(Conflict):
        with runner.store.runner_connection() as connection:
            runner._settle(connection, attempt["id"], 0, "unclaimed_zero")
    assert _ledger(world, campaign["id"]) == (0, 0)


def _unregister_local_docker():
    from tessera_control.campaign_providers import unregister_provider

    unregister_provider("local-docker")  # the conftest fixture registers it again


def test_an_unregistered_provider_never_settles(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)
    world.tick_until(lambda: world.attempts(campaign["id"]), worker=False)
    (attempt,) = world.attempts(campaign["id"])
    held = _ledger(world, campaign["id"])
    _unregister_local_docker()
    runner = world.runner
    with pytest.raises(StepFailed, match="settlement_unavailable"):
        with runner.store.runner_connection() as connection:
            assert runner._lease(connection)
            runner._settle(connection, attempt["id"], 0, "unclaimed_zero")
    assert world.attempts(campaign["id"])[0]["settled_cents"] is None
    assert _ledger(world, campaign["id"]) == held
    (frame,) = _frames(world, campaign["id"])
    assert frame["state"] == "reserved"


def test_an_unregistered_provider_settlement_refusal_is_recorded_once(tmp_path):
    world = World(tmp_path)
    campaign = _campaign(world)
    world.tick_until(lambda: world.attempts(campaign["id"]), worker=False)
    held = _ledger(world, campaign["id"])
    world.cancel(campaign)
    _unregister_local_docker()
    for _ in range(3):
        world.runner.tick()
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["settled_cents"] is None and _ledger(world, campaign["id"]) == held
    assert world.status(campaign["id"]) == "cancel_requested"
    (frame,) = _frames(world, campaign["id"])
    assert frame["ambiguous_reason"] == "provider_unregistered"
    assert frame["state"] == "reserved" and frame["settlement_id"] is None
    events = world.events_of("campaign.settlement_unavailable")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["reason"] == "provider_unregistered"
    assert payload["provider_id"] == "local-docker" and payload["frame_id"] == frame["frame_id"]
    assert len(world.events_of("frame.ambiguous")) == 1


def test_an_unknown_attempt_a_human_reconciled_closes_lost_not_failed(tmp_path):
    """No exit was observed, so the frame must not claim a failure (ADR 0041 section 1)."""
    world = World(tmp_path)
    campaign = _campaign(world)
    cid = campaign["id"]
    # Drive to a bound operation, then let the runner see the attempt as unknown.
    world.tick_until(lambda: world.attempts(cid) and world.attempts(cid)[0]["grant_id"])
    (attempt,) = world.attempts(cid)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown', exit_code=NULL, "
        "run_receipt_digest=NULL, operation_id=COALESCE(operation_id, 'op-unobserved') WHERE id=?",
        attempt["id"],
    )
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled' WHERE id=?", attempt["grant_id"]
    )
    world.env.execute("UPDATE campaigns SET status='blocked' WHERE id=?", cid)
    for _ in range(20):
        world.runner.tick()
        if world.attempts(cid)[0]["settled_cents"] is not None:
            break
    assert world.attempts(cid)[0]["settled_cents"] is not None, dump(world, cid)
    (frame,) = _frames(world, cid)
    assert frame["state"] == "closed"
    assert frame["outcome"] == "lost", frame["outcome"]
