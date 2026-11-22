"""An agent's release revoke while the runner is cancelling the same campaign (ADR 0055)."""

from __future__ import annotations

from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import CampaignRunner
from tessera_control.store import canonical_digest

ACTIONS = ["campaign:submit", "campaign:read", "campaign:cancel", "campaign:logs"]
METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


def _cancelling(tmp_path):
    world = World(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    spec = env.spec()
    spec["stages"] = [stage_spec(env, "s1", metrics=METRIC)]
    campaign = env.approve(env.submit(agent, spec))
    cid = campaign["id"]
    world.tick_until(lambda: any(a["release_id"] for a in world.attempts(cid)), worker=False)
    body = {
        "expected_revision": world.row("SELECT revision FROM campaigns WHERE id=?", cid)[0],
        "reason": "stop",
    }
    status, _ = env.store.cancel_campaign(agent, cid, "cancel-1", canonical_digest(body), body)
    assert status == 200 and world.status(cid) == "cancel_requested"
    release = world.row("SELECT * FROM snapshot_releases WHERE status='active'")
    return world, agent, cid, release


def _revoke(world, agent, cid, release):
    body = {"expected_revision": release["revision"], "reason": "stale"}
    return world.env.store.revoke_campaign_release(
        agent, cid, release["id"], "revoke-1", canonical_digest(body), body
    )


def test_a_release_revoked_by_the_agent_first_leaves_the_runners_cancel_clean(tmp_path):
    world, agent, cid, release = _cancelling(tmp_path)
    status, done = _revoke(world, agent, cid, release)
    assert status == 200 and done["status"] == "revoked"
    for _ in range(20):
        world.runner.tick()
        if world.status(cid) == "cancelled":
            break
    assert world.status(cid) == "cancelled", dump(world, cid)
    revokes = world.env.events("workspace.snapshot_release_revoked")
    assert [e["payload"]["release_id"] for e in revokes] == [release["id"]]  # revoked once


def test_a_stale_runner_revoke_after_the_agents_does_not_wedge_the_cancel(tmp_path, monkeypatch):
    """The runner read the release before the agent revoked it, then sends its older revision."""
    world, agent, cid, release = _cancelling(tmp_path)
    original = CampaignRunner._rows
    stale = dict(release)
    replayed: list[bool] = []

    def rows(self, sql, params=()):
        found = original(self, sql, params)
        if "FROM snapshot_releases r WHERE r.status='active'" in sql:
            if not found:  # served after the agent revoked: replay what the runner had read
                replayed.append(True)
                return [{**stale, "stage_key": "s1", "attempt": 1}]
        return found

    assert _revoke(world, agent, cid, release)[0] == 200
    monkeypatch.setattr(CampaignRunner, "_rows", rows)
    for _ in range(30):
        try:
            world.runner.tick()
        except Exception as error:  # a step refused for a stale revision must not escape the tick
            raise AssertionError(f"tick raised {error!r}") from error
        if world.status(cid) == "cancelled":
            break
    assert replayed  # the runner really did send its older revision
    monkeypatch.undo()
    for _ in range(20):
        if world.status(cid) == "cancelled":
            break
        world.runner.tick()
    assert world.status(cid) == "cancelled", dump(world, cid)
