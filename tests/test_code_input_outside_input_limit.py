"""A stage's code input sits outside the per-stage data-input limit (ADR 0121).

A v2 stage holds at most 8 data inputs; when it names ``code_input`` (ADR 0095), that one pinned
snapshot input is not counted, so it may hold 9 inputs in all. Anything else past 8 is refused,
and every spec that was valid before keeps its digest. Nothing here runs a provider or a network.
"""

from __future__ import annotations

import json

import pytest
from campaign_fixture import Env
from salad_campaign_fixture import IMAGE, salad_provider_reset, salad_spec_v2  # noqa: F401
from test_salad_stage_adapter import cpu_world, near  # noqa: F401
from test_salad_stage_adapter import spec_of as adapter_spec_of

from tessera_control import campaign_models
from tessera_control.campaign_models import (
    MAX_STAGE_DATA_INPUTS,
    SaladStageSpec,
    normalize,
    parse_spec,
    parse_spec_json,
    spec_digest,
)
from tessera_control.campaign_salad_adapter import expand_spec
from tessera_control.snapshot_composition import ComposeSnapshot, composed_inventory
from tessera_control.store import canonical_digest
from tessera_control.workspace import inventory_digest

ARTIFACT = {
    "kind": "artifact",
    "name": "art",
    "campaign_id": "c1",
    "stage": "s0",
    "output": "out",
    "sha256": "a" * 64,
}


def snap(env, name):
    return {
        "kind": "snapshot",
        "name": name,
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }


def data_inputs(env, count):
    return [snap(env, f"data{index}") for index in range(count)]


def stage(env, inputs, **over):
    value = {
        "key": "s1",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "/inputs/code/main.py"],
        "max_duration_seconds": 120,
        "max_exposure_cents": 0,
        "inputs": inputs,
        "outputs": [{"name": "out", "path": "out.json", "max_bytes": 1024}],
    }
    value.update(over)
    return value


@pytest.fixture
def env(tmp_path):
    from tessera_control import campaign_provider_worker as worker

    env = Env(tmp_path)
    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    yield env
    worker.register_from_config(None)


def submit(env, spec):
    body = {"spec": spec}
    return env.store.submit_campaign(
        env.owner, env.project["id"], "k", canonical_digest(body), body
    )


def test_the_limit_is_eight_data_inputs():
    assert MAX_STAGE_DATA_INPUTS == 8


def test_eight_data_inputs_and_the_code_input_are_accepted_through_submit(env):
    inputs = [*data_inputs(env, 8), snap(env, "code")]
    spec = salad_spec_v2(env, [stage(env, inputs, code_input="code")])
    parsed = parse_spec(spec)
    normal = normalize(parsed).stages[0]
    assert len(normal.inputs) == 9
    assert normal.code_input == "code"
    status, body = submit(env, spec)
    assert status == 201
    assert len(body["spec"]["stages"][0]["inputs"]) == 9
    assert body["spec"]["stages"][0]["code_input"] == "code"


def test_the_code_input_may_be_any_position(env):
    inputs = [snap(env, "code"), *data_inputs(env, 8)]
    parsed = parse_spec(salad_spec_v2(env, [stage(env, inputs, code_input="code")]))
    assert len(parsed.stages[0].inputs) == 9


def test_eight_inputs_without_a_code_input_stay_valid(env):
    parsed = parse_spec(salad_spec_v2(env, [stage(env, data_inputs(env, 8))]))
    assert len(parsed.stages[0].inputs) == 8


def test_nine_inputs_without_a_code_input_are_refused(env):
    spec = salad_spec_v2(env, [stage(env, data_inputs(env, 9))])
    with pytest.raises(ValueError, match="more than 8 data inputs"):
        parse_spec(spec)


def test_nine_inputs_whose_code_input_names_a_missing_input_are_refused(env):
    spec = salad_spec_v2(env, [stage(env, data_inputs(env, 9), code_input="nope")])
    with pytest.raises(ValueError, match="more than 8 data inputs"):
        parse_spec(spec)


def test_nine_inputs_whose_code_input_names_an_artifact_input_are_refused(env):
    inputs = [*data_inputs(env, 8), ARTIFACT]
    spec = salad_spec_v2(env, [stage(env, inputs, code_input="art")])
    with pytest.raises(ValueError, match="more than 8 data inputs"):
        parse_spec(spec)


def test_nine_inputs_whose_code_input_names_a_stage_input_are_refused(env):
    upstream = {"kind": "stage", "name": "up", "stage": "s0", "outputs": ["out"]}
    spec = salad_spec_v2(
        env,
        [
            stage(env, [snap(env, "code")], key="s0"),
            stage(env, [*data_inputs(env, 8), upstream], code_input="up"),
        ],
    )
    with pytest.raises(ValueError, match="more than 8 data inputs"):
        parse_spec(spec)


def test_ten_inputs_are_refused_even_with_a_code_input(env):
    inputs = [*data_inputs(env, 9), snap(env, "code")]
    spec = salad_spec_v2(env, [stage(env, inputs, code_input="code")])
    with pytest.raises(ValueError):
        parse_spec(spec)


def test_the_code_input_is_not_a_data_edge(env, monkeypatch):
    # The spec-wide edge cap counts data inputs only; a code input past it is not refused.
    inputs = [*data_inputs(env, 8), snap(env, "code")]
    spec = salad_spec_v2(env, [stage(env, inputs, code_input="code")])
    monkeypatch.setattr(campaign_models, "MAX_INPUT_EDGES_V2", 8)
    assert len(parse_spec(spec).stages[0].inputs) == 9
    monkeypatch.setattr(campaign_models, "MAX_INPUT_EDGES_V2", 7)
    with pytest.raises(ValueError, match="more than 7 stage inputs in total"):
        parse_spec(spec)


# -- digests valid before ADR 0121 are unchanged ------------------------------------------------

_SNAP = {
    "kind": "snapshot",
    "snapshot_id": "snap_fixed",
    "digest": "b" * 64,
    "data_class": "public",
}


def _static_spec(count, **over):
    value = {
        "key": "s1",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "/inputs/code/main.py"],
        "max_duration_seconds": 120,
        "inputs": [{**_SNAP, "name": f"in{index}"} for index in range(count)],
        "outputs": [{"name": "out", "path": "out.json", "max_bytes": 1024}],
    }
    value.update(over)
    return {
        "version": 2,
        "name": "fixed",
        "budget_cap_cents": 50,
        "deadline_seconds": 86400,
        "stages": [value],
    }


# Digests computed with the contract before ADR 0121; the change must leave them byte-identical.
@pytest.mark.parametrize(
    ("count", "over", "expected"),
    [
        (1, {}, "cb9cc653aacd6d13e723f726a740a8ab34b99827399cc4eca44a1264edbd4ea7"),
        (8, {}, "b16209b896b214ce4fb1d6c6717d492745224cc30f122d1a6a85932ace204d4f"),
        (
            8,
            {"code_input": "in0"},
            "65cf5693ca1104926d8252608d8abdfd18ad05c67a4799174a287d742b14da9b",
        ),
    ],
)
def test_specs_valid_before_keep_their_digest(count, over, expected):
    spec = parse_spec_json(json.dumps(_static_spec(count, **over)))
    assert spec_digest(spec) == expected


# -- downstream: composition and the Salad stage adapter -----------------------------------------


def _source(index):
    return {
        "name": f"in{index}",
        "kind": "snapshot",
        "snapshot_id": "s",
        "expected_digest": "c" * 64,
    }


def test_composition_takes_eight_data_inputs_and_the_code_input():
    body = {"sources": [_source(i) for i in range(9)], "expected_snapshot_digest": "d" * 64}
    assert len(ComposeSnapshot.model_validate(body).sources) == 9
    body["sources"].append(_source(9))
    with pytest.raises(ValueError):
        ComposeSnapshot.model_validate(body)


def test_the_stage_adapter_composes_nine_inputs(tmp_path, near):  # noqa: F811
    world = cpu_world(tmp_path, near)
    files = [("a.txt", "a" * 64, 10)]
    inputs = [*data_inputs(world.env, 8), snap(world.env, "code")]
    spec = parse_spec(adapter_spec_of(world, inputs=inputs, code_input="code"))
    result = expand_spec(spec, world.profiles, lambda source: list(files), tenant=None)
    assert result.stages[0].code_input == "code"
    (item,) = result.stages[0].alternate_providers
    work = SaladStageSpec.model_validate(item.provider_config).workload
    rows = [(f"{source['name']}/a.txt", "a" * 64, 10, "imported_unverified") for source in inputs]
    assert work.input.snapshot_digest == inventory_digest(composed_inventory(rows))
    assert work.input.max_bytes == 90
