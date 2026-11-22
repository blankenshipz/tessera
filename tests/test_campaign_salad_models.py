"""Salad stage spec validation (ADR 0038 amendment); pure model tests, no state."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from salad_campaign_fixture import contract_data, salad_spec, salad_stage

from tessera_control.campaign_models import (
    CampaignSpec,
    spec_digest,
    stage_execution_spec,
)
from tessera_control.store import canonical_digest

ENV = SimpleNamespace(snapshot_id="snapshot_1", snapshot_digest="b" * 64, worker={"id": "w1"})


def spec(**stage_over):
    return salad_spec(ENV, [salad_stage(ENV, "g1", **stage_over)])


def invalid(match, **stage_over):
    with pytest.raises(ValidationError, match=match):
        CampaignSpec.model_validate(spec(**stage_over))


def test_a_complete_salad_stage_validates_and_maps_to_a_kubernetes_plan():
    parsed = CampaignSpec.model_validate(spec())
    plan = stage_execution_spec(parsed, "g1", "c" * 64)
    assert plan["profile"] == "kubernetes-opencode" and plan["network_enabled"] is False
    assert plan["argv"][0] == "/tessera/bin/run" and plan["pinned_skill_digests"] == []
    assert plan["output_paths"] == ["output.json"] and plan["input_snapshot_digest"] == "c" * 64


def test_salad_block_is_required_for_and_only_for_the_salad_backend():
    stage = salad_stage(ENV, "g1")
    bare = {k: v for k, v in stage.items() if k != "salad"}
    with pytest.raises(ValidationError, match="salad is required"):
        CampaignSpec.model_validate(salad_spec(ENV, [bare]))
    cpu = {**bare, "backend": "docker-local-cpu-v1", "salad": stage["salad"]}
    cpu["argv"] = ["python", "-c", "1"]
    cpu["image"] = "python@sha256:" + "a" * 64
    with pytest.raises(ValidationError, match="salad is required"):
        CampaignSpec.model_validate(salad_spec(ENV, [cpu]))


@pytest.mark.parametrize(
    "change,match",
    [
        ({"env": {"A": "1"}}, "env comes from the workload contract"),
        (
            {
                "egress": {
                    "profile": "allowlist-proxy-v1",
                    "destinations": [{"host": "example.com", "port": 443}],
                    "max_egress_bytes": 10,
                }
            },
            "egress is supported only",
        ),
        ({"retry": {"max_attempts": 1, "transient_exit_codes": [3]}}, "no exit code"),
        ({"max_exposure_cents": 0}, "max_exposure_cents >= 1"),
        ({"argv": ["/tessera/bin/run", "other"]}, "argv must equal"),
        ({"output_paths": ["other.json"]}, "output_paths must equal"),
        ({"max_output_bytes": 100}, "max_output_bytes is below"),
        ({"max_duration_seconds": 10}, "max_duration_seconds is below"),
        ({"max_duration_seconds": 3601}, "max_duration_seconds <= 3600"),
    ],
)
def test_each_salad_refusal(change, match):
    invalid(match, **change)


def _no_hosts_salad():
    block = salad_stage(ENV)["salad"]
    return {
        **block,
        "workload": contract_data(egress=dict(kind="provider_broad", fetches=[])),
    }


def test_a_salad_stage_with_non_public_input_cannot_declare_hosts():
    # ADR 0040: input classes are not a spec-level requirement (provider trust decides at
    # submit and approve); a non-public input only forbids declared fetch or redirect hosts.
    private = {**salad_stage(ENV)["inputs"][0], "data_class": "private_derived"}
    invalid("non-public input cannot declare workload fetch or redirect hosts", inputs=[private])
    CampaignSpec.model_validate(spec(inputs=[private], salad=_no_hosts_salad()))


def test_a_stage_fed_by_private_output_may_be_salad_only_without_hosts():
    upstream = {
        "key": "u1",
        "backend": "docker-local-cpu-v1",
        "image": "python@sha256:" + "a" * 64,
        "argv": ["python", "-c", "1"],
        "output_paths": ["out.json"],
        "max_output_bytes": 100,
        "max_duration_seconds": 60,
        "inputs": salad_stage(ENV)["inputs"],
        "output_data_class": "private_derived",
    }
    inputs = [{"kind": "stage", "name": "up", "stage": "u1", "paths": ["out.json"]}]
    hosted = salad_stage(ENV, "g1", inputs=inputs)
    with pytest.raises(ValidationError, match="non-public input cannot declare"):
        CampaignSpec.model_validate(salad_spec(ENV, [upstream, hosted]))
    bare = salad_stage(ENV, "g1", inputs=inputs, salad=_no_hosts_salad())
    CampaignSpec.model_validate(salad_spec(ENV, [upstream, bare]))


def test_workload_contract_is_validated_by_its_own_model():
    bad = copy.deepcopy(salad_stage(ENV))
    bad["salad"]["workload"] = contract_data(env={"API_TOKEN": "x"})
    with pytest.raises(ValidationError, match="environment entry refused"):
        CampaignSpec.model_validate(salad_spec(ENV, [bad]))


def test_grant_ttl_and_unknown_fields_are_closed():
    for ttl in (3599, 43201):
        bad = copy.deepcopy(salad_stage(ENV))
        bad["salad"]["grant_ttl_seconds"] = ttl
        with pytest.raises(ValidationError):
            CampaignSpec.model_validate(salad_spec(ENV, [bad]))
    extra = copy.deepcopy(salad_stage(ENV))
    extra["salad"]["gpu_class"] = "someone-elses"
    with pytest.raises(ValidationError):
        CampaignSpec.model_validate(salad_spec(ENV, [extra]))


def test_cpu_spec_digests_do_not_change():
    cpu = {
        "key": "a",
        "backend": "docker-local-cpu-v1",
        "image": "python@sha256:" + "a" * 64,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["out.json"],
        "max_output_bytes": 1024,
        "max_duration_seconds": 60,
        "inputs": salad_stage(ENV)["inputs"],
    }
    parsed = CampaignSpec.model_validate(salad_spec(ENV, [cpu]))
    dumped = parsed.model_dump(mode="json")
    assert "salad" not in dumped["stages"][0]
    assert spec_digest(parsed) == canonical_digest(dumped)
