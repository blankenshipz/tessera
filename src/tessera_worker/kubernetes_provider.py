"""Pure Kubernetes Job contract surface.

This module is deliberately an adapter-free public surface: callers provide all
observations and side-effect functions.  It never reads ambient kubeconfig,
starts a subprocess, or contacts a cluster.  The lower-level contract module is
kept as the single implementation of the immutable manifest rules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

from .kubernetes_contracts import (
    KubernetesCancellationObservation,
    KubernetesClusterConfig,
    KubernetesContractError,
    KubernetesJobBinding,
    KubernetesJobJournal,
    KubernetesJobObservation,
    KubernetesPodObservation,
    KubernetesProviderIdentity,
    KubernetesUnresolvedError,
    canonical_json,
    cluster_identity_digest,
    domain_identifier_from_resource_name,
    job_manifest,
    job_manifest_digest,
    kubernetes_resource_name,
    parse_job,
    parse_pod,
)

ClusterIdentity = KubernetesClusterConfig
JobBinding = KubernetesJobBinding
ProviderIdentity = KubernetesProviderIdentity
JobObservation = KubernetesJobObservation
PodObservation = KubernetesPodObservation
CancellationObservation = KubernetesCancellationObservation

_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")


def deterministic_job_name(operation_id: str) -> str:
    """Return the DNS-safe, stable Job name for one operation."""
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation_id must be exactly 32 lowercase hexadecimal characters.")
    return f"tessera-op-{operation_id}"


def canonical_job_spec(binding: JobBinding, cluster: ClusterIdentity) -> dict[str, object]:
    """Build the digest-pinned, security-hardened canonical Job manifest."""
    if deterministic_job_name(binding.operation_id) != binding.job_name:
        raise KubernetesContractError("Job name is not the deterministic operation binding.")
    return job_manifest(binding, cluster)


def canonical_job_spec_digest(spec: Mapping[str, object]) -> str:
    """Digest a canonical Job manifest, excluding its self-referential annotation."""
    return job_manifest_digest(spec)


def stable_provider_identity(
    cluster_uid: str, namespace_uid: str, job_uid: str
) -> ProviderIdentity:
    """Construct the stable identity from observed cluster, namespace, and Job UIDs."""
    return ProviderIdentity(cluster_uid, namespace_uid, job_uid)


def reconcile_create(
    journal: KubernetesJobJournal,
    binding: JobBinding,
    cluster: ClusterIdentity,
    inspect: Callable[[str], Mapping[str, object] | None],
    create: Callable[[Mapping[str, object]], Mapping[str, object] | None],
) -> JobObservation:
    """Reconcile one create, fencing retries after a lost create response."""
    return journal.reconcile_create(binding, cluster, inspect, create)


def reconcile_cancellation(
    journal: KubernetesJobJournal,
    binding: JobBinding,
    cluster: ClusterIdentity,
    inspect: Callable[[str], Mapping[str, object] | None],
    delete: Callable[[str, str], object],
    observe_pods: Callable[[str], Sequence[Mapping[str, object]]],
) -> CancellationObservation:
    """Request deletion; report confirmation only after fresh Job/Pod absence."""
    return journal.request_cancel(binding, cluster, inspect, delete, observe_pods)


__all__ = [
    "CancellationObservation",
    "ClusterIdentity",
    "JobBinding",
    "JobObservation",
    "KubernetesContractError",
    "KubernetesJobJournal",
    "KubernetesUnresolvedError",
    "PodObservation",
    "ProviderIdentity",
    "canonical_job_spec",
    "canonical_job_spec_digest",
    "canonical_json",
    "cluster_identity_digest",
    "deterministic_job_name",
    "domain_identifier_from_resource_name",
    "kubernetes_resource_name",
    "parse_job",
    "parse_pod",
    "reconcile_cancellation",
    "reconcile_create",
    "stable_provider_identity",
]
