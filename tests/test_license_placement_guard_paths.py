"""Licence-label guard on the reroute paths and artifact inputs (ADR 0113). Fakes only."""

# ruff: noqa: F811
from __future__ import annotations

import copy

import pytest
from campaign_runner_harness import dump
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, NEAR, _placed, _stage, _world, providers  # noqa: F401
from test_campaign_placement_reroute import KILLED, OK, _granted, _reroute_stage
from test_license_placement_guard import MESSAGE, _label, _policy

from tessera_control.store import Conflict

_ = providers  # fixture re-exported for pytest


def test_a_restricted_reroute_target_is_refused_at_submit(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open"])
    _label(env, ["restricted"])
    with pytest.raises(Conflict, match=MESSAGE) as refused:
        world.campaign(_spec2(_reroute_stage(env)))
    assert FAR in str(refused.value) and "restricted" in str(refused.value)


def test_a_reroute_target_restricted_after_approval_is_not_placed_and_says_why(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    world.scenarios = [(KILLED, {"data.json": OK}), (0, {"data.json": OK})]
    campaign = world.campaign(_spec2(_reroute_stage(env)))
    cid = campaign["id"]
    world.tick_until(lambda: _granted(world, cid, 1), worker=False)
    # After approval the input gains a label and the far provider's trust entry forbids it.
    _label(env, ["restricted"])
    _policy(env, world.far_worker, far_labels=["open"])
    world.tick_all(cid)
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert 2 not in placed or placed[2]["provider_id"] != FAR, dump(world, cid)
    attempts = {a["attempt"]: a for a in world.attempts(cid)}
    assert all(a["provider_id"] != FAR for a in attempts.values())
    skipped = [s for p in placed.values() for s in p.get("skipped", ())]
    # ADR 0058's reroute refusal records the guard's own message as the reason detail.
    assert any(
        s["provider_id"] == FAR and s["reason"] == "reroute_refused" and MESSAGE in s["detail"]
        for s in skipped
    ), dump(world, cid)


def _with_artifact(spec, stage_key, campaign_id, stage, output, sha256):
    item = {
        "kind": "artifact",
        "name": "art",
        "campaign_id": campaign_id,
        "stage": stage,
        "output": output,
        "sha256": sha256,
    }
    spec = copy.deepcopy(spec)
    for entry in spec["stages"]:
        if entry["key"] == stage_key:
            entry["inputs"].append(item)
    return spec


def test_artifact_inputs_follow_the_guard_end_to_end(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _label(env, ["restricted"])
    _policy(env, world.far_worker, far_labels=["open"])
    source = world.campaign(_spec2(_stage(env, "a", provider=NEAR, alternates=())))["id"]
    world.tick_all(source)
    assert world.status(source) == "succeeded", dump(world, source)
    row = world.row(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output'", source
    )

    def consumer(campaign_id, stage="a"):
        base = _spec2(_stage(env, "c", provider=FAR, alternates=()))
        return _with_artifact(base, "c", campaign_id, stage, row["name"], row["sha256"])

    # The source stage's label travels with the artifact: the restricted provider refuses.
    with pytest.raises(Conflict, match=MESSAGE) as refused:
        world.campaign(consumer(source))
    assert "restricted" in str(refused.value)
    # A provider listed without the key is unrestricted.
    _policy(env, world.far_worker)
    assert world.campaign(consumer(source))["id"]
    # An unresolvable source never reaches the guard: submit refuses it first (the label
    # layer's unknown-lineage marker is covered in test_license_labels).
    _policy(env, world.far_worker, far_labels=["open", "restricted", "unlabelled"])
    with pytest.raises(Conflict, match="does not resolve"):
        world.campaign(consumer("campaign_missing"))
