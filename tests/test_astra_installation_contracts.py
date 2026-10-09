"""Unit tests for the pure, offline Astra installation contract slice."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tessera_provisioner.astra_installation import (
    AstraInstallationError,
    CredentialReference,
    InstallationFinding,
    InstallationIdentity,
    InstallationInspection,
    InstallationPlan,
    ManagerConflict,
    ObservedInstallation,
    OwnershipConflict,
    TargetIdentity,
    inspection_from_payload,
    plan_from_payload,
)

SPEC = "a" * 64
CHANGED_SPEC = "b" * 64


def _identity() -> InstallationIdentity:
    return InstallationIdentity("astra-control", "astra-phase1")


def _target() -> TargetIdentity:
    return TargetIdentity(
        kubeconfig_reference_sha256="c" * 64,
        context="named-target",
        server="https://cluster.example.test:6443",
        ca_sha256="d" * 64,
        cluster_uid="cluster-uid",
        namespace="astra-system",
        namespace_uid="namespace-uid",
    )


def _inspection(observed: bool = False) -> InstallationInspection:
    identity = _identity()
    target = _target()
    observation = (
        ObservedInstallation(identity, target, "installation-uid", SPEC, "2026-09-18T01:00:00Z")
        if observed
        else None
    )
    return InstallationInspection(identity, target, observation)


def test_explicit_modes_produce_deterministic_digest_bound_plans() -> None:
    absent = _inspection()
    observed = _inspection(observed=True)
    references = (
        CredentialReference("external-reference", "zeta-ref"),
        CredentialReference("credential-lease", "alpha-lease"),
    )

    create = InstallationPlan.create(
        mode="create",
        inspection=absent,
        desired_spec_sha256=SPEC,
        credential_references=references,
        approval_expires_at="2026-09-18T02:00:00+00:00",
    )
    observe = InstallationPlan.create(
        mode="observe",
        inspection=observed,
        desired_spec_sha256=None,
        approval_expires_at="2026-09-18T02:00:00Z",
    )
    adopt = InstallationPlan.create(
        mode="adopt",
        inspection=observed,
        desired_spec_sha256=SPEC,
        approval_expires_at="2026-09-18T02:00:00Z",
    )
    adapt = InstallationPlan.create(
        mode="adapt",
        inspection=observed,
        desired_spec_sha256=CHANGED_SPEC,
        approval_expires_at="2026-09-18T02:00:00Z",
    )

    assert [plan.mode for plan in (create, observe, adopt, adapt)] == [
        "create",
        "observe",
        "adopt",
        "adapt",
    ]
    assert create == InstallationPlan.create(
        mode="create",
        inspection=absent,
        desired_spec_sha256=SPEC,
        credential_references=tuple(reversed(references)),
        approval_expires_at="2026-09-18T02:00:00Z",
    )
    assert [item.reference for item in create.credential_references] == ["alpha-lease", "zeta-ref"]
    assert create.approval_expires_at == "2026-09-18T02:00:00Z"
    assert create.is_approval_expired(datetime(2026, 9, 18, 2, 0, tzinfo=UTC))
    assert not create.is_approval_expired(datetime(2026, 9, 18, 1, 59, tzinfo=UTC))


def test_observation_binds_installation_target_uid_and_spec_digest() -> None:
    inspection = _inspection(observed=True)

    assert inspection.state == "observed"
    assert inspection.observation is not None
    assert inspection.observation.uid == "installation-uid"
    assert inspection.observation.spec_sha256 == SPEC
    assert inspection.payload()["target"]["cluster_uid"] == "cluster-uid"  # type: ignore[index]

    with pytest.raises(AstraInstallationError, match="match the installation inspection"):
        InstallationInspection(
            _identity(),
            _target(),
            ObservedInstallation(
                InstallationIdentity("other-install", "astra-phase1"),
                _target(),
                "installation-uid",
                SPEC,
                "2026-09-18T01:00:00Z",
            ),
        )


def test_conflicts_fail_closed_for_mutating_modes_but_remain_observable() -> None:
    identity = _identity()
    target = _target()
    conflict = OwnershipConflict(identity, target, "foreign-uid", "other-operator")
    conflicted = InstallationInspection(identity, target, None, (conflict,))

    observe = InstallationPlan.create(
        mode="observe",
        inspection=conflicted,
        desired_spec_sha256=None,
        approval_expires_at="2026-09-18T02:00:00Z",
    )
    assert conflicted.state == "ownership_conflict"
    assert observe.inspection.ownership_conflicts == (conflict,)
    with pytest.raises(AstraInstallationError, match="ownership conflicts"):
        InstallationPlan.create(
            mode="create",
            inspection=conflicted,
            desired_spec_sha256=SPEC,
            approval_expires_at="2026-09-18T02:00:00Z",
        )


def test_ownership_conflicts_are_resource_scoped_not_collapsed_by_owner() -> None:
    identity, target = _identity(), _target()
    conflicts = (
        OwnershipConflict(identity, target, "uid-one", "other-operator", "deployment/astra"),
        OwnershipConflict(identity, target, "uid-two", "other-operator", "serviceaccount/astra"),
    )
    inspection = InstallationInspection(identity, target, None, ownership_conflicts=conflicts)

    assert [item.resource_identity for item in inspection.ownership_conflicts] == [
        "deployment/astra",
        "serviceaccount/astra",
    ]


def test_untyped_observation_or_ownership_conflict_fails_with_contract_error() -> None:
    with pytest.raises(AstraInstallationError, match="observation must be typed"):
        InstallationInspection(_identity(), _target(), "untrusted")  # type: ignore[arg-type]
    with pytest.raises(AstraInstallationError, match="typed and immutable"):
        InstallationInspection(_identity(), _target(), None, ("untrusted",))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mode", "inspection", "desired", "message"),
    [
        ("create", _inspection(observed=True), SPEC, "absent"),
        ("adopt", _inspection(), SPEC, "observed"),
        ("adopt", _inspection(observed=True), CHANGED_SPEC, "digests to match"),
        ("observe", _inspection(), SPEC, "cannot request a spec"),
    ],
)
def test_mode_preconditions_fail_closed(
    mode: str, inspection: InstallationInspection, desired: str | None, message: str
) -> None:
    with pytest.raises(AstraInstallationError, match=message):
        InstallationPlan.create(
            mode=mode,  # type: ignore[arg-type]
            inspection=inspection,
            desired_spec_sha256=desired,
            approval_expires_at="2026-09-18T02:00:00Z",
        )


def test_credential_references_are_opaque_and_plan_tampering_is_detected() -> None:
    with pytest.raises(AstraInstallationError, match="opaque identifier"):
        CredentialReference("credential-lease", "sk-live-secret-value")

    plan = InstallationPlan.create(
        mode="create",
        inspection=_inspection(),
        desired_spec_sha256=SPEC,
        credential_references=(CredentialReference("credential-lease", "lease-one"),),
        approval_expires_at="2026-09-18T02:00:00Z",
    )
    assert "secret" not in str(plan.payload())
    with pytest.raises(AstraInstallationError, match="does not match"):
        replace(
            plan,
            credential_references=(CredentialReference("credential-lease", "lease-two"),),
        ).assert_current()


def test_target_rejects_ambient_or_credential_bearing_server_values() -> None:
    values = {
        "kubeconfig_reference_sha256": "c" * 64,
        "context": "named-target",
        "ca_sha256": "d" * 64,
        "cluster_uid": "cluster-uid",
        "namespace": "astra-system",
        "namespace_uid": "namespace-uid",
    }
    with pytest.raises(AstraInstallationError, match="HTTPS endpoint"):
        TargetIdentity(server="http://cluster.example.test", **values)
    with pytest.raises(AstraInstallationError, match="without credentials"):
        TargetIdentity(server="https://token@cluster.example.test", **values)


def test_manager_and_incompatible_findings_block_mutation_but_not_observation() -> None:
    identity, target = _identity(), _target()
    manager = ManagerConflict(identity, target, "installation-uid", "foreign-manager")
    finding = InstallationFinding("permission", "incompatible", "astra-runtime")
    inspection = InstallationInspection(
        identity, target, None, manager_conflicts=(manager,), findings=(finding,)
    )

    assert inspection.mutation_blockers == ("manager_conflict", "incompatible_resource")
    assert InstallationPlan.create(
        mode="observe",
        inspection=inspection,
        desired_spec_sha256=None,
        approval_expires_at="2026-09-18T02:00:00Z",
    ).inspection.findings == (finding,)
    with pytest.raises(AstraInstallationError, match="manager"):
        InstallationPlan.create(
            mode="create",
            inspection=inspection,
            desired_spec_sha256=SPEC,
            approval_expires_at="2026-09-18T02:00:00Z",
        )


def test_serialized_plans_reject_live_snapshots_and_changed_targets() -> None:
    plan = InstallationPlan.create(
        mode="create",
        inspection=_inspection(),
        desired_spec_sha256=SPEC,
        approval_expires_at="2026-09-18T02:00:00Z",
    )
    restored = plan_from_payload({**plan.payload(), "digest": plan.digest})
    assert restored == plan
    with pytest.raises(AstraInstallationError, match="target identity changed"):
        plan.assert_target_unchanged(
            TargetIdentity(
                "c" * 64,
                "other-target",
                "https://cluster.example.test:6443",
                "d" * 64,
                "cluster-uid",
                "astra-system",
                "namespace-uid",
            )
        )
    snapshot = _inspection().payload()
    snapshot["verification"] = "live_verified"
    with pytest.raises(AstraInstallationError, match="cannot claim live"):
        inspection_from_payload(snapshot)
