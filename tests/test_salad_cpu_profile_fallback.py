"""Operator CPU capacity fallback (``fallback_profiles``): the profile file and the order.

Pure model checks: the field is optional and closed, the whole file is validated at load
(dangling names, cycles, another kind, another tenant or workload), and the operator chain only
ever extends the approved order without changing any spec or its digest. No provider call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tessera_control.campaign_models import SaladStageSpec
from tessera_control.salad_alternate_profiles import (
    next_profile,
    operator_chain,
    profile_order,
    stage_on_operator_profile,
)
from tessera_control.salad_campaign_profiles import (
    MAX_FALLBACK_PROFILES,
    SaladCampaignProfile,
    SaladCampaignProfiles,
)
from tessera_control.store import canonical_digest

RATE = dict(vcpu_hour_usd="0.04", memory_gb_hour_usd="0.01")
SCHEMA = "tessera.salad.campaign-profiles.v1"


def cpu(name: str = "cpu-main", **over):
    value = dict(
        name=name,
        org_id="org-1",
        project_id="project-1",
        worker_principal_id="worker-1",
        salad_organization="example-org",
        salad_project="tessera-research",
        country="US",
        cpu_rate=RATE,
        cpu_millicores=2000,
        memory_mb=4096,
        disk_mb=1024,
        route_max_seconds=900,
        capability_environment_id="salad-cpu-campaign",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        custody_host="custody.example-tailnet.ts.net",
        credential_reference="credential-ref:salad-management",
        credential_version=1,
        allowed_image_digests=["sha256:" + "1" * 64],
        max_run_cents=100,
        max_task_cents=100,
    )
    value.update(over)
    return value


def load(*documents):
    return SaladCampaignProfiles.model_validate(dict(schema=SCHEMA, profiles=list(documents)))


# -- the field and the file ----------


def test_absent_field_keeps_every_existing_document_and_its_dump_unchanged():
    data = cpu()
    profile = SaladCampaignProfile.model_validate(data)
    assert profile.fallback_profiles is None
    dumped = profile.model_dump(mode="json")
    assert "fallback_profiles" not in dumped
    # The same document with the field explicitly absent digests exactly as before.
    assert canonical_digest(dumped) == canonical_digest(
        SaladCampaignProfile.model_validate(dict(data)).model_dump(mode="json")
    )
    assert load(data).fallback_chain("cpu-main") == ()


def test_a_chain_loads_and_resolves_depth_first_in_order():
    profiles = load(
        cpu(fallback_profiles=["cpu-b", "cpu-c"]),
        cpu("cpu-b", country="CA", fallback_profiles=["cpu-d"]),
        cpu("cpu-c", country="DE"),
        cpu("cpu-d", country="GB"),
    )
    assert profiles.fallback_chain("cpu-main") == ("cpu-b", "cpu-d", "cpu-c")
    assert profiles.fallback_chain("cpu-b") == ("cpu-d",)
    assert profiles.get("cpu-main").model_dump(mode="json")["fallback_profiles"] == [
        "cpu-b",
        "cpu-c",
    ]


@pytest.mark.parametrize(
    "names",
    [
        ["cpu-main"],  # itself
        ["cpu-b", "cpu-b"],  # duplicates
        [],  # empty (absent is the default)
        [f"cpu-{i}" for i in range(MAX_FALLBACK_PROFILES + 1)],  # more than 3
        ["Not-A-Key"],
    ],
)
def test_a_profile_refuses_a_malformed_list(names):
    with pytest.raises(ValidationError):
        SaladCampaignProfile.model_validate(cpu(fallback_profiles=names))


def test_null_and_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        SaladCampaignProfile.model_validate(cpu(fallback_profile=["cpu-b"]))


def test_a_gpu_profile_cannot_list_fallbacks():
    gpu = cpu(cpu_rate=None, gpu_class="550e8400-e29b-41d4-a716-446655440000")
    SaladCampaignProfile.model_validate(gpu)
    with pytest.raises(ValidationError, match="CPU-only"):
        SaladCampaignProfile.model_validate(dict(gpu, fallback_profiles=["cpu-b"]))


def test_the_file_refuses_a_dangling_name():
    with pytest.raises(ValidationError, match="unknown profile"):
        load(cpu(fallback_profiles=["cpu-b"]))


@pytest.mark.parametrize(
    "documents",
    [
        [cpu(fallback_profiles=["cpu-b"]), cpu("cpu-b", fallback_profiles=["cpu-main"])],
        [
            cpu(fallback_profiles=["cpu-b"]),
            cpu("cpu-b", fallback_profiles=["cpu-c"]),
            cpu("cpu-c", fallback_profiles=["cpu-b"]),
        ],
    ],
)
def test_the_file_refuses_a_cycle(documents):
    with pytest.raises(ValidationError, match="cycle"):
        load(*documents)


def test_a_diamond_is_not_a_cycle():
    profiles = load(
        cpu(fallback_profiles=["cpu-b", "cpu-c"]),
        cpu("cpu-b", fallback_profiles=["cpu-c"]),
        cpu("cpu-c"),
    )
    assert profiles.fallback_chain("cpu-main") == ("cpu-b", "cpu-c")


def test_the_file_refuses_a_fallback_of_another_kind():
    gpu = cpu("gpu-b", cpu_rate=None, gpu_class="550e8400-e29b-41d4-a716-446655440000")
    with pytest.raises(ValidationError, match="another kind"):
        load(cpu(fallback_profiles=["gpu-b"]), gpu)


@pytest.mark.parametrize(
    "change",
    [
        {"org_id": "org-2"},
        {"project_id": "project-2"},
        {"worker_principal_id": "worker-2"},
        {"salad_organization": "other-org"},
        {"custody_host": "other.example-tailnet.ts.net"},
        {"watchdog_profile": "c" * 64},
        {"monitor_profile": "c" * 64},
        {"capability_environment_id": "other-env"},
        {"artifact_destination": "artifact-ref:other"},
    ],
)
def test_the_file_refuses_a_fallback_with_another_tenant_worker_or_workload(change):
    with pytest.raises(ValidationError, match="differs"):
        load(cpu(fallback_profiles=["cpu-b"]), cpu("cpu-b", **change))


def test_a_fallback_may_differ_in_country_shape_rates_and_ceilings():
    load(
        cpu(fallback_profiles=["cpu-b"]),
        cpu(
            "cpu-b",
            country="CA",
            cpu_millicores=4000,
            memory_mb=8192,
            cpu_rate=dict(vcpu_hour_usd="0.05", memory_gb_hour_usd="0.02"),
            max_run_cents=50,
            max_task_cents=60,
            route_max_seconds=1200,
            salad_project="other-project",
        ),
    )


# -- the order: the approved spec first, then the operator chain ----------


class Config:
    def __init__(self, profile, alternates=None):
        self.profile = profile
        self.alternate_profiles = alternates


def test_operator_chain_follows_the_approved_order_and_skips_listed_profiles():
    profiles = load(
        cpu(fallback_profiles=["cpu-b", "cpu-c"]),
        cpu("cpu-b"),
        cpu("cpu-c", fallback_profiles=["cpu-d"]),
        cpu("cpu-d"),
    )
    assert operator_chain(Config("cpu-main"), profiles) == ("cpu-b", "cpu-c", "cpu-d")
    # A profile the spec already lists keeps its approved place, never repeated.
    assert operator_chain(Config("cpu-main", ["cpu-b"]), profiles) == ("cpu-c", "cpu-d")
    assert operator_chain(Config("cpu-main"), None) == ()
    # The approved selector never sees operator names: the spec order is unchanged.
    assert profile_order(Config("cpu-main", ["cpu-b"])) == ("cpu-main", "cpu-b")
    assert next_profile(Config("cpu-main"), ("cpu-main",)) is None


def test_the_spec_model_has_no_operator_fallback_field():
    assert "fallback_profiles" not in SaladStageSpec.model_fields


def test_stage_on_operator_profile_substitutes_only_the_profile():
    from salad_campaign_fixture import salad_stage

    from tessera_control.campaign_models import CampaignSpec

    class FakeEnv:
        owner = {"org_id": "org-1"}
        project = {"id": "project-1"}
        worker = {"id": "worker-1"}
        snapshot_id = "snapshot-1"
        snapshot_digest = "a" * 64

    stage = salad_stage(FakeEnv(), "g1")
    stage["salad"] = {**stage["salad"], "alternate_profiles": ["gpu-b"]}
    spec = CampaignSpec.model_validate(
        dict(
            version=1,
            name="x",
            budget_cap_cents=50,
            deadline_seconds=86400,
            worker_principal_id="worker-1",
            stages=[stage],
        )
    )
    approved = spec.stages[0]
    before = canonical_digest(spec.model_dump(mode="json"))
    moved = stage_on_operator_profile(approved, "cpu-x")
    assert moved.salad.profile == "cpu-x" and moved.salad.alternate_profiles is None
    assert moved.max_exposure_cents == approved.max_exposure_cents
    assert moved.retry == approved.retry and moved.image == approved.image
    assert moved.salad.workload == approved.salad.workload
    # The approved spec itself is untouched.
    assert canonical_digest(spec.model_dump(mode="json")) == before
    # A listed profile still takes the approved substitution.
    assert stage_on_operator_profile(approved, "gpu-b").salad.alternate_profiles == ["gpu-main"]
