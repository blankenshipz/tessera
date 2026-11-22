"""Campaign spec validation (ADR 0038); pure model tests, no state."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from tessera_control.campaign_models import (
    CampaignSpec,
    needs_composition,
    spec_digest,
    stage_execution_spec,
    stage_input_class,
    stage_output_class,
    topological_order,
)

IMAGE = "python@sha256:" + "a" * 64
SNAP = {
    "kind": "snapshot",
    "name": "code",
    "snapshot_id": "snapshot_1",
    "digest": "b" * 64,
    "data_class": "public",
}


def _stage(key: str, *inputs: dict, **extra) -> dict:
    return {
        "key": key,
        "backend": "docker-local-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["out.json"],
        "max_output_bytes": 1024,
        "max_duration_seconds": 60,
        "inputs": list(inputs) or [SNAP],
        **extra,
    }


def _up(name: str, stage: str, *paths: str) -> dict:
    return {"kind": "stage", "name": name, "stage": stage, "paths": list(paths) or ["out.json"]}


def _spec(*stages: dict, **extra) -> dict:
    return {
        "version": 1,
        "name": "c",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "worker_principal_id": "worker-1",
        "stages": list(stages) or [_stage("a")],
        **extra,
    }


def _invalid(spec: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CampaignSpec.model_validate(spec)


def test_valid_dag_orders_topologically_with_ties_by_key():
    spec = CampaignSpec.model_validate(
        _spec(
            _stage("c", _up("up", "a"), SNAP),
            _stage("b", _up("up", "a"), SNAP),
            _stage("a"),
        )
    )
    assert topological_order(spec) == ["a", "b", "c"]
    assert needs_composition(spec.stages[0]) and not needs_composition(spec.stages[2])


def test_cycle_is_rejected():
    _invalid(
        _spec(_stage("a", _up("up", "b")), _stage("b", _up("up", "a"))),
        "cycle",
    )


def test_missing_and_self_references_are_rejected():
    _invalid(_spec(_stage("a", _up("up", "ghost"))), "unknown stage")
    _invalid(_spec(_stage("a", _up("up", "a"))), "own outputs")


def test_undeclared_input_path_is_rejected():
    _invalid(_spec(_stage("a"), _stage("b", _up("up", "a", "other.json"))), "other.json")


def test_duplicate_stage_keys_and_input_names_are_rejected():
    _invalid(_spec(_stage("a"), _stage("a")), "stage keys must be unique")
    _invalid(
        _spec(_stage("a", SNAP, {**SNAP, "snapshot_id": "snapshot_2"})),
        "input names must be unique",
    )


def test_hold_above_cap_is_rejected():
    _invalid(_spec(_stage("a", max_exposure_cents=11)), "hold exceeds")


def test_env_on_local_cpu_is_rejected():
    _invalid(_spec(_stage("a", env={"X": "1"})), "env is not supported")


def test_egress_requires_local_backend_and_public_inputs():
    egress = {
        "profile": "allowlist-proxy-v1",
        "destinations": [{"host": "example.com", "port": 443}],
        "max_egress_bytes": 1000,
    }
    CampaignSpec.model_validate(_spec(_stage("a", egress=egress)))
    _invalid(_spec(_stage("a", backend="salad-gpu-v1", egress=egress)), "egress is supported only")
    private = {**SNAP, "data_class": "private_derived"}
    _invalid(_spec(_stage("a", private, egress=egress)), "private-derived inputs cannot use egress")
    # Privacy propagates: a stage fed by a private-derived stage cannot use egress either.
    _invalid(
        _spec(_stage("a", private), _stage("b", _up("up", "a"), egress=egress)),
        "private-derived inputs cannot use egress",
    )


def test_metric_and_stop_rule_references():
    metric = {"name": "score", "path": "out.json", "json_pointer": "/score"}
    ok = CampaignSpec.model_validate(
        _spec(
            _stage(
                "a",
                metrics=[metric],
                stop_when=[{"metric": "score", "op": "ge", "value": 0.5}],
            )
        )
    )
    assert ok.stages[0].stop_when[0].op == "ge"
    _invalid(_spec(_stage("a", metrics=[{**metric, "path": "nope.json"}])), "path is not an output")
    _invalid(
        _spec(_stage("a", stop_when=[{"metric": "score", "op": "ge", "value": 1}])),
        "unknown metric",
    )


def test_unknown_fields_and_bad_types_are_rejected():
    _invalid({**_spec(), "surprise": 1}, "surprise|extra")
    _invalid(_spec(_stage("a", max_output_bytes=True)), "max_output_bytes")
    _invalid(_spec(budget_cap_cents=5001), "budget_cap_cents")
    _invalid(_spec(_stage("A!")), "key")
    _invalid(_spec(_stage("a", image="python:latest")), "image")


def test_data_class_propagation():
    private = {**SNAP, "data_class": "private_derived"}
    spec = CampaignSpec.model_validate(
        _spec(
            _stage("a"),
            _stage("b", _up("up", "a")),
            _stage("c", SNAP, _up("up", "a")) | {"inputs": [private, _up("up", "a")]},
        )
    )
    assert stage_input_class(spec, "a") == "public"
    # ADR 0039: every stage output is private_derived, whatever the stage declares.
    assert stage_output_class(spec, "a") == "private_derived"
    assert stage_input_class(spec, "b") == "private_derived"
    assert stage_input_class(spec, "c") == "private_derived"
    assert stage_output_class(spec, "c") == "private_derived"


def test_public_output_class_is_refused():
    _invalid(_spec(_stage("a", output_data_class="public")), "private_derived")


def test_execution_spec_derivation_and_digest_stability():
    spec = CampaignSpec.model_validate(_spec())
    execution = stage_execution_spec(spec, "a", "d" * 64)
    assert execution["profile"] == "local-cpu"
    assert execution["input_snapshot_digest"] == "d" * 64
    assert execution["network_enabled"] is False
    assert execution["output_paths"] == ["out.json"]
    reordered = copy.deepcopy(_spec())
    reordered["stages"][0] = dict(reversed(list(reordered["stages"][0].items())))
    reordered = dict(reversed(list(reordered.items())))
    assert spec_digest(CampaignSpec.model_validate(reordered)) == spec_digest(spec)
    changed = _spec(name="other")
    assert spec_digest(CampaignSpec.model_validate(changed)) != spec_digest(spec)


def _dense_stages(count: int) -> list[dict]:
    stages = [_stage("s0")]
    for index in range(1, count):
        previous = range(max(0, index - 8), index)
        inputs = [_up(f"i{n}", f"s{n}") for n in previous]
        stages.append(_stage(f"s{index}", *inputs))
    return stages


def test_dense_dag_class_validation_is_not_exponential():
    import time

    from pydantic import TypeAdapter

    from tessera_control.campaign_models import StageSpec, stage_classes

    start = time.monotonic()
    spec = CampaignSpec.model_validate(_spec(*_dense_stages(32), budget_cap_cents=10))
    assert time.monotonic() - start < 1.0
    assert stage_output_class(spec, "s31") == "private_derived"

    stages = [TypeAdapter(StageSpec).validate_python(item) for item in _dense_stages(64)]
    wide = CampaignSpec.model_construct(**{**_spec(), "stages": stages})
    start = time.monotonic()
    classes = stage_classes(wide)
    assert stage_input_class(wide, "s63") == "private_derived"
    assert time.monotonic() - start < 1.0
    assert len(classes) == 64


def test_dense_dag_private_class_propagates():
    stages = _dense_stages(20)
    stages[0] = _stage("s0", {**SNAP, "data_class": "private_derived"})
    spec = CampaignSpec.model_validate(_spec(*stages))
    assert stage_output_class(spec, "s19") == "private_derived"


def test_total_input_edges_are_capped(monkeypatch):
    from tessera_control import campaign_models

    monkeypatch.setattr(campaign_models, "MAX_INPUT_EDGES", 10)
    _invalid(_spec(*_dense_stages(10)), "stage inputs in total")


def test_stage_hold_across_attempts_must_fit_the_experiment_ceiling():
    stage = _stage("a", max_exposure_cents=1500, retry={"max_attempts": 4})
    _invalid(_spec(stage, budget_cap_cents=5000), "exceeds the experiment budget ceiling")
    ok = _stage("a", max_exposure_cents=1250, retry={"max_attempts": 4})
    CampaignSpec.model_validate(_spec(ok, budget_cap_cents=5000))
