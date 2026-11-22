"""A retry moved to another candidate finishes that provider's preparation first (ADR 0060).

The stage runs first on ``near-cpu`` and fails with a declared transient exit code. Its retry is
placed on ``far-cpu``, whose ``prepare`` (a stand-in for multi-tick scaffolding such as Salad's)
reports progress, then waits for a person, then is ready. No hold, attempt or grant may exist on
``far-cpu`` before it is ready. Fakes only.
"""

from __future__ import annotations

import pytest
from campaign_runner_harness import dump
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, NEAR, _placed, _stage, _world

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import register_provider, unregister_provider


class _Host(worker.WorkerGrantProvider):
    """``near-cpu``: its capacity is set by the test."""

    slots = 1

    def free_slots(self, rt, ctx, stage):
        return type(self).slots


class _Scaffolded(worker.WorkerGrantProvider):
    """``far-cpu``: progress on the first call, then waits for a person until released."""

    calls = 0
    released = False

    def prepare(self, rt, ctx, key):
        type(self).calls += 1
        if type(self).calls == 1:
            return "progress"
        return "ready" if type(self).released else "waiting"


@pytest.fixture
def providers():
    made: list[str] = []
    _Host.slots, _Scaffolded.calls, _Scaffolded.released = 1, 0, False

    def make(provider_id: str, principal: str, slots: int = 1) -> None:
        cls = _Host if provider_id == NEAR else _Scaffolded
        register_provider(cls(worker._descriptor(provider_id, principal, None, slots)))
        made.append(provider_id)

    yield make
    for provider_id in made:
        unregister_provider(provider_id)


def test_a_retry_moved_to_a_candidate_waits_until_its_prepare_is_ready(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    world.scenarios = [(3, {"data.json": '{"score": 1}'}), (0, {"data.json": '{"score": 2}'})]
    stage = _stage(env, "a", retry={"max_attempts": 2, "transient_exit_codes": [3]})
    campaign = world.campaign(_spec2(stage))
    cid = campaign["id"]
    world.tick_until(lambda: bool(world.attempts(cid)) and world.attempts(cid)[0]["grant_id"])
    # The host is busy from now on (attempt 1 already holds its grant): the retry must move.
    _Host.slots = 0
    world.tick_until(lambda: world.attempts(cid)[0]["status"] == "failed_transient")
    assert world.attempts(cid)[0]["provider_id"] == NEAR
    for _ in range(6):
        world.runner.tick()
        world.poller.run(max_cycles=1)
    # Placed on far-cpu, but its preparation is not finished: nothing is reserved there.
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert placed[2]["provider_id"] == FAR
    assert placed[2]["skipped"][0] == {
        "provider_id": NEAR,
        "reason": "no_free_slot",
        "detail": "0 free, 0 placed awaiting a grant",
    }
    assert [a["attempt"] for a in world.attempts(cid)] == [1], dump(world, cid)
    assert _Scaffolded.calls >= 3  # re-entered every tick, not skipped after the first
    held = world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"]
    assert held == 0
    assert not world.env.query(
        "SELECT 1 FROM execution_grants WHERE worker_principal_id=?", world.far_worker["id"]
    )
    # The person acts; far-cpu is ready, and only now the retry is reserved and run there.
    _Scaffolded.released = True
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    attempts = world.attempts(cid)
    assert [(a["attempt"], a["provider_id"]) for a in attempts] == [(1, NEAR), (2, FAR)]
    assert len(_placed(world, cid)) == 2  # one placement per attempt, never re-decided
