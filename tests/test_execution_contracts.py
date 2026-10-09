"""Compatibility evidence for the provider-neutral Kubernetes translation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_worker.execution_contracts import (
    ExecutionAdapter,
    ExecutionEnvelope,
    PreflightRequest,
    TargetBinding,
)
from tessera_worker.kubernetes_contracts import KubernetesProviderIdentity, cluster_identity_digest
from tessera_worker.kubernetes_execution import (
    KubernetesExecutionError,
    KubernetesExecutionProtocolAdapter,
)


def _neutral_adapter(tmp_path: Path):
    _response, _worker, package = _admitted_package(tmp_path)
    cluster = _cluster()
    fake = _FakeKubernetes(package, cluster)
    neutral = KubernetesExecutionProtocolAdapter(fake.adapter(tmp_path / "kubernetes.sqlite3"))
    return package, cluster, fake, neutral


def test_kubernetes_translation_preserves_identity_deadline_and_preflight(tmp_path: Path) -> None:
    package, cluster, _fake, neutral = _neutral_adapter(tmp_path)
    envelope = package.execution_envelope(neutral.target)

    assert isinstance(neutral, ExecutionAdapter)
    assert envelope.target == TargetBinding(
        "kubernetes-job", cluster.profile, cluster_identity_digest(cluster)
    )
    assert envelope.deadline == min(
        package.hard_expires_at,
        package.lease_expires_at,
        package.lease_issued_at + timedelta(seconds=package.approved_duration_seconds),
    )
    assert neutral.preflight(PreflightRequest(envelope.target, envelope)).admissible is True
    assert neutral.capabilities(envelope.target).collect is True
    assert neutral.account(envelope).exposure_state == "unknown"

    elapsed = ExecutionEnvelope(
        envelope.operation_id,
        envelope.package_digest,
        envelope.target,
        envelope.lease_id,
        envelope.epoch,
        datetime.now(UTC) - timedelta(seconds=1),
        envelope.payload,
    )
    assert neutral.preflight(PreflightRequest(elapsed.target, elapsed)).admissible is False


def test_kubernetes_unknown_observation_reconciles_without_resubmission(tmp_path: Path) -> None:
    package, _cluster_value, fake, neutral = _neutral_adapter(tmp_path)
    envelope = package.execution_envelope(neutral.target)

    submitted = neutral.submit(envelope)
    assert submitted.state == "running"
    assert (
        submitted.handle.provider_id
        == KubernetesProviderIdentity(
            fake.cluster.cluster_uid, fake.cluster.namespace_uid, "job-fixture"
        ).provider_id
    )

    fake.jobs.clear()
    unknown = neutral.observe(envelope)
    reconciled = neutral.reconcile(envelope)

    assert unknown.state == reconciled.state == "unknown"
    assert unknown.handle == reconciled.handle == submitted.handle
    assert fake.creates == 1


def test_kubernetes_translation_maps_cancel_and_collection_without_weakening_handoff(
    tmp_path: Path,
) -> None:
    package, _cluster_value, fake, neutral = _neutral_adapter(tmp_path)
    envelope = package.execution_envelope(neutral.target)
    submitted = neutral.submit(envelope)

    cancelled = neutral.cancel(envelope)
    assert cancelled.requested is True
    assert cancelled.termination_confirmed is True
    assert cancelled.reconciliation_required is False
    assert cancelled.handle == submitted.handle
    with pytest.raises(KubernetesExecutionError, match="successful Kubernetes Job"):
        neutral.collect(envelope)

    # A cancelled operation cannot collect. A distinct successful operation
    # proves that generic collection retains the existing target-bound handoff.
    second_root = tmp_path / "second"
    second_root.mkdir()
    second, _second_cluster, second_fake, second_neutral = _neutral_adapter(second_root)
    second_envelope = second.execution_envelope(second_neutral.target)
    second_neutral.submit(second_envelope)
    second_fake.jobs[second.binding.job_name] = second_fake.job("succeeded")
    second_fake.pods = [second_fake.pod("Succeeded")]
    observed = second_neutral.reconcile(second_envelope)
    handoff = second_neutral.collect(second_envelope)
    kubernetes_handoff = second_neutral.adapter.artifact_collection_handoff(second)

    assert observed.state == "succeeded"
    assert handoff.handle == observed.handle
    assert handoff.output_paths == ("result.txt",)
    assert kubernetes_handoff.pvc_uid == "pvc-fixture"
    assert kubernetes_handoff.job_uid == "job-fixture"
    assert second_fake.creates == 1
