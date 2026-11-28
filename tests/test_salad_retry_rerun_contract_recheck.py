"""An adapted Salad stage keeps a valid workload contract across a checkpoint retry and a re-run.

A checkpoint-resume retry (ADR 0197) composes a new input snapshot, so a contract that declares a
pinned snapshot is bound to the composed digest exactly as a late-bound one is. A spec whose
adapted contract no longer declares what a fresh input plan gives is a 409 at submit and approval.
"""

from __future__ import annotations

import copy
import json

import pytest
from test_salad_stage_adapter import cpu_world, expanded, near, spec_of  # noqa: F401

from tessera_control.campaign_models import normalize, parse_spec, stage_on_candidate
from tessera_control.campaign_salad import stage_workload
from tessera_control.store import Conflict


def _expanded_json(world):
    return json.loads(expanded(world).model_dump_json(exclude_none=True))


def test_a_checkpoint_composed_retry_binds_a_pinned_contract_to_the_composed_digest(tmp_path, near):
    world = cpu_world(tmp_path, near)
    data = _expanded_json(world)
    normal = stage_on_candidate(normalize(parse_spec(data)).stages[0], "salad")
    declared = data["stages"][0]["alternate_providers"][0]["provider_config"]["workload"]["input"]
    assert declared["snapshot_digest"] == world.env.snapshot_digest
    row = {"input_snapshot_digest": "e" * 64}
    # No checkpoint composition: the reviewed contract is untouched (a changed source still fails
    # closed at the route check, never silently rebound).
    assert stage_workload(row, normal).input.snapshot_digest == declared["snapshot_digest"]
    bound = stage_workload(row, normal, checkpoint_bytes=777)
    assert bound.input.snapshot_digest == "e" * 64
    # The reviewed cap grows by exactly the checkpoint's recorded bytes (ADR 0211).
    cap = declared["max_bytes"] + 777
    assert bound.input.max_bytes == cap
    assert f"input:{'e' * 64}:{cap}" in bound.argv_template
    assert f"input:{declared['snapshot_digest']}:{declared['max_bytes']}" not in bound.argv_template
    # No checkpoint bytes: the contract is bound only when it is late-bound, cap untouched.
    assert stage_workload(row, normal, checkpoint_bytes=None).input.max_bytes == declared["max_bytes"]
    # Nothing composed yet: nothing to bind.
    assert (
        stage_workload({}, normal, checkpoint_bytes=777).input.snapshot_digest
        == declared["snapshot_digest"]
    )


def _stale(data, **change):
    spec = copy.deepcopy(data)
    work = spec["stages"][0]["alternate_providers"][0]["provider_config"]["workload"]
    declared = work["input"]
    old = f"input:{declared['snapshot_digest']}:{declared['max_bytes']}"
    declared.update(change)
    new = f"input:{declared['snapshot_digest']}:{declared['max_bytes']}"
    work["argv_template"] = [new if token == old else token for token in work["argv_template"]]
    return spec


def test_a_current_contract_submits_and_a_stale_one_is_a_409_before_any_hold(tmp_path, near):
    world = cpu_world(tmp_path, near)
    data = _expanded_json(world)
    assert world.env.submit(spec=data)["id"]  # the fresh derivation passes
    for change in ({"snapshot_digest": "d" * 64}, {"max_bytes": 1}):
        with pytest.raises(Conflict, match="adapted Salad contract is stale"):
            world.env.submit(spec=_stale(data, **change))


# -- a checkpoint retry through the runner (fakes: the Salad world and its executor double) ----


@pytest.fixture
def adapted(monkeypatch):
    """Treat the world's reviewed contract as an adapter-derived one (pinned snapshot input)."""
    from tessera_control import campaign_salad

    real = campaign_salad.salad_config
    monkeypatch.setattr(
        campaign_salad,
        "salad_config",
        lambda stage: real(stage).model_copy(update={"adapted": True}),
    )


def _pinned_world(tmp_path):
    """The checkpoint world whose contract pins the stage's snapshot as its input, capped at
    exactly the base inputs' bytes (what the adapter derives for a pinned input)."""
    from test_salad_interruption_retry import checkpoint_world

    world = checkpoint_world(tmp_path)
    row = world.row(
        "SELECT manifest_json FROM source_snapshots WHERE id=?", world.env.snapshot_id
    )
    base = json.loads(row["manifest_json"])["total_bytes"]
    (stage,) = world.spec["stages"]
    stage["provider_config"]["workload"]["input"] = {
        "kind": "staged_snapshot",
        "snapshot_digest": world.env.snapshot_digest,
        "max_bytes": base,
    }
    return world


def _to_retry(world):
    from test_salad_interruption_retry import interrupted_with_manifest

    interrupted_with_manifest(world)
    world.tick_until(
        lambda: world.attempt()["attempt"] == 2 and world.attempt()["trust_policy_digest"]
    )


def test_a_checkpoint_retry_binds_a_contract_admission_accepts(tmp_path, adapted):
    """The retry composed with the checkpoint (real runner, Salad world): the contract the route
    carries names the composed snapshot, and its raised cap admits it where the reviewed cap
    would not (admission refuses ``manifest total_bytes > input.max_bytes``).

    Limit: the world's retry stops at the grant step (the approved task plan binds the base
    snapshot digest), so route approval itself is not driven here."""
    from types import SimpleNamespace

    from test_salad_interruption_retry import CHECKPOINT

    world = _pinned_world(tmp_path)
    _to_retry(world)
    stage = dict(world.row("SELECT * FROM campaign_stages WHERE stage_key='g1'"))
    snapshot = world.row(
        "SELECT manifest_json FROM source_snapshots WHERE id=?", stage["input_snapshot_id"]
    )
    total = json.loads(snapshot["manifest_json"])["total_bytes"]
    ctx = SimpleNamespace(campaign=dict(world.row("SELECT * FROM campaigns")))
    driver = world.driver
    spec_stage = normalize(parse_spec(world.spec)).stages[0]
    reviewed = spec_stage.provider_config["workload"]["input"]["max_bytes"]
    assert total > reviewed  # the reviewed cap alone would refuse the composed snapshot
    bound = driver._workload(ctx, stage, spec_stage)
    assert bound.input.snapshot_digest == stage["input_snapshot_digest"]
    assert bound.input.max_bytes == reviewed + len(CHECKPOINT) >= total
    assert f"{stage['input_snapshot_digest']}:{bound.input.max_bytes}" in "".join(
        bound.argv_template
    ) or bound.runtime != "gpu_runtime_v1"
    assert driver.retry_input_refusal(ctx, stage, spec_stage) is None  # fits the profile
    # One composition, one read: a second call is served from the per-snapshot cache.
    assert driver._checkpoint_bytes(ctx, stage) == len(CHECKPOINT)
    assert driver._checkpoint_bytes(ctx, {**stage, "input_snapshot_id": None}) is None


def test_a_checkpoint_over_the_profile_limit_fails_before_any_lease(tmp_path, adapted, monkeypatch):
    from types import SimpleNamespace

    world = _pinned_world(tmp_path)
    driver = world.driver
    real = driver.profile

    def limited(spec_stage):
        profile = real(spec_stage)
        return profile.model_copy(
            update={"stage_adapter": SimpleNamespace(max_input_bytes=1)}  # nothing fits
        )

    monkeypatch.setattr(driver, "profile", limited)
    from test_salad_interruption_retry import interrupted_with_manifest

    interrupted_with_manifest(world)
    world.tick_until(lambda: world.status() == "failed", limit=80)
    reason = world.row("SELECT stop_reason FROM campaigns")["stop_reason"]
    assert reason.startswith("stage_failed:g1:step_refused")
    second = world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=2")
    assert second is None or second["grant_id"] is None  # refused before any lease or hold
    dump = world.dump()
    assert "salad_checkpoint_over_cap" in dump and "'resume_state'" in dump
    assert "issue_grant" not in [
        r["step"] for r in world.env.query("SELECT step FROM campaign_step_intents WHERE attempt=2")
    ]
