"""Execution frames across a two-vendor campaign DAG (ADR 0041), reusing that harness.

Each stage attempt opens a frame under its provider, and every frame closes through
settle_frame with nothing left reserved. Fakes only; no network.
"""

from __future__ import annotations

import json

from campaign_runner_harness import World, dump
from test_campaign_multi_vendor_dag import VENDOR, _chain, _setup, second_vendor  # noqa: F401

from tessera_control import execution_frames as frames


def _frames(world: World, campaign_id: str) -> list:
    return world.env.query(
        """SELECT f.*, a.stage_key, a.settled_cents FROM execution_frames f
        JOIN campaign_stage_attempts a ON a.id=f.campaign_attempt_id
        WHERE a.campaign_id=? ORDER BY a.stage_key, a.attempt""",
        campaign_id,
    )


def _held(world: World, campaign_id: str) -> int:
    return world.row("SELECT held_cents FROM campaigns WHERE id=?", campaign_id)["held_cents"]


def test_each_vendor_stage_opens_and_closes_its_own_frame(tmp_path, second_vendor):  # noqa: F811
    world = World(tmp_path)
    env = _setup(world, second_vendor)
    world.scenarios = [(0, {"data.json": json.dumps({"score": n})}) for n in (1, 2, 3)]
    campaign = world.campaign(_chain(env))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    found = _frames(world, campaign["id"])
    assert [(f["stage_key"], f["provider_id"]) for f in found] == [
        ("a", "local-docker"),
        ("b", VENDOR),
        ("c", "local-docker"),
    ]
    parent = frames.campaign_frame_id(campaign["id"])
    for frame in found:
        assert frame["frame_id"] == frames.stage_frame_id(parent, frame["stage_key"], 1)
        assert frame["state"] == "closed" and frame["outcome"] == "succeeded"
        assert frame["receipt_digest"] and frame["settled_cents"] == 0
    assert _held(world, campaign["id"]) == 0


def test_a_failing_vendor_stage_closes_its_frame_and_releases_the_hold(
    tmp_path,
    second_vendor,  # noqa: F811
):
    world = World(tmp_path)
    env = _setup(world, second_vendor)
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (9, {"data.json": '{"score": 2}'})]
    campaign = world.campaign(_chain(env))
    world.tick_all(campaign["id"])
    found = _frames(world, campaign["id"])
    vendor = [f for f in found if f["stage_key"] == "b"]
    assert vendor and all(f["provider_id"] == VENDOR for f in vendor), dump(world, campaign["id"])
    assert all(f["state"] == "closed" and f["outcome"] == "failed" for f in vendor)
    assert all(f["state"] == "closed" for f in found)
    assert _held(world, campaign["id"]) == 0
