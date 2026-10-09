from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import pytest

from tessera_provisioner.outcome0_bootstrap import (
    ApprovedSource,
    BootstrapLimits,
    BootstrapPlan,
    CapabilityDeclaration,
    CustodyRecord,
    DeclaredArtifact,
    DeclaredPackage,
    FakeOutcome0BootstrapAdapter,
    LeasePackageBinding,
    Outcome0BootstrapError,
    Outcome0BootstrapStore,
    PinnedEnvironment,
)


@dataclass
class FakeCustody:
    records: list[CustodyRecord] = field(default_factory=list)

    def record(self, custody: CustodyRecord) -> None:
        assert custody.custody_state == "quarantined_unreleased"
        self.records.append(custody)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plan(content: bytes = b"fixture-data") -> tuple[BootstrapPlan, dict[str, bytes]]:
    uri = "fixture://outcome0-fixtures/molecular-distillation/train.json"
    declaration = CapabilityDeclaration(
        id="outcome0-capability",
        approval_id="approval-one",
        approved_by="human-one",
        sources=(
            ApprovedSource(
                "outcome0-fixtures", "fixture://outcome0-fixtures/molecular-distillation"
            ),
        ),
        system_packages=(DeclaredPackage("build-essential", "1", "1" * 64),),
        python_packages=(DeclaredPackage("rdkit", "2025.9", "2" * 64),),
        artifacts=(
            DeclaredArtifact(
                "training-data", "data", "outcome0-fixtures", uri, _digest(content), len(content)
            ),
        ),
    )
    return (
        BootstrapPlan.create(
            LeasePackageBinding("lease-one", "a" * 32, "3" * 64),
            declaration,
            PinnedEnvironment("registry.example/outcome0@sha256:" + "4" * 64, "5" * 64),
            BootstrapLimits(1024, 512, 2, 2, 2),
        ),
        {uri: content},
    )


def _approved_store() -> tuple[
    Outcome0BootstrapStore, FakeOutcome0BootstrapAdapter, FakeCustody, str
]:
    plan, fixtures = _plan()
    custody = FakeCustody()
    adapter = FakeOutcome0BootstrapAdapter(fixtures, custody)
    store = Outcome0BootstrapStore(adapter)
    requested = store.request(plan)
    approved = store.approve(requested.id, requested.revision)
    return store, adapter, custody, approved.id


def test_successful_offline_bootstrap_is_ready_and_has_quarantined_provenance() -> None:
    store, adapter, custody, operation_id = _approved_store()

    completed = store.reconcile(operation_id)
    readiness = store.readiness(operation_id)

    assert completed.desired_state == completed.observed_state == "ready"
    assert adapter.live_effects is False
    assert len(adapter.effects) == 4  # workspace, system, Python, and one local fixture artifact
    assert len(custody.records) == 1
    assert readiness["ready"] is True
    assert readiness["provider_enabled"] is False
    assert readiness["scientific_results"]["status"] == "not_collected"
    assert readiness["custody"] == [
        {
            "artifact_id": custody.records[0].artifact_id,
            "custody_state": "quarantined_unreleased",
            "sha256": custody.records[0].sha256,
            "size": custody.records[0].size,
        }
    ]
    assert readiness["manifest_digest"] == completed.evidence.manifest_digest  # type: ignore[union-attr]
    assert readiness["log_digest"] == completed.evidence.log_digest  # type: ignore[union-attr]


def test_partial_failure_retries_the_same_effect_identities_without_duplicates() -> None:
    store, adapter, custody, operation_id = _approved_store()
    adapter.fail_once_at = "python_package:rdkit"

    failed = store.reconcile(operation_id)
    assert failed.observed_state == "failed"
    assert [effect.kind for effect in adapter.effects] == ["workspace", "system_package"]

    retried = store.retry(operation_id, failed.revision)
    completed = store.reconcile(retried.id)
    assert completed.observed_state == "ready"
    assert len(adapter.effects) == 4
    assert len({effect.key for effect in adapter.effects}) == 4
    assert len(custody.records) == 1


def test_unavailable_or_corrupt_fixture_does_not_claim_a_completed_download() -> None:
    plan, fixtures = _plan()
    custody = FakeCustody()
    adapter = FakeOutcome0BootstrapAdapter(
        {"fixture://outcome0-fixtures/molecular-distillation/train.json": b"corrupt"}, custody
    )
    store = Outcome0BootstrapStore(adapter)
    requested = store.request(plan)
    approved = store.approve(requested.id, requested.revision)

    failed = store.reconcile(approved.id)
    assert failed.observed_state == "failed"
    assert [effect.kind for effect in adapter.effects] == [
        "workspace",
        "system_package",
        "python_package",
    ]
    assert custody.records == []

    adapter.fixtures = fixtures
    retried = store.retry(approved.id, failed.revision)
    completed = store.reconcile(retried.id)
    assert completed.observed_state == "ready"
    assert [effect.kind for effect in adapter.effects] == [
        "workspace",
        "system_package",
        "python_package",
        "artifact",
    ]


def test_lost_reply_is_unknown_until_reconciled_without_duplicate_artifact_custody() -> None:
    store, adapter, custody, operation_id = _approved_store()
    adapter.lose_next_reply = True

    unknown = store.reconcile(operation_id)
    assert unknown.observed_state == "unknown"
    assert store.readiness(operation_id)["ready"] is False
    assert len(adapter.effects) == 4
    assert len(custody.records) == 1

    retried = store.retry(operation_id, unknown.revision)
    completed = store.reconcile(retried.id)
    assert completed.observed_state == "ready"
    assert len(adapter.effects) == 4
    assert len(custody.records) == 1


def test_cancellation_wins_at_the_effect_boundary_and_never_reports_ready() -> None:
    store, adapter, _custody, operation_id = _approved_store()

    def cancel_at_boundary() -> None:
        adapter.before_effect = None
        current = store._operations[operation_id]  # test-only race injection
        store.cancel(operation_id, current.revision)

    adapter.before_effect = cancel_at_boundary
    cancelled = store.reconcile(operation_id)

    assert cancelled.desired_state == "cancel_requested"
    assert cancelled.observed_state == "cancelled"
    assert adapter.effects == []
    assert store.readiness(operation_id)["ready"] is False


def test_limits_and_unapproved_sources_fail_before_any_fake_effect() -> None:
    plan, fixtures = _plan()
    declaration = plan.declaration
    too_small = BootstrapLimits(4, 4, 1, 2, 2)
    with pytest.raises(Outcome0BootstrapError, match="artifacts exceed"):
        BootstrapPlan.create(plan.binding, declaration, plan.environment, too_small)

    with pytest.raises(Outcome0BootstrapError, match="not within"):
        CapabilityDeclaration(
            declaration.id,
            declaration.approval_id,
            declaration.approved_by,
            declaration.sources,
            declaration.python_packages,
            declaration.system_packages,
            (
                DeclaredArtifact(
                    "unapproved-data",
                    "data",
                    "outcome0-fixtures",
                    "fixture://outcome0-fixtures-other/molecular-distillation/train.json",
                    _digest(b"x"),
                    1,
                ),
            ),
        )
    adapter = FakeOutcome0BootstrapAdapter(fixtures, FakeCustody())
    assert adapter.effects == []

    with pytest.raises(Outcome0BootstrapError, match="canonical URI"):
        DeclaredArtifact(
            "query-data",
            "data",
            "outcome0-fixtures",
            "fixture://outcome0-fixtures/molecular-distillation/train.json?other-source=true",
            _digest(b"x"),
            1,
        )


def test_declaration_order_is_canonical_for_reproducible_plan_identity() -> None:
    plan, _fixtures = _plan()
    data_uri = "fixture://outcome0-fixtures/molecular-distillation/validation.json"
    declaration = CapabilityDeclaration(
        "outcome0-capability",
        "approval-one",
        "human-one",
        plan.declaration.sources,
        (
            DeclaredPackage("zlib", "1", "6" * 64),
            plan.declaration.python_packages[0],
        ),
        (
            DeclaredPackage("zstd", "1", "7" * 64),
            plan.declaration.system_packages[0],
        ),
        (
            DeclaredArtifact(
                "validation-data",
                "data",
                "outcome0-fixtures",
                data_uri,
                _digest(b"validation"),
                len(b"validation"),
            ),
            plan.declaration.artifacts[0],
        ),
    )
    reordered = CapabilityDeclaration(
        declaration.id,
        declaration.approval_id,
        declaration.approved_by,
        tuple(reversed(declaration.sources)),
        tuple(reversed(declaration.python_packages)),
        tuple(reversed(declaration.system_packages)),
        tuple(reversed(declaration.artifacts)),
    )
    canonical = BootstrapPlan.create(plan.binding, declaration, plan.environment, plan.limits)
    equivalent = BootstrapPlan.create(plan.binding, reordered, plan.environment, plan.limits)
    assert equivalent.digest == canonical.digest
    assert equivalent.declaration == declaration


def test_provenance_binds_lease_package_lock_approval_and_artifact_hash() -> None:
    store, _adapter, custody, operation_id = _approved_store()
    completed = store.reconcile(operation_id)
    assert completed.evidence is not None

    manifest = completed.evidence.manifest
    assert manifest["execution"] == {
        "lease_id": "lease-one",
        "operation_id": "a" * 32,
        "package_digest": "3" * 64,
    }
    assert manifest["environment"] == {
        "image": "registry.example/outcome0@sha256:" + "4" * 64,
        "lock_digest": "5" * 64,
    }
    assert manifest["capability_declaration"] == {
        "id": "outcome0-capability",
        "approval_id": "approval-one",
        "approved_by": "human-one",
    }
    assert manifest["artifacts"][0]["sha256"] == custody.records[0].sha256
    assert (
        completed.evidence.manifest_digest
        == hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
