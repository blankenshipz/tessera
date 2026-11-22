"""Salad campaign profiles: closed schema, private loader, deterministic derivations."""

import importlib.util
import json
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from tessera_control.salad_campaign_profiles import (
    PROFILES_SCHEMA,
    SaladCampaignProfile,
    SaladCampaignProfiles,
    capability_declaration,
    derive_selection,
    execution_profile,
    load_profiles,
    proxy_policy,
    target_policy,
)
from tessera_control.salad_workload_contract import WorkloadContractV1
from tessera_control.store import canonical_digest

SPEC = importlib.util.spec_from_file_location(
    "salad_live_setup", Path(__file__).parents[1] / "scripts/salad_live_setup.py"
)
setup_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_script)

IMAGE = "ghcr.io/example/tessera-gpu@sha256:" + "1" * 64
GPU = "550e8400-e29b-41d4-a716-446655440000"
OPERATION = "a" * 32
DATA = "d" * 64


def profile_data(**over):
    value = dict(
        name="gpu-main",
        org_id="org_1",
        project_id="project_1",
        worker_principal_id="principal_w",
        salad_organization="example-org",
        salad_project="tessera-research",
        country="US",
        gpu_class=GPU,
        cpu_millicores=1000,
        memory_mb=1024,
        disk_mb=1024,
        route_max_seconds=900,
        capability_environment_id="salad-gpu-campaign",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        custody_host="custody.example-tailnet.ts.net",
        credential_reference="credential-ref:salad-management",
        credential_version=1,
        allowed_image_digests=["sha256:" + "1" * 64],
        max_run_cents=500,
        max_task_cents=1000,
    )
    value.update(over)
    return value


def contract_data(**over):
    value = dict(
        contract="tessera.salad.workload.v1",
        argv_template=["train", "--op", "{operation}", "--epoch", "{epoch}", "{custody_host}"],
        env={"OMP_NUM_THREADS": "4"},
        workload_seconds=20,
        outputs=[dict(name="output.json", max_bytes=4096)],
        egress=dict(
            kind="provider_broad",
            fetches=[dict(url="https://models.example.com/w.bin", sha256="c" * 64, max_bytes=1000)],
        ),
        custody_host="custody.example-tailnet.ts.net",
    )
    value.update(over)
    return value


def make_file(tmp_path, document, mode=0o600):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document))
    path.chmod(mode)
    return path


def test_profile_schema_is_closed_and_bounded():
    assert SaladCampaignProfile.model_validate(profile_data()).name == "gpu-main"
    for bad in (
        dict(extra_field=1),
        dict(max_run_cents=5001),
        dict(max_run_cents=0),
        dict(max_task_cents=499),
        dict(route_max_seconds=7201),
        dict(cpu_millicores=1500),
        dict(country="us"),
        dict(gpu_class="not-a-uuid"),
        dict(custody_host="custody.example.com"),
        dict(credential_reference="plain-secret-value"),
        dict(name="Bad Name"),
    ):
        with pytest.raises(ValidationError):
            SaladCampaignProfile.model_validate(profile_data(**bad))


def test_profile_file_schema_limits_and_uniqueness():
    document = dict(schema=PROFILES_SCHEMA, profiles=[profile_data()])
    assert SaladCampaignProfiles.model_validate(document).get("gpu-main") is not None
    assert SaladCampaignProfiles.model_validate(document).get("other") is None
    for bad in (
        dict(schema="other", profiles=[profile_data()]),
        dict(schema=PROFILES_SCHEMA, profiles=[]),
        dict(schema=PROFILES_SCHEMA, profiles=[profile_data(), profile_data()]),
        dict(
            schema=PROFILES_SCHEMA,
            profiles=[profile_data(name=f"p{i}") for i in range(17)],
        ),
        dict(schema=PROFILES_SCHEMA, profiles=[profile_data()], extra=1),
    ):
        with pytest.raises(ValidationError):
            SaladCampaignProfiles.model_validate(bad)


def test_loader_requires_private_absolute_regular_file(tmp_path):
    tmp_path.chmod(0o700)
    document = dict(schema=PROFILES_SCHEMA, profiles=[profile_data()])
    good = make_file(tmp_path, document)
    assert load_profiles(good).get("gpu-main").salad_project == "tessera-research"
    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps(document))
    shared.chmod(0o644)
    link = tmp_path / "link.json"
    link.symlink_to(good)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    broken.chmod(0o600)
    for path in (shared, link, broken, Path("relative.json"), tmp_path / "missing.json"):
        with pytest.raises(ValueError, match="owner-only"):
            load_profiles(path)


def test_selection_is_deterministic_and_matches_the_setup_script():
    profile = SaladCampaignProfile.model_validate(profile_data())
    contract = WorkloadContractV1.model_validate(contract_data())
    arguments = dict(
        operation_id=OPERATION,
        epoch=3,
        snapshot_digest=DATA,
        image=IMAGE,
        max_cost_cents=200,
        plan_max_seconds=600,
    )
    first = derive_selection(profile, contract, **arguments)
    second = derive_selection(profile, contract, **arguments)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.salad.group == "attempt-" + OPERATION
    # Billed plan time plus the unbilled provisioning allowance, capped by the profile.
    assert first.resources.max_seconds == 900
    long = derive_selection(profile, contract, **{**arguments, "plan_max_seconds": 100})
    assert long.resources.max_seconds == min(900, 100 + 3600)

    args = types.SimpleNamespace(
        workload=contract,
        image=IMAGE,
        salad_org=profile.salad_organization,
        salad_project=profile.salad_project,
        gpu_class_id=profile.gpu_class,
        country=profile.country,
        cpu_millicores=profile.cpu_millicores,
        memory_mb=profile.memory_mb,
        disk_mb=profile.disk_mb,
        max_seconds=profile.route_max_seconds,
        max_cost_cents=200,
        artifact_destination=profile.artifact_destination,
        checkpoint_destination=profile.checkpoint_destination,
        monitor_profile=profile.monitor_profile,
        watchdog_profile=profile.watchdog_profile,
    )
    declaration = capability_declaration(profile, contract, IMAGE)
    digest = canonical_digest(declaration)
    expected = setup_script._selection(
        args,
        setup_script._execution_profile(args, digest, OPERATION, "3"),
        operation_id=OPERATION,
        snapshot_digest=DATA,
        derived=dict(
            target_digest=canonical_digest(
                dict(
                    target="salad_container",
                    organization=args.salad_org,
                    project=args.salad_project,
                    gpu_class=args.gpu_class_id,
                    country=args.country,
                    image=IMAGE,
                )
            ),
            code_digest=canonical_digest(contract.model_dump(mode="json")),
            model_digest=canonical_digest({"model": None, "workload": "contract"}),
            environment_digest=canonical_digest(
                dict(platform="linux/amd64", env=dict(contract.env), image=IMAGE)
            ),
        ),
    )
    assert first.model_dump(mode="json") == expected.model_dump(mode="json")
    assert execution_profile(profile, contract, digest, OPERATION, 3, 200) == (
        setup_script._execution_profile(args, digest, OPERATION, "3")
    )


def test_policies_are_built_from_the_profile_and_selection_only():
    profile = SaladCampaignProfile.model_validate(profile_data())
    contract = WorkloadContractV1.model_validate(contract_data())
    selection = derive_selection(
        profile,
        contract,
        operation_id=OPERATION,
        epoch=1,
        snapshot_digest=DATA,
        image=IMAGE,
        max_cost_cents=100,
        plan_max_seconds=300,
    )
    assert target_policy(selection).selection == selection
    policy = proxy_policy(profile, selection)
    assert policy.org_id == "org_1" and policy.project_id == "project_1"
    assert policy.management.reference == "credential-ref:salad-management"
    assert policy.target.selection == selection
