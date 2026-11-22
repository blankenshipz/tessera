"""Stage adapter input derivation, defaults and exact refusal hints (ADR 0077). Fakes only."""
# ruff: noqa: F811

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_salad_stage_adapter import (  # noqa: F401
    NEAR,
    cpu_world,
    near,  # noqa: F401
    spec_of,
    stage,
)

from tessera_control.campaign_models import SaladProgramTemplate, SaladStageSpec, parse_spec
from tessera_control.campaign_salad_adapter import default_program, expand_spec
from tessera_control.snapshot_composition import composed_inventory
from tessera_control.store import Conflict, canonical_digest
from tessera_control.workspace import inventory_digest

SNAP_FILES = [("a.txt", "a" * 64, 10), ("d/b.txt", "b" * 64, 20)]
ART_FILES = [("out.json", "c" * 64, 5)]
ART = {
    "kind": "artifact",
    "name": "art",
    "campaign_id": "camp-1",
    "stage": "s",
    "output": "o",
    "sha256": "5" * 64,
}


def fake_entries(files=None, art=None):
    def entries(source):
        return list(art if source.kind == "artifact" else (files or SNAP_FILES))

    return entries


def expanded(world, entries, **over):
    return expand_spec(parse_spec(spec_of(world, **over)), world.profiles, entries, tenant=None)


def config(spec):
    (item,) = spec.stages[0].alternate_providers
    return SaladStageSpec.model_validate(item.provider_config)


def test_several_inputs_expand_with_the_composed_digest_and_exact_size(tmp_path, near):
    world = cpu_world(tmp_path, near)
    first = stage(world.env)["inputs"][0]
    inputs = [first, {**first, "name": "second"}, ART]
    work = config(expanded(world, fake_entries(art=ART_FILES), inputs=inputs)).workload
    rows = [
        (f"{n}/{p}", sha, size, "imported_unverified")
        for n in ("code", "second")
        for p, sha, size in SNAP_FILES
    ]
    rows += [("art/out.json", "c" * 64, 5, "imported_unverified")]
    assert work.input.snapshot_digest == inventory_digest(composed_inventory(rows))
    assert work.input.max_bytes == 2 * 30 + 5


def test_a_single_snapshot_keeps_its_digest_and_submit_sizes_it_from_the_record(tmp_path, near):
    world = cpu_world(tmp_path, near)
    env = world.env
    submitted = env.submit(spec=spec_of(world))
    (item,) = submitted["spec"]["stages"][0]["alternate_providers"]
    work = SaladStageSpec.model_validate(item["provider_config"]).workload
    manifest = json.loads(
        env.query("SELECT manifest_json FROM source_snapshots")[0]["manifest_json"]
    )
    assert work.input.snapshot_digest == env.snapshot_digest
    assert work.input.max_bytes == max(1, sum(e["size"] for e in manifest["entries"]))


def test_an_empty_stage_adapter_section_uses_the_default_program(tmp_path, near):
    world = cpu_world(tmp_path, near, adapter=False, stage_adapter={})
    spec = expanded(world, fake_entries())
    stage_model = parse_spec(spec_of(world)).stages[0]
    expected = SaladProgramTemplate.model_validate(default_program(stage_model))
    assert config(spec).program == expected
    assert config(spec).workload.input.max_bytes == 30


def test_inputs_over_the_profile_disk_are_refused(tmp_path, near):
    world = cpu_world(tmp_path, near)
    big = [("big.bin", "a" * 64, 2 << 30)]
    with pytest.raises(Conflict, match="disk_mb 8192|disk_mb"):
        expanded(world, fake_entries(files=big))


def test_inputs_over_the_stage_adapter_ceiling_are_refused(tmp_path, near):
    world = cpu_world(tmp_path, near, adapter=False, stage_adapter={"max_input_bytes": 10})
    with pytest.raises(Conflict, match="max_input_bytes 10"):
        expanded(world, fake_entries())


def test_an_image_outside_the_allowlist_names_the_exact_entry_and_profile(tmp_path, near):
    world = cpu_world(tmp_path, near)
    with pytest.raises(Conflict) as refused:
        expanded(world, fake_entries(), image="ghcr.io/example/other@sha256:" + "9" * 64)
    text = str(refused.value)
    assert '"sha256:' + "9" * 64 + '"' in text and "'cpu-main'" in text


def test_the_provider_check_names_the_digest_to_allowlist(tmp_path):
    world = SaladWorld(tmp_path)
    env = world.env
    other = salad_stage(env, "g1", image="ghcr.io/example/other@sha256:" + "2" * 64)
    body = {"spec": salad_spec(env, [other])}
    with pytest.raises(Conflict, match="not allowlisted") as refused:
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(body), body)
    assert '"sha256:' + "2" * 64 + '"' in str(refused.value) and "gpu-main" in str(refused.value)


def test_an_untrusted_salad_worker_gets_the_entry_to_merge_into_the_trust_file(tmp_path, near):
    from campaign_fixture import record_snapshot_private
    from provider_trust_fixture import DERIVED, install_trust

    world = cpu_world(tmp_path, near, slots=0)
    env = world.env
    record_snapshot_private(env.state, env.snapshot_id, "derived")
    install_trust({NEAR: (DERIVED, [near.principal["id"]])})
    (source,) = stage(env)["inputs"]
    body = {"spec": spec_of(world, inputs=[{**source, "data_class": "private_derived"}])}
    with pytest.raises(Conflict) as refused:
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(body), body)
    text = str(refused.value)
    assert '"salad"' in text and world.worker["id"] in text and "private_derived" in text
    assert "merge" in text and "do not replace" in text
    assert "all_registered_providers" not in text


def test_validate_returns_the_expanded_spec(tmp_path, near):
    world = cpu_world(tmp_path, near)
    env = world.env
    checked = env.store.validate_campaign_spec(
        env.owner, env.project["id"], {"spec": spec_of(world)}
    )
    (item,) = checked["expanded_spec"]["stages"][0]["alternate_providers"]
    assert item["provider_config"]["adapted"] is True
