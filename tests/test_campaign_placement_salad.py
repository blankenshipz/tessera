"""Salad as an alternate provider candidate (ADR 0060), end to end with fakes only.

The stage's own provider is a CPU worker-grant provider (``near-cpu``); Salad is its alternate.
The Salad executor, guardian and human are played by ``SaladWorld`` exactly as in the Salad
runner tests: no Salad call, no network, no money. ``near-cpu`` reports its capacity through
the provider's own ``free_slots`` hook, which a test double fixes at full or empty.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    HOLD,
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)
from test_execution import _worker

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import SALAD_RUNTIME
from tessera_control.campaign_providers import register_provider, unregister_provider

NEAR = "near-cpu"


class _Capacity(worker.WorkerGrantProvider):
    """A worker-grant provider whose free slots the test sets (its host is busy or idle)."""

    slots = 0

    def free_slots(self, rt, ctx, stage):
        return type(self).slots


@pytest.fixture
def near(tmp_path):
    def make(world: SaladWorld, slots: int) -> dict:
        env = world.env
        principal = _worker(env.state, env.owner, env.project["id"], "near-token")
        _Capacity.slots = slots
        register_provider(_Capacity(worker._descriptor(NEAR, principal["id"], None, 1)))
        return principal

    yield make
    unregister_provider(NEAR)


def _stage(env) -> dict:
    """A stage that prefers the CPU host and may run on Salad (a Salad CPU or GPU profile)."""
    salad = salad_stage_v2(env, "g1")
    return {
        **salad,
        "provider": NEAR,
        "runtime": "docker-cpu-v1",
        "provider_config": {},
        # The CPU worker's grant must outlive the stage's duration plus the settle margin.
        "grant_ttl_seconds": salad["max_duration_seconds"] + 600,
        "alternate_providers": [
            {
                "provider": "salad",
                "runtime": SALAD_RUNTIME,
                "provider_config": salad["provider_config"],
            }
        ],
    }


def _world(tmp_path, near, slots):
    world = SaladWorld(tmp_path)
    near(world, slots)
    world.spec = salad_spec_v2(world.env, [_stage(world.env)])
    return world


def _placed(world):
    return [e for e in world.events("campaign.stage_placed")]


def test_a_busy_cpu_host_sends_the_stage_to_its_salad_alternate_end_to_end(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    world.to_route_started()
    # Placed on Salad because the CPU host had no free slot; recorded once, with the reason.
    (placed,) = _placed(world)
    assert placed["stage_key"] == "g1" and placed["attempt"] == 1
    assert placed["candidates"] == [NEAR, "salad"] and placed["provider_id"] == "salad"
    assert placed["hold_cents"] == HOLD
    (skip,) = placed["skipped"]
    assert skip["provider_id"] == NEAR and skip["reason"] == "no_free_slot"
    stage = world.row(
        "SELECT provider_id FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )
    assert stage["provider_id"] == "salad"
    attempt = world.attempt()
    assert attempt["provider_id"] == "salad" and attempt["hold_cents"] == HOLD
    # The Salad route and the delegation's worker set follow the approved candidate.
    assert world.phase() == "route_started"
    from tessera_control.campaign_delegation import _spec_workers

    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert world.worker["id"] in _spec_workers(dict(campaign))
    # Nothing was granted to the CPU host's worker.
    assert not world.env.query(
        "SELECT 1 FROM execution_grants WHERE worker_principal_id LIKE 'worker-near%'"
    )
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    world.record_charge(13)
    world.tick_until(lambda: world.status() == "succeeded")
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["settled_cents"] == 13 and campaign["held_cents"] == 0
    assert len(_placed(world)) == 1  # one attempt, one placement

    receipt = json.loads(
        world.row(
            "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
        )["receipt_json"]
    )
    (stage_entry,) = receipt["stages"]
    assert stage_entry["provider_id"] == "salad" and stage_entry["runtime"] == SALAD_RUNTIME
    (entry,) = stage_entry["attempts"]
    assert entry["provider_id"] == "salad" and entry["runtime"] == SALAD_RUNTIME
    assert entry["settled_cents"] == 13 and entry["settlement_basis"] == "human_charge"
    assert entry["evidence_kind"] == "salad_live_receipt" and entry["evidence_digest"]
    assert entry["placement"]["provider_id"] == "salad"
    assert entry["placement"]["skipped"][0]["reason"] == "no_free_slot"

    view = world.env.store.get_campaign(world.env.owner, world.campaign_id)["placement"]
    assert view[0]["candidates"] == [NEAR, "salad"]
    assert view[0]["placements"][0]["provider_id"] == "salad" and view[0]["waiting"] is None


def test_an_idle_cpu_host_keeps_the_stage_off_salad(tmp_path, near):
    world = _world(tmp_path, near, slots=1)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    world.tick(3)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["skipped"] == []
    assert placed["not_evaluated"] == ["salad"]
    assert world.attempt()["provider_id"] == NEAR
    # The Salad alternate was never scaffolded, routed or held.
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None


def test_salad_excluded_by_steering_makes_the_stage_wait_for_the_cpu_host(tmp_path, near):
    from tessera_control.store import canonical_digest

    world = _world(tmp_path, near, slots=0)
    world.start()
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", world.campaign_id)
    body = {
        "expected_revision": revision["revision"],
        "stage_key": "g1",
        "exclude": ["salad"],
        "reason": "keep this stage off billable compute",
    }
    status, _ = world.env.store.steer_campaign_placement(
        world.human, world.campaign_id, "k-steer", canonical_digest(body), body
    )
    assert status == 200
    world.tick(5)
    # Never degraded onto Salad: no attempt, no hold, no Salad rows; it says why it waits.
    assert world.attempt() is None and not _placed(world)
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None
    held = world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)
    assert held["held_cents"] == 0
    (waiting,) = world.events("campaign.stage_waiting_placement")
    assert {(s["provider_id"], s["reason"]) for s in waiting["skipped"]} == {
        (NEAR, "no_free_slot"),
        ("salad", "excluded_by_steering"),
    }
    # The host frees a slot: the stage is placed there.
    _Capacity.slots = 1
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None


def test_a_salad_candidate_refuses_a_scratch_request(tmp_path, near):
    """Salad grants no scratch (ADR 0059, ADR 0070): a stage that asks for scratch and lists
    Salad is refused at submit rather than run there without it."""
    world = SaladWorld(tmp_path)
    near(world, 1)
    stage = {**_stage(world.env), "scratch_bytes": 128 * 1024 * 1024}
    with pytest.raises(Exception, match="scratch_bytes is not granted by Salad"):
        world.env.submit(spec=salad_spec_v2(world.env, [stage]))
