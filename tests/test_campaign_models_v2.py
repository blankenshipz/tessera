"""Campaign spec v2 and normalization (ADR 0040 section 1); pure model tests."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError
from test_campaign_models import IMAGE, SNAP, _spec, _stage

from tessera_control.artifact_limits import operator_limits
from tessera_control.campaign_models import (
    CampaignSpec,
    CampaignSpecV2,
    normalize,
    parse_spec,
    spec_digest,
    stage_classes,
    stage_execution_spec,
    topological_order,
)

# Pinned: v1 digests must never change, or every existing approval would be invalidated.
V1_DIGEST = "8d91a8c1958d7f6ceb62bf55bfc4dfea165c8ddd030f85050fa4010864d6c745"


def _out(name="result", path="out.json", **extra) -> dict:
    return {"name": name, "path": path, "max_bytes": 1024, **extra}


def _stage2(key: str, *inputs: dict, **extra) -> dict:
    return {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [_out()],
        "max_duration_seconds": 60,
        "inputs": list(inputs) or [SNAP],
        **extra,
    }


def _spec2(*stages: dict, **extra) -> dict:
    return {
        "version": 2,
        "name": "c",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": list(stages) or [_stage2("a")],
        **extra,
    }


def _from(name, stage, *outputs):
    return {"kind": "stage", "name": name, "stage": stage, "outputs": list(outputs) or ["result"]}


def _invalid(spec: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match or None):
        parse_spec(spec)


def test_v1_digest_is_unchanged():
    assert spec_digest(CampaignSpec.model_validate(_spec(_stage("a")))) == V1_DIGEST
    assert spec_digest(parse_spec(_spec(_stage("a")))) == V1_DIGEST


def test_v1_normalizes_to_provider_and_runtime():
    normal = normalize(parse_spec(_spec(_stage("a"), _stage("b"))))
    assert [(s.provider, s.runtime) for s in normal.stages] == [
        ("local-docker", "docker-cpu-v1")
    ] * 2
    stage = normal.stage("a")
    assert stage.worker_principal_id == "worker-1" and stage.output_paths == ["out.json"]
    assert stage.max_output_bytes == 1024 and normal.version == 1


def test_v1_stage_sources_normalize_to_paths_and_keep_their_class():
    spec = parse_spec(
        _spec(
            _stage("a"),
            _stage("b", {"kind": "stage", "name": "up", "stage": "a", "paths": ["out.json"]}),
        )
    )
    stage = normalize(spec).stage("b")
    assert stage.inputs[0].kind == "stage" and stage.inputs[0].paths == ("out.json",)
    assert stage_classes(spec)["b"][0] == "private_derived"
    assert topological_order(spec) == ["a", "b"]


def test_v2_validates_and_resolves_output_names_to_paths():
    spec = parse_spec(_spec2(_stage2("a"), _stage2("b", _from("up", "a"))))
    assert isinstance(spec, CampaignSpecV2)
    normal = normalize(spec)
    assert normal.stage("b").inputs[0].paths == ("out.json",)
    assert normal.stage("a").max_output_bytes == 1024
    assert stage_execution_spec(spec, "a", "d" * 64)["profile"] == "local-cpu"
    assert spec_digest(spec) != V1_DIGEST


def test_v2_artifact_source_shape_is_strict():
    ok = {
        "kind": "artifact",
        "name": "pinned",
        "campaign_id": "campaign_1",
        "stage": "a",
        "output": "result",
        "sha256": "c" * 64,
    }
    spec = parse_spec(_spec2(_stage2("a", ok)))
    assert normalize(spec).stage("a").inputs[0].kind == "artifact"
    assert stage_classes(spec)["a"][0] == "private_derived"
    for broken in (
        {**ok, "sha256": "short"},
        {**ok, "extra": 1},
        {k: v for k, v in ok.items() if k != "output"},
    ):
        _invalid(_spec2(_stage2("a", broken)), "")


def test_v2_refusals():
    _invalid(_spec2(_stage2("a", _from("up", "a"))), "own outputs")
    _invalid(_spec2(_stage2("a"), _stage2("b", _from("up", "a", "nope"))), "undeclared outputs")
    _invalid(_spec2(_stage2("a", provider="nowhere")), "Unknown compute provider")
    _invalid(_spec2(_stage2("a", runtime="other-v1")), "no runtime")
    _invalid(_spec2(_stage2("a", provider_config={"x": 1})), "takes no provider_config")
    _invalid(_spec2(_stage2("a", env={"A": "b"})), "env is not supported")
    _invalid(_spec2(_stage2("a"), forbid_providers=["local-docker"]), "forbidden")
    _invalid(_spec2(_stage2("a", outputs=[_out(), _out(path="o2.json")])), "unique")
    _invalid(_spec2(_stage2("a", outputs=[_out(max_bytes=2**40 + 1)])), "")
    limit = operator_limits().max_artifact_bytes
    _invalid(
        _spec2(_stage2("a", outputs=[_out(max_bytes=limit), _out("two", "o2")])),
        "exceed the provider",
    )


def test_v2_bounds():
    stages = [_stage2(f"s{i}") for i in range(65)]
    _invalid(_spec2(*stages), "")
    many = [_stage2(f"s{i}") for i in range(64)]
    ok = parse_spec(_spec2(*many))
    assert len(ok.stages) == 64
    too_many_edges = [_stage2("a")] + [
        _stage2(f"s{i}", *[_from(f"u{j}", "a") for j in range(8)]) for i in range(65)
    ]
    _invalid(_spec2(*too_many_edges), "")
    big = _stage2("a", argv=["x" * 4000] * 128)
    _invalid(_spec2(*[copy.deepcopy(big) | {"key": f"s{i}"} for i in range(3)]), "1 MiB")


def test_v2_send_classes_narrow_only_and_must_cover_inputs():
    spec = parse_spec(_spec2(_stage2("a", send_classes=["public"])))
    assert normalize(spec).stage("a").send_classes == ("public",)
    _invalid(_spec2(_stage2("a", send_classes=["secret"])), "")
    _invalid(_spec2(_stage2("a", send_classes=[])), "")
    _invalid(
        _spec2(_stage2("a"), _stage2("b", _from("up", "a"), send_classes=["public"])),
        "excludes private_derived",
    )


def test_a_v1_spec_is_not_accepted_as_version_2_and_back():
    v1 = _spec(_stage("a"))
    _invalid({**v1, "version": 2}, "")
    _invalid({**_spec2(), "version": 3}, "")
