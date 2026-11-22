"""CPU-only Salad profiles (ADR 0062): profile, route, transport wire, rate, placement, cap.

No live Salad call: the transport runs against the recorded-response factory of
``test_salad_http`` and the provider against the campaign fixture's fakes.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from pydantic import ValidationError
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_salad_campaign_profiles import (
    DATA,
    IMAGE,
    OPERATION,
    contract_data,
    profile_data,
)
from test_salad_http import (
    ID,
    Factory,
    group,
    selection,
)

from tessera_control import campaign_providers as providers
from tessera_control.campaign_models import CampaignSpec
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.remote_execution_models import CpuRate, ExecutionSelection
from tessera_control.salad_campaign_profiles import (
    SaladCampaignProfile,
    derive_selection,
    derived_digests,
    execution_profile,
)
from tessera_control.salad_http import SaladHTTPTransport
from tessera_control.salad_secrets import SecretBuffer
from tessera_control.salad_workload_contract import WorkloadContractV1
from tessera_control.store import Conflict, canonical_digest

RATE = dict(vcpu_hour_usd="0.04", memory_gb_hour_usd="0.01")
EXPENSIVE = CpuRate(vcpu_hour_usd="9.99", memory_gb_hour_usd="9.99")


def cpu_profile_data(**over):
    return profile_data(
        name="cpu-main",
        gpu_class=None,
        cpu_rate=RATE,
        cpu_millicores=2000,
        memory_mb=2048,
        capability_environment_id="salad-cpu-campaign",
        **over,
    )


def derive(profile, **over):
    contract = WorkloadContractV1.model_validate(contract_data())
    values = dict(
        operation_id=OPERATION,
        epoch=1,
        snapshot_digest=DATA,
        image=IMAGE,
        max_cost_cents=50,
        plan_max_seconds=600,
    )
    return derive_selection(profile, contract, **(values | over))


# -- the profile ---------------------------------------------------------------------------


def test_a_cpu_profile_names_rates_not_a_gpu_class():
    profile = SaladCampaignProfile.model_validate(cpu_profile_data())
    assert profile.gpu is False and profile.gpu_class is None
    assert profile.cpu_rate == CpuRate(**RATE)
    # 2 vCPU x $0.04 + 2 GB x $0.01 = $0.10/h: 600 s cost 1.666... cents.
    assert profile.estimated_cents(600) == Decimal("0.10") * 600 * 100 / 3600
    assert SaladCampaignProfile.model_validate(profile_data()).gpu is True


@pytest.mark.parametrize(
    "change",
    [
        {"gpu_class": "550e8400-e29b-41d4-a716-446655440000"},  # both a GPU and rates
        {"cpu_rate": None},  # neither
        {"cpu_rate": {"vcpu_hour_usd": "0", "memory_gb_hour_usd": "0.01"}},  # a free vCPU
        {"cpu_rate": {"vcpu_hour_usd": "free", "memory_gb_hour_usd": "0.01"}},
        {"cpu_rate": {"vcpu_hour_usd": "0.04"}},  # missing memory rate
        {"cpu_rate": dict(RATE, extra="x")},
    ],
)
def test_a_profile_is_exactly_gpu_or_cpu_with_valid_rates(change):
    with pytest.raises(ValidationError):
        SaladCampaignProfile.model_validate(cpu_profile_data() | change)


def test_a_gpu_profile_cannot_carry_cpu_rates():
    with pytest.raises(ValidationError):
        SaladCampaignProfile.model_validate(profile_data(cpu_rate=RATE))


def test_a_gpu_profile_has_no_static_rate_to_estimate_from():
    with pytest.raises(ValueError, match="static rate"):
        SaladCampaignProfile.model_validate(profile_data()).estimated_cents(60)


# -- the route -----------------------------------------------------------------------------


def test_a_cpu_route_has_no_gpu_fields_and_carries_its_reviewed_rates():
    profile = SaladCampaignProfile.model_validate(cpu_profile_data())
    chosen = derive(profile)
    assert (chosen.resources.gpu_count, chosen.resources.gpu_class) == (0, None)
    assert chosen.resources.cpu_millicores == 2000 and chosen.resources.memory_mb == 2048
    assert chosen.salad.execution.cpu_rate == CpuRate(**RATE)
    again = ExecutionSelection.model_validate_json(chosen.model_dump_json())
    assert again == chosen  # it round-trips and re-derives identically
    assert canonical_digest(derive(profile).model_dump(mode="json")) == canonical_digest(
        chosen.model_dump(mode="json")
    )


def test_a_gpu_routes_bytes_and_digests_are_unchanged_by_cpu_support():
    gpu = SaladCampaignProfile.model_validate(profile_data())
    chosen = derive(gpu)
    dumped = chosen.model_dump(mode="json")
    assert "cpu_rate" not in dumped["salad"]["execution"]  # historical bytes
    assert (chosen.resources.gpu_count, chosen.resources.gpu_class) == (1, gpu.gpu_class)
    contract = WorkloadContractV1.model_validate(contract_data())
    # The target digest of a GPU route is exactly the pre-existing five-field dictionary.
    expected = canonical_digest(
        dict(
            target="salad_container",
            organization=gpu.salad_organization,
            project=gpu.salad_project,
            gpu_class=gpu.gpu_class,
            country=gpu.country,
            image=IMAGE,
        )
    )
    assert derived_digests(gpu, contract, IMAGE)["target_digest"] == expected
    cpu = SaladCampaignProfile.model_validate(cpu_profile_data())
    assert derived_digests(cpu, contract, IMAGE)["target_digest"] != expected


def test_cpu_rates_are_refused_on_a_route_that_requests_a_gpu():
    chosen = derive(SaladCampaignProfile.model_validate(profile_data()))
    data = chosen.model_dump(mode="json")
    data["salad"]["execution"]["cpu_rate"] = RATE
    with pytest.raises(ValidationError, match="CPU-only"):
        ExecutionSelection.model_validate(data)


def test_a_stored_cpu_route_without_rates_still_parses_for_stop_and_status():
    data = derive(SaladCampaignProfile.model_validate(cpu_profile_data())).model_dump(mode="json")
    del data["salad"]["execution"]["cpu_rate"]
    assert ExecutionSelection.model_validate(data).resources.gpu_count == 0


def test_the_cpu_execution_profile_is_otherwise_the_generic_workload_profile():
    profile = SaladCampaignProfile.model_validate(cpu_profile_data())
    contract = WorkloadContractV1.model_validate(contract_data())
    execution = execution_profile(profile, contract, "c" * 64, OPERATION, 1, 50)
    assert execution["cpu_rate"] == RATE and execution["argv_binding"] == "workload_v1"
    assert execution["credit_exposure"] == "unknown_blocks_new_work_credit_not_hard_cap"


# -- the transport (recorded responses, no network) ---------------------------------------------


def cpu_selection(**over):
    chosen = selection().model_dump(mode="json")
    chosen["resources"] |= dict(gpu_class=None, gpu_count=0)
    chosen["salad"]["execution"]["cpu_rate"] = RATE
    for key, value in over.items():
        chosen["resources"][key] = value
    return ExecutionSelection.model_validate(chosen)


def run_cpu(factory, operation, provider_id=None, chosen=None):
    """Drive one transport operation; ``gpu_class`` and ``preflight`` are methods of their own."""
    chosen = chosen or cpu_selection()
    secret = SecretBuffer(bytearray(b"isolated-secret-marker"))
    with secret:
        transport = SaladHTTPTransport(chosen, connection_factory=factory)
        if operation in {"gpu_class", "preflight"}:
            return getattr(transport, operation)(chosen, secret)
        return transport.send(operation, chosen, provider_id, secret)


def cpu_group():
    value = group()
    value["container"]["resources"] = dict(cpu=2, memory=2048, storage_amount=2147483648)
    value["container"]["resources"]["gpu_classes"] = []
    return value


def test_the_cpu_create_requests_no_gpu_class():
    import json

    factory = Factory((201, cpu_group()))
    result = run_cpu(factory, "create_stopped")
    assert result.status in {"pending", "observed"}
    ((method, path, body, _),) = factory.calls
    assert method == "POST" and path.endswith("/containers")
    resources = json.loads(body)["container"]["resources"]
    assert resources["gpu_classes"] == []  # no GPU field of any kind is rendered
    assert resources["cpu"] == 2 and resources["memory"] == 2048


def test_a_cpu_group_is_matched_whether_salad_returns_an_empty_gpu_list_or_none():
    for gpu_classes in ([], None):
        data = cpu_group()
        if gpu_classes is None:
            del data["container"]["resources"]["gpu_classes"]
        result = run_cpu(Factory((200, data)), "read_group", ID)
        assert result.status == "observed", gpu_classes
    # A different shape is still an identity mismatch.
    data = cpu_group()
    data["container"]["resources"]["cpu"] = 4
    assert run_cpu(Factory((200, data)), "read_group", ID).status != "observed"


def test_the_cpu_rate_read_uses_the_operator_rates_and_makes_no_provider_call():
    factory = Factory()
    result = run_cpu(factory, "gpu_class")
    assert factory.calls == []  # there is no provider read for a CPU price
    observed = result.observation
    # 2 vCPU x 0.04 + (2048 MB / 1024) x 0.01 = 0.10 USD per hour
    assert Decimal(observed["hourly_price_usd"]) == Decimal("0.10")
    assert observed["price_basis"] == "operator_cpu_rate_not_actual_cost"
    assert observed["gpu_class_id"] is None


def test_the_cpu_preflight_skips_the_gpu_availability_read():
    group_list = (200, dict(items=[]))
    quotas = (
        200,
        dict(container_groups_quotas=dict(container_replicas_quota=10, container_replicas_used=0)),
    )
    factory = Factory(group_list, quotas)  # exactly two provider reads, no availability POST
    result = run_cpu(factory, "preflight")
    assert result.status == "observed", result
    assert [call[0] for call in factory.calls] == ["GET", "GET"]
    assert not any("availability" in call[1] for call in factory.calls)
    assert result.observation["available"] is True and result.observation["quota_ok"] is True
    assert result.observation["price_basis"] == "operator_cpu_rate_not_actual_cost"


def test_the_transport_refuses_a_cpu_selection_without_rates_or_with_a_gpu_class():
    chosen = cpu_selection()
    no_rates = chosen.model_dump(mode="json")
    del no_rates["salad"]["execution"]["cpu_rate"]
    with pytest.raises(ValueError, match="CPU rates"):
        SaladHTTPTransport(ExecutionSelection.model_validate(no_rates))
    # The GPU path keeps requiring exactly one valid class.
    gpu = selection().model_dump(mode="json")
    gpu["resources"]["gpu_class"] = "not-a-uuid"
    with pytest.raises(ValueError):
        SaladHTTPTransport(ExecutionSelection.model_validate(gpu))


# -- the guardian's rate check -----------------------------------------------------------------


def _guardian_rate(chosen, observation):
    from types import SimpleNamespace

    from tessera_control import salad_safety_service as service
    from tessera_control.remote_execution_models import RemoteProposal

    proposal = RemoteProposal.model_validate(
        dict(
            operation_id=OPERATION,
            package_digest="d" * 64,
            attempt_id="attempt_1",
            plan_digest="e" * 64,
            lease_id="lease_1",
            epoch=1,
            selection=chosen.model_dump(mode="json"),
            study_id="study_1",
            program_review_id="review_1",
            source_review_id="review_2",
        )
    )
    row = {"proposal": proposal.model_dump_json()}
    fake = SimpleNamespace(
        _send=lambda r, action: SimpleNamespace(status="observed", observation=observation),
        clock=lambda: 100.0,
    )
    return service.SaladSafetyService._rate_once(fake, row)


def _observation(chosen, **over):
    from tessera_control.store import canonical_digest as digest

    value = dict(
        selection_digest=digest(chosen.model_dump(mode="json")),
        gpu_class_id=None,
        hourly_price_usd="0.10",
        price_basis="operator_cpu_rate_not_actual_cost",
        observed_at="1970-01-01T00:01:40Z",
    )
    return value | over


def test_the_guardian_accepts_the_operator_cpu_rate_and_refuses_anything_else():
    chosen = cpu_selection()
    price, _ = _guardian_rate(chosen, _observation(chosen))
    assert price == Decimal("0.10")
    for bad in (
        dict(price_basis="published_gpu_hourly_rate_not_actual_cost"),  # a GPU basis on a CPU route
        dict(gpu_class_id="550e8400-e29b-41d4-a716-446655440000"),
        dict(hourly_price_usd="0"),
        dict(selection_digest="f" * 64),
    ):
        with pytest.raises(ValueError, match="rate_observation_invalid"):
            _guardian_rate(chosen, _observation(chosen, **bad))


def test_the_guardian_still_refuses_a_cpu_basis_on_a_gpu_route():
    chosen = ExecutionSelection.model_validate(selection().model_dump(mode="json"))
    with pytest.raises(ValueError, match="rate_observation_invalid"):
        _guardian_rate(chosen, _observation(chosen, gpu_class_id=chosen.resources.gpu_class))


# -- placement and the per-run cap at admission ---------------------------------------------------


@pytest.fixture
def cpu_world(tmp_path):
    built = SaladWorld(tmp_path, name="gpu-main", gpu_class=None, cpu_rate=RATE)
    register_provider_instance(built.profiles)
    return built


def stage_of(world):
    return CampaignSpec.model_validate(world.spec).stages[0]


def connection(world):
    db = sqlite3.connect(world.env.state / "tessera.sqlite3")
    db.row_factory = sqlite3.Row
    return db


def declare(stage, **salad):
    return stage.model_copy(update={"salad": stage.salad.model_copy(update=salad)})


def test_a_stage_that_declares_no_gpu_runs_on_a_cpu_profile_and_not_on_a_gpu_one(cpu_world):
    provider = providers.get_provider("salad")
    db = connection(cpu_world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        provider.check_stage(db, project, declare(stage_of(cpu_world), gpu=False))
        with pytest.raises(Conflict, match="needs a GPU profile"):  # the default is a GPU stage
            provider.check_stage(db, project, stage_of(cpu_world))
    finally:
        db.close()


def test_a_gpu_profile_refuses_a_stage_that_declares_no_gpu(tmp_path):
    world = SaladWorld(tmp_path)
    register_provider_instance(world.profiles)
    provider = providers.get_provider("salad")
    db = connection(world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        provider.check_stage(db, project, stage_of(world))
        with pytest.raises(Conflict, match="CPU-only profile"):
            provider.check_stage(db, project, declare(stage_of(world), gpu=False))
    finally:
        db.close()


def test_the_per_run_cap_is_enforced_at_admission_from_the_cpu_rates(cpu_world):
    provider = providers.get_provider("salad")
    db = connection(cpu_world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        stage = declare(stage_of(cpu_world), gpu=False)
        # 1 vCPU, 1 GB at the test rates for 3000 s is well under the stage's 20 cents hold.
        provider.check_stage(db, project, stage)
        # The same duration at rates that cost far more than the stage's hold is refused.
        from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

        costly = SaladCampaignProfiles.model_validate(
            dict(
                schema="tessera.salad.campaign-profiles.v1",
                profiles=[
                    cpu_world.profiles.profiles[0]
                    .model_copy(
                        update=dict(
                            cpu_rate=CpuRate(vcpu_hour_usd="9.99", memory_gb_hour_usd="9.99")
                        )
                    )
                    .model_dump(mode="json")
                ],
            )
        )
        register_provider_instance(costly)
        with pytest.raises(Conflict, match="estimated CPU cost"):
            providers.get_provider("salad").check_stage(db, project, stage)
    finally:
        db.close()


def test_the_stage_gpu_flag_is_absent_from_the_spec_unless_it_is_false(cpu_world):
    gpu_stage = stage_of(cpu_world)
    assert "gpu" not in gpu_stage.salad.model_dump(mode="json")  # every existing digest holds
    assert declare(gpu_stage, gpu=False).salad.model_dump(mode="json")["gpu"] is False


# -- the approval path applies the same placement check as submit --------------------------------


def _private(path):
    """A fresh private (0700) directory for a world's state."""
    path.mkdir(mode=0o700)
    return path


def _submit(world, spec):
    body = {"spec": spec}
    return world.env.store.submit_campaign(
        world.env.owner,
        world.env.project["id"],
        "submit-mismatch",
        canonical_digest(body),
        body,
    )


def _approve(world, submitted):
    body = {
        "expected_revision": submitted["revision"],
        "expected_spec_digest": submitted["spec_digest"],
    }
    return world.env.store.approve_campaign(
        world.env.human(), submitted["id"], "approve-mismatch", canonical_digest(body), body
    )


def _cpu_profiles(world):
    from salad_campaign_fixture import profiles

    return profiles(world.env, name="gpu-main", gpu_class=None, cpu_rate=RATE)


def _gpu_profiles(world):
    from salad_campaign_fixture import profiles

    return profiles(world.env, name="gpu-main")


def _declaring_no_gpu(spec):
    import copy

    changed = copy.deepcopy(spec)
    changed["stages"][0]["salad"]["gpu"] = False
    return changed


def test_submit_refuses_a_gpu_flag_that_does_not_match_the_profile_kind(tmp_path):
    world = SaladWorld(tmp_path)  # a GPU profile
    with pytest.raises(Conflict, match="CPU-only profile"):
        _submit(world, _declaring_no_gpu(world.spec))
    cpu_world = SaladWorld(_private(tmp_path / "cpu"), gpu_class=None, cpu_rate=RATE)
    with pytest.raises(Conflict, match="needs a GPU profile"):
        _submit(cpu_world, cpu_world.spec)  # a default (GPU) stage on a CPU profile


def test_approval_refuses_the_same_mismatch_when_the_operators_profiles_changed(tmp_path):
    """Approval re-checks the stage against the profiles in force, exactly as submit does."""
    world = SaladWorld(tmp_path)
    status, submitted = _submit(world, world.spec)
    assert status == 201
    # The operator replaces the profile file between submit and approval: the same profile name
    # is now CPU-only, so the stored GPU stage no longer fits and approval is refused with 409.
    register_provider_instance(_cpu_profiles(world))
    with pytest.raises(Conflict, match="needs a GPU profile"):
        _approve(world, submitted)
    # The mirror case: a stage that declared no GPU, accepted on a CPU profile, is refused once
    # the profile is a GPU one again.
    cpu_world = SaladWorld(_private(tmp_path / "cpu"), gpu_class=None, cpu_rate=RATE)
    status, submitted = _submit(cpu_world, _declaring_no_gpu(cpu_world.spec))
    assert status == 201
    register_provider_instance(_gpu_profiles(cpu_world))
    with pytest.raises(Conflict, match="CPU-only profile"):
        _approve(cpu_world, submitted)


def test_an_unchanged_matching_stage_is_approved(tmp_path):
    world = SaladWorld(_private(tmp_path / "ok"), gpu_class=None, cpu_rate=RATE)
    status, submitted = _submit(world, _declaring_no_gpu(world.spec))
    assert status == 201
    status, approved = _approve(world, submitted)
    assert status == 200 and approved["id"] == submitted["id"]
