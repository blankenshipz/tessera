"""The ``salad`` compute provider over the stage driver (ADR 0040). Fakes only."""

from __future__ import annotations

import sqlite3

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control import campaign_providers as providers
from tessera_control.campaign_delegation import STEP_TABLE
from tessera_control.campaign_models import CampaignSpec, SnapshotSource, StageSource
from tessera_control.campaign_provider_salad import (
    SaladRouteProvider,
    register_from_config,
    register_provider_instance,
)
from tessera_control.remote_execution_models import ExecutionSelection
from tessera_control.salad_campaign_profiles import (
    DATA_POLICY_PUBLIC,
    DATA_POLICY_TRUST,
    execution_profile,
    stage_data_policy,
    stage_has_nonpublic_input,
)
from tessera_control.store import Conflict


@pytest.fixture
def world(tmp_path):
    built = SaladWorld(tmp_path)
    # The spec models resolve the provider at validation, so register one with the
    # world's profiles for the test and restore the profile-less default afterwards.
    register_provider_instance(built.profiles)
    return built


@pytest.fixture
def provider(world):
    return providers.get_provider("salad")


def first_stage(world):
    return CampaignSpec.model_validate(world.spec).stages[0]


def connection(world):
    db = sqlite3.connect(world.env.state / "tessera.sqlite3")
    db.row_factory = sqlite3.Row
    return db


def with_stage_input(stage):
    source = StageSource(kind="stage", name="prior", stage="up", paths=["out.json"])
    return stage.model_copy(update={"inputs": [*stage.inputs, source]})


def test_descriptor_and_registry():
    d = providers.get_provider("salad").descriptor
    assert (d.kind, d.target, d.third_party, d.billable) == (
        "salad_route",
        "salad_container",
        True,
        True,
    )
    assert d.runtimes == frozenset({"salad-workload-v1"})
    assert d.delegable_steps <= frozenset(STEP_TABLE)
    assert (d.output_limits.max_files, d.output_limits.max_total_bytes) == (16, 16 << 20)
    # Without an operator file the provider exists and refuses every stage as not enabled.
    assert providers.get_provider("salad").profiles is None


def test_register_from_config_reads_the_profiles_document_and_replaces(world):
    document = world.profiles.model_dump(mode="json", by_alias=True)
    register_from_config({"salad_profiles": document})
    found = providers.get_provider("salad").profiles
    assert found is not None and [p.name for p in found.profiles] == [
        p.name for p in world.profiles.profiles
    ]
    register_from_config(None)
    assert providers.get_provider("salad").profiles is None
    with pytest.raises(ValueError):
        register_from_config({"salad_profiles": {"schema": "nope", "profiles": []}})


def test_a_foreign_provider_owning_the_id_is_not_replaced():
    providers.unregister_provider("salad")
    try:
        # Simulates another module holding the id before this one registered.
        import tessera_control.campaign_provider_salad as module

        saved = list(module._REGISTERED)
        module._REGISTERED.clear()
        providers.register_provider(SaladRouteProvider(None))
        with pytest.raises(ValueError, match="already registered"):
            register_from_config(None)
    finally:
        providers.unregister_provider("salad")
        module._REGISTERED[:] = saved
        register_provider_instance(None)


def test_validate_stage_accepts_the_fixture_stage(world, provider):
    provider.validate_stage(first_stage(world))


def test_non_public_input_with_workload_egress_is_refused(world, provider):
    stage = first_stage(world)
    # The fixture workload declares fetches; a stage output input is non-public.
    assert stage.salad.workload.egress.fetches or stage.salad.workload.egress.redirect_hosts
    with pytest.raises(ValueError, match="non-public input"):
        provider.validate_stage(with_stage_input(stage))
    private = SnapshotSource(
        kind="snapshot",
        name="d",
        snapshot_id="s",
        digest="a" * 64,
        data_class="private_derived",
    )
    with pytest.raises(ValueError, match="non-public input"):
        provider.validate_stage(stage.model_copy(update={"inputs": [private]}))


def test_non_public_input_without_egress_passes_validation(world, provider):
    stage = first_stage(world)
    egress = stage.salad.workload.egress.model_copy(update={"fetches": [], "redirect_hosts": []})
    workload = stage.salad.workload.model_copy(update={"egress": egress})
    salad = stage.salad.model_copy(update={"workload": workload})
    provider.validate_stage(with_stage_input(stage.model_copy(update={"salad": salad})))


def test_validate_stage_requires_salad_fields_and_exposure(world, provider):
    stage = first_stage(world)
    with pytest.raises(ValueError, match="salad is required"):
        provider.validate_stage(stage.model_copy(update={"salad": None}))
    with pytest.raises(ValueError, match="max_exposure_cents"):
        provider.validate_stage(stage.model_copy(update={"max_exposure_cents": 0}))


def test_check_stage_applies_the_profile_ceilings(world, provider):
    stage = first_stage(world)
    project = world.env.project_id if hasattr(world.env, "project_id") else None
    db = connection(world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        provider.check_stage(db, project, stage)
        with pytest.raises(Conflict, match="per-run ceiling"):
            provider.check_stage(
                db, project, stage.model_copy(update={"max_exposure_cents": 10**6})
            )
        with pytest.raises(Conflict, match="belong to this organization"):
            provider.check_stage(db, "other-project", stage)
        with pytest.raises(Conflict, match="not enabled"):
            SaladRouteProvider(None).check_stage(db, project, stage)
    finally:
        db.close()


def test_enabled_infrastructure_retries_count_against_the_billable_task_ceiling(world, provider):
    """A Salad stage's worst case is every attempt's hold, infrastructure retries included
    (ADR 0058): exposure equal to the per-task ceiling with three enabled retries is refused,
    while the same stage with the loop off, or a hold that fits all four attempts, passes."""
    from tessera_control.campaign_models import RetryPolicy

    stage = first_stage(world)
    db = connection(world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        ceiling = 100  # the fixture profile's max_task_cents (and its per-run ceiling)

        def with_retry(exposure, **retry):
            return stage.model_copy(
                update={
                    "max_exposure_cents": exposure,
                    "retry": RetryPolicy(max_attempts=1, **retry),
                }
            )

        provider.check_stage(db, project, with_retry(ceiling))  # one attempt: exactly the ceiling
        provider.check_stage(db, project, with_retry(ceiling, infra_retries=0))
        provider.check_stage(db, project, with_retry(ceiling // 4, infra_retries=3))  # 4 x 25
        with pytest.raises(Conflict, match="per-task ceiling"):
            provider.check_stage(db, project, with_retry(ceiling, infra_retries=3))
        with pytest.raises(Conflict, match="per-task ceiling"):
            provider.check_stage(db, project, with_retry(ceiling // 4 + 1, infra_retries=3))
    finally:
        db.close()


def _adopt_approved_code(world, tmp_path):
    from tessera_control.snapshots import SnapshotStore
    from tessera_control.workspace import capture_workspace, inventory_digest

    root = tmp_path / "staged"
    root.mkdir()
    (root / "main.py").write_bytes(b"x = 1\n")
    capture = capture_workspace(str(root))
    actor = dict(world.env.owner, automation_label="approved-code-test")
    _, adopted = SnapshotStore(world.env.state).adopt_snapshot(
        actor,
        world.env.project["id"],
        "adopt-code",
        inventory_digest(capture.inventory),
        capture,
        code_approval={"paths": ["main.py"]},
    )
    return adopted


def test_an_approved_code_snapshot_is_non_public_for_route_policy_and_fetch_hosts(
    world, provider, tmp_path
):
    stage = first_stage(world)
    adopted = _adopt_approved_code(world, tmp_path)
    driver = provider.driver(world.runner)
    row = {"operation_id": "op", "epoch": 1}
    plain = {"input_snapshot_digest": "d" * 64}
    assert driver._selection(None, plain, stage, row).salad.execution.data_policy == (
        "public_released_only"
    )
    approved = {"input_snapshot_digest": "d" * 64, "input_snapshot_id": adopted["id"]}
    selected = driver._selection(None, approved, stage, row)
    assert selected.salad.execution.data_policy == "provider_trust_policy_v1"
    moved = stage.model_copy(
        update={
            "inputs": [
                stage.inputs[0].model_copy(
                    update={"snapshot_id": adopted["id"], "digest": adopted["digest"]}
                )
            ]
        }
    )
    db = connection(world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        with pytest.raises(Conflict, match="fetch or redirect hosts"):
            provider.check_stage(db, project, moved)
    finally:
        db.close()


def test_static_input_classification(world):
    stage = first_stage(world)
    assert not stage_has_nonpublic_input(stage)
    assert stage_data_policy(stage) == DATA_POLICY_PUBLIC
    assert stage_has_nonpublic_input(with_stage_input(stage))
    assert stage_data_policy(with_stage_input(stage)) == DATA_POLICY_TRUST
    narrowed = stage.model_copy(update={})
    object.__setattr__(narrowed, "send_classes", ["public", "private_derived"])
    assert stage_has_nonpublic_input(narrowed)


def test_a_missing_or_unknown_input_class_counts_as_non_public(world, provider):
    from types import SimpleNamespace

    stage = first_stage(world)
    for source in (
        SimpleNamespace(kind="snapshot"),
        SimpleNamespace(kind="snapshot", data_class="mystery"),
    ):
        odd = SimpleNamespace(inputs=[source], send_classes=None)
        assert stage_has_nonpublic_input(odd)
        assert stage_data_policy(odd) == DATA_POLICY_TRUST
    fetching = stage.model_copy(update={"inputs": [SimpleNamespace(kind="snapshot")]})
    with pytest.raises(ValueError, match="fetch or redirect hosts"):
        provider.validate_stage(fetching)


def test_execution_profile_carries_the_route_data_policy(world):
    stage = first_stage(world)
    profile = world.profiles.profiles[0]
    args = (profile, stage.salad.workload, "c" * 64, "op", 1, 5)
    assert execution_profile(*args)["data_policy"] == "public_released_only"
    assert execution_profile(*args, data_policy=DATA_POLICY_TRUST)["data_policy"] == (
        "provider_trust_policy_v1"
    )
    with pytest.raises(ValueError):
        execution_profile(*args, data_policy="anything")


@pytest.mark.skipif(
    "provider_trust_policy_v1" not in str(ExecutionSelection.model_fields["salad"].annotation)
    and "provider_trust_policy_v1" not in repr(ExecutionSelection.model_json_schema()),
    reason="the data_policy literal gains provider_trust_policy_v1 with the trust change",
)
def test_private_input_selection_uses_the_trust_policy(world, provider):
    stage = with_stage_input(first_stage(world))
    selection = provider.driver(world.runner)._selection(
        None, {"input_snapshot_digest": "d" * 64}, stage, {"operation_id": "op", "epoch": 1}
    )
    assert selection.salad.execution.data_policy == "provider_trust_policy_v1"


def test_grant_request_and_execution_spec(world, provider):
    stage = first_stage(world)
    grant = provider.grant_request(None, None, stage)
    assert grant.worker_principal_id == stage.salad.worker_principal_id
    assert grant.ttl_seconds == stage.salad.grant_ttl_seconds
    spec = provider.execution_spec(stage, "d" * 64)
    assert spec["profile"] == "kubernetes-opencode" and spec["network_enabled"] is False
    assert spec["input_snapshot_digest"] == "d" * 64 and spec["argv"] == list(stage.argv)


def test_settle_waits_then_settles_at_the_charge_with_its_basis(world, provider):
    world.to_awaiting_charge()
    attempt = world.attempt()
    with pytest.raises(providers.Waiting):
        provider.settle(world.runner, attempt)
    world.record_charge(13)
    settled = provider.settle(world.runner, world.attempt())
    assert settled == providers.Settlement(13, "human_charge", True)


def test_unclaimed_attempt_settles_at_zero(world, provider):
    world.to_awaiting_charge()
    world.env.execute(
        "UPDATE campaign_salad_attempts SET phase='unclaimed' WHERE attempt_id=?",
        world.attempt()["id"],
    )
    assert provider.settle(world.runner, world.attempt()) == providers.Settlement(
        0, "unclaimed_zero", True
    )


def test_bounded_hold_settlement_is_provisional(tmp_path):
    from test_campaign_salad_settlement import CLEAN, run

    world = SaladWorld(tmp_path)
    run(world, CLEAN)
    world.tick_until(lambda: world.attempt()["salad_verdict"] is not None)
    # Control's own read of the guardian record is what lets the bounded hold settle.
    provider = SaladRouteProvider(world.profiles, guardian_reader=world.guardian)
    settled = provider.settle(world.runner, world.attempt())
    assert settled == providers.Settlement(HOLD, "guardian_bounded_hold", False)
    # Without a reader the same attempt does not settle on the bounded hold.
    blind = SaladRouteProvider(world.profiles)
    with pytest.raises(Exception):  # noqa: B017, PT011  (the runner's wait signal)
        blind.settle(world.runner, world.attempt())


def test_observe_maps_the_recorded_verdict(provider):
    base = {"salad_evidence_digest": "e" * 64, "manifest_id": "m1", "failure_detail": None}
    ok = provider.observe(None, {**base, "salad_verdict": "ok"})
    assert (ok.outcome, ok.evidence_kind, ok.manifest_id) == (
        "succeeded",
        "salad_live_receipt",
        "m1",
    )
    assert provider.observe(None, {**base, "salad_verdict": None}).outcome == "running"
    failed = provider.observe(None, {**base, "salad_verdict": "failed"})
    assert (failed.outcome, failed.failure_class) == ("failed_permanent", "salad_not_passed")


def test_exposure_open_follows_the_attempt_phase(world, provider):
    world.to_awaiting_charge()
    db = connection(world)
    try:
        assert provider.exposure_open(db, world.campaign_id)
        assert not provider.exposure_open(db, "no-such-campaign")
    finally:
        db.close()
