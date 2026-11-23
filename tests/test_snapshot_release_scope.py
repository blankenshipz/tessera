"""An idle campaign's input release is freed at its transition; a live holder still blocks."""

from __future__ import annotations

import pytest
from campaign_runner_harness import World, admit, stage_spec

from tessera_control.campaign_release_scope import holder_refusal

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _spec(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return spec


def _active(world: World) -> list:
    return world.env.query("SELECT * FROM snapshot_releases WHERE status='active'")


def _settled_holder(world: World, status: str = "running") -> tuple[str, str]:
    """A campaign whose only attempt is finished and settled but whose release is active,
    left in ``status`` (an older runner, or a transition that never revoked it)."""
    campaign = world.campaign(_spec(world))
    cid = campaign["id"]
    complete = world.runner._maybe_complete
    world.runner._maybe_complete = lambda ctx: None
    try:
        world.tick_until(
            lambda: (
                any(a["settled_cents"] is not None for a in world.attempts(cid))
                and world.attempts(cid)[0]["status"] == "succeeded"
            )
        )
    finally:
        world.runner._maybe_complete = complete
    (attempt,) = world.attempts(cid)
    assert attempt["release_id"]
    assert world.row("SELECT status FROM snapshot_releases WHERE id=?", attempt["release_id"])[
        "status"
    ] == ("active")
    world.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, cid)
    return cid, attempt["release_id"]


def _freed(world: World, release_id: str) -> list[dict]:
    return [
        event
        for event in world.events_of("campaign.release_freed")
        if event["payload"]["release_id"] == release_id
    ]


def _assert_freed(world: World, cid: str, release_id: str, trigger: str, reason: str) -> None:
    row = world.row("SELECT status, revoked_at FROM snapshot_releases WHERE id=?", release_id)
    assert row["status"] == "revoked" and row["revoked_at"]
    (event,) = _freed(world, release_id)
    payload = event["payload"]
    assert payload["campaign_id"] == cid
    assert payload["snapshot_id"] == world.env.snapshot_id
    assert payload["snapshot_digest"] == world.env.snapshot_digest
    assert payload["released_by"] == "system" and payload["automatic"] is True
    assert payload["trigger"] == trigger and payload["reason"] == reason


def _new_campaign_admitted(world: World) -> None:
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    (attempt,) = world.attempts(fresh["id"])
    assert attempt["status"] == "admitted" and attempt["release_id"]


@pytest.mark.parametrize("status", ["failed", "stopped", "cancelled", "succeeded"])
def test_terminal_transition_frees_the_release_and_a_new_campaign_is_admitted(tmp_path, status):
    world = World(tmp_path)
    cid, release_id = _settled_holder(world)
    world.runner._finish(world.runner._campaign(cid), status)
    assert world.status(cid) == status
    _assert_freed(world, cid, release_id, "campaign_terminal_transition", status)
    _new_campaign_admitted(world)


def test_settled_blocked_transition_frees_the_release(tmp_path):
    world = World(tmp_path)
    cid, release_id = _settled_holder(world)
    world.runner._stage_unknown(world.runner._load(cid), "s1", "inconclusive")
    assert world.status(cid) == "blocked"
    _assert_freed(world, cid, release_id, "campaign_blocked", "blocked")
    _new_campaign_admitted(world)


def test_paused_settled_campaign_frees_the_release(tmp_path):
    world = World(tmp_path)
    cid, release_id = _settled_holder(world)
    world.runner._pause(world.runner._load(cid), "approval_expired")
    assert world.status(cid) == "paused_delegation_lapsed"
    _assert_freed(world, cid, release_id, "campaign_paused", "paused_delegation_lapsed")


def test_release_is_freed_in_the_same_transaction_as_the_terminal_state(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, release_id = _settled_holder(world)
    original = world.runner.store.record_runner_event

    def refuse(connection, campaign, event_type, payload, **kwargs):
        if event_type == "campaign.release_freed":
            raise RuntimeError("event write failed")
        return original(connection, campaign, event_type, payload, **kwargs)

    monkeypatch.setattr(world.runner.store, "record_runner_event", refuse)
    with pytest.raises(RuntimeError):
        world.runner._finish(world.runner._campaign(cid), "failed")
    assert world.status(cid) == "running"
    assert world.row("SELECT status FROM snapshot_releases WHERE id=?", release_id)["status"] == (
        "active"
    )


def test_running_holder_still_blocks_and_the_refusal_names_it_and_the_expiry(tmp_path):
    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    admit(world, holder["id"])
    (live,) = world.attempts(holder["id"])
    release = world.row("SELECT * FROM snapshot_releases WHERE id=?", live["release_id"])
    second = world.campaign(_spec(world))
    world.tick_until(lambda: world.attempts(second["id"]), worker=False)
    world.tick_until(lambda: world.status(second["id"]) == "failed", worker=False)
    (refused,) = world.attempts(second["id"])
    assert refused["failure_class"] == "step_refused" and refused["grant_id"] is None
    assert holder["id"] in refused["failure_detail"]
    assert release["expires_at"] in refused["failure_detail"]
    assert world.row("SELECT status FROM snapshot_releases WHERE id=?", release["id"])[
        "status"
    ] == ("active")
    assert world.attempts(holder["id"])[0]["status"] == "admitted"


def test_live_attempt_of_any_campaign_keeps_the_shared_release(tmp_path):
    world = World(tmp_path)
    done, old_release = _settled_holder(world, status="succeeded")
    world.env.execute("UPDATE snapshot_releases SET status='revoked' WHERE id=?", old_release)
    live = world.campaign(_spec(world))
    admit(world, live["id"])
    (attempt,) = world.attempts(live["id"])
    shared = attempt["release_id"]
    # The finished campaign also used the live campaign's release (one shared row).
    world.env.execute(
        "UPDATE campaign_stage_attempts SET release_id=? WHERE campaign_id=?", shared, done
    )
    world.env.execute("UPDATE campaigns SET status='running' WHERE id=?", done)
    world.runner._finish(world.runner._campaign(done), "failed")
    assert world.status(done) == "failed"
    # Even the holder's own terminal transition keeps it while its attempt is live.
    world.runner._finish(world.runner._campaign(live["id"]), "failed")
    world.runner.tick()
    assert world.row("SELECT status FROM snapshot_releases WHERE id=?", shared)["status"] == (
        "active"
    )
    assert not _freed(world, shared)


def test_unknown_attempt_keeps_the_release_when_blocked(tmp_path):
    world = World(tmp_path)
    cid, release_id = _settled_holder(world)
    (attempt,) = world.attempts(cid)
    world.runner.block_settlement_mismatch(attempt)
    assert world.status(cid) == "blocked"
    # The attempt is unknown (its run may be live): the blocked transition keeps it, and so
    # does the reconcile sweep while the attempt stays unknown.
    world.runner._free_idle_releases()
    assert world.row("SELECT status FROM snapshot_releases WHERE id=?", release_id)["status"] == (
        "active"
    )
    assert not _freed(world, release_id)


def test_old_terminal_campaign_release_is_freed_by_the_reconcile_sweep(tmp_path):
    world = World(tmp_path)
    cid, release_id = _settled_holder(world, status="failed")
    assert not _freed(world, release_id)
    world.restart_runner()
    world.runner.tick()
    _assert_freed(world, cid, release_id, "reconcile_sweep", "failed")
    world.runner.tick()
    assert len(_freed(world, release_id)) == 1
    _new_campaign_admitted(world)


def test_refusal_to_another_organization_names_no_campaign(tmp_path):
    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    admit(world, holder["id"])
    (live,) = world.attempts(holder["id"])
    release = world.row("SELECT * FROM snapshot_releases WHERE id=?", live["release_id"])
    org = world.row("SELECT org_id FROM campaigns WHERE id=?", holder["id"])["org_id"]
    connection = world.runner.store.runner_connection
    with connection() as conn:
        other = holder_refusal(conn, release, "org_elsewhere")
        same = holder_refusal(conn, release, org)
    assert holder["id"] in same and release["expires_at"] in same
    assert other == f"the input snapshot is held until {release['expires_at']}"
    assert holder["id"] not in other and org not in other and release["id"] not in other
