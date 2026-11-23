"""A stage with a ``public_artifact`` input on the Salad path (ADR 0097). Fakes only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)

from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_receipts import public_fetch_entry
from tessera_control.campaign_runner import _Ctx
from tessera_control.campaign_salad import stage_data_digest
from tessera_control.campaign_salad_adapter import input_plan
from tessera_control.store import Conflict

URL = "https://models.example.com/w.bin"  # the fixture contract's one fetch row
SHA = "c" * 64
SIZE = 1000
SNAPSHOT = "d" * 64


def public_source():
    return {
        "kind": "public_artifact",
        "name": "weights",
        "files": [{"name": "w", "url": URL, "sha256": SHA, "max_bytes": SIZE}],
    }


def profile(max_input_bytes=10_000):
    return SimpleNamespace(
        stage_adapter=SimpleNamespace(max_input_bytes=max_input_bytes), disk_mb=1024, name="p"
    )


def stage(*inputs):
    return SimpleNamespace(key="g1", inputs=list(inputs))


def snapshot_item():
    return SimpleNamespace(kind="snapshot", name="code", digest=SNAPSHOT)


def public_item():
    return SimpleNamespace(kind="public_artifact", name="weights")


def test_a_mixed_stage_plans_its_snapshot_and_ignores_the_public_input():
    assert input_plan(stage(snapshot_item(), public_item()), profile(), None) == (SNAPSHOT, 10_000)
    assert input_plan(stage(snapshot_item()), profile(), None) == (SNAPSHOT, 10_000)


def test_a_public_only_stage_has_no_input_plan():
    assert input_plan(stage(public_item()), profile(), None) is None


def test_an_unknown_input_kind_is_still_refused_beside_a_public_input():
    other = SimpleNamespace(kind="stage_output", name="up")
    with pytest.raises(Conflict, match="cannot be carried to Salad"):
        input_plan(stage(other, public_item()), profile(), None)


def test_the_data_digest_is_the_snapshot_digest_or_binds_the_pins():
    spec_stage = SimpleNamespace(
        inputs=[
            SimpleNamespace(
                kind="public_artifact", name="weights", public_files=(("w", URL, SHA, SIZE),)
            )
        ]
    )
    assert stage_data_digest({"input_snapshot_digest": SNAPSHOT}, spec_stage) == SNAPSHOT
    first = stage_data_digest({"input_snapshot_digest": None}, spec_stage)
    assert len(first) == 64 and first != "None"
    spec_stage.inputs[0].public_files = (("w", URL, "e" * 64, SIZE),)
    assert stage_data_digest({"input_snapshot_digest": None}, spec_stage) != first


def public_only_world(tmp_path):
    world = SaladWorld(tmp_path)
    item = salad_stage_v2(world.env, inputs=[public_source()])
    world.spec = salad_spec_v2(world.env, [item])
    return world


def spec_stage_of(world, submitted):
    row = world.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0]
    campaign = dict(row)
    spec = normalize(parse_spec_json(campaign["spec_json"]))
    return campaign, spec


def test_review_digests_preview_and_bind_the_pins(tmp_path):
    world = public_only_world(tmp_path)
    campaign, spec = spec_stage_of(world, world.env.submit(spec=world.spec))
    digests = world.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1")
    assert all(len(v) == 64 for k, v in digests.items() if k.endswith("_digest"))
    # Another pin is another plan scope: the pins are bound by what the reviewer digests.
    other = salad_stage_v2(world.env, inputs=[public_source() | {"name": "other"}])
    world.spec = salad_spec_v2(world.env, [other])
    campaign, spec = spec_stage_of(world, world.env.submit(spec=world.spec))
    changed = world.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1")
    assert changed["plan_digest"] != digests["plan_digest"]


def test_a_public_only_stage_registers_its_study_with_the_pin_digest(tmp_path):
    world = public_only_world(tmp_path)
    world.start()
    world.tick_until(lambda: world.row("SELECT 1 FROM campaign_salad_stages") is not None)
    world.tick_until(
        lambda: world.row("SELECT 1 FROM campaign_salad_stages WHERE source_id IS NOT NULL")
    )
    world.assess_source()
    world.tick_until(
        lambda: world.row("SELECT 1 FROM campaign_salad_stages WHERE split_lock_id IS NOT NULL")
    )
    row = world.row("SELECT * FROM campaign_stages WHERE campaign_id=?", world.campaign_id)
    assert row["input_snapshot_id"] is None and row["input_data_class"] == "public"
    assert row["input_snapshot_digest"] and row["input_snapshot_digest"] != "None"
    assert world.row("SELECT 1 FROM snapshot_releases WHERE status='active'") is None


@pytest.mark.xfail(
    reason="the runner's reserved phase refuses a stage with no snapshot before the Salad grant",
    strict=True,
)
def test_a_public_only_stage_reaches_the_route_without_delivering_a_snapshot(tmp_path):
    world = public_only_world(tmp_path)
    world.to_route_started()
    assert world.route_state()["state"] == "queued"
    assert world.row("SELECT 1 FROM snapshot_releases WHERE status='active'") is None


def test_the_pins_are_declared_only_in_the_receipt_entry(tmp_path):
    world = public_only_world(tmp_path)
    _campaign, spec = spec_stage_of(world, world.env.submit(spec=world.spec))
    entry = public_fetch_entry("salad", spec.stage("g1"), None)
    assert entry["provider"] == "salad"
    assert SHA in repr(entry) and "declared_only" in repr(entry)
