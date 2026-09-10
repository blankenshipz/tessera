"""Pure, fail-closed Kubernetes Job contracts for the opt-in Minikube fixture.

This module deliberately contains no Kubernetes client, ambient kubeconfig lookup,
or control-plane write path.  A caller supplies one fully identified cluster and
explicitly injected read/create/delete/Pod-observation functions.  It is a local
conformance seam, not a worker enrollment or provider adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION = re.compile(r"[0-9a-f]{32}\Z")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")
_MOUNT_PATH = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_PROFILE = "development-minikube-opencode-v1"


class KubernetesContractError(RuntimeError):
    """The observed resource cannot safely be attributed to this operation."""


class KubernetesUnresolvedError(KubernetesContractError):
    """An external Kubernetes side effect needs a later explicit reconciliation."""


def canonical_json(value: object) -> bytes:
    """Encode an unambiguous, bounded-by-call-site JSON value for hashing."""
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _required_string(value: object, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError(f"{field} must be a bounded non-empty string.")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"{field} has an invalid format.")
    return value


def _digest(value: object, field: str) -> str:
    return _required_string(value, field, _DIGEST)


def _kubernetes_name(value: object, field: str) -> str:
    name = _required_string(value, field, _NAME)
    if len(name) > 63:
        raise ValueError(f"{field} must be at most 63 characters.")
    return name


def _server(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
        raise ValueError("server must be a bounded explicit HTTPS endpoint.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("server must be an explicit HTTPS endpoint without credentials.")
    return value


@dataclass(frozen=True)
class KubernetesClusterConfig:
    """An explicit cluster/namespace identity; no current-context fallback exists."""

    kubeconfig: str
    kubeconfig_sha256: str
    context: str
    profile: str
    server: str
    server_sha256: str
    ca_sha256: str
    namespace: str
    cluster_uid: str
    namespace_uid: str

    def __post_init__(self) -> None:
        kubeconfig = _required_string(self.kubeconfig, "kubeconfig")
        if "\0" in kubeconfig or not Path(kubeconfig).is_absolute():
            raise ValueError("kubeconfig must be an explicit absolute path.")
        _digest(self.kubeconfig_sha256, "kubeconfig_sha256")
        _kubernetes_name(self.context, "context")
        if self.profile != _PROFILE:
            raise ValueError(f"profile must be {_PROFILE!r}.")
        _server(self.server)
        _digest(self.server_sha256, "server_sha256")
        _digest(self.ca_sha256, "ca_sha256")
        _kubernetes_name(self.namespace, "namespace")
        _required_string(self.cluster_uid, "cluster_uid", _UID)
        _required_string(self.namespace_uid, "namespace_uid", _UID)


@dataclass(frozen=True)
class KubernetesJobBinding:
    """Frozen operation/authority/package values copied into a Job contract."""

    operation_id: str
    attempt_id: str
    lease_id: str
    epoch: int
    package_sha256: str
    image: str
    command: tuple[str, ...]
    active_deadline_seconds: int
    cpu_millicores: int = 500
    memory_mebibytes: int = 256
    ephemeral_storage_mebibytes: int = 64
    network_mode: Literal["none", "cluster"] = "none"
    output_pvc: str | None = None
    output_mount_path: str | None = None
    output_pvc_uid: str | None = None
    output_pvc_spec_sha256: str | None = None

    def __post_init__(self) -> None:
        _required_string(self.operation_id, "operation_id", _OPERATION)
        _kubernetes_name(self.attempt_id, "attempt_id")
        _kubernetes_name(self.lease_id, "lease_id")
        if type(self.epoch) is not int or not 1 <= self.epoch <= 2**31 - 1:
            raise ValueError("epoch must be a positive integer.")
        _digest(self.package_sha256, "package_sha256")
        _required_string(self.image, "image", _IMAGE)
        if (
            not self.command
            or len(self.command) > 32
            or any(
                not isinstance(item, str)
                or not item
                or "\0" in item
                or len(item.encode("utf-8")) > 4096
                for item in self.command
            )
        ):
            raise ValueError("command must be a bounded non-empty argv.")
        if not 1 <= self.active_deadline_seconds <= 3600:
            raise ValueError("active_deadline_seconds must be between one second and one hour.")
        if not 50 <= self.cpu_millicores <= 1000:
            raise ValueError("cpu_millicores is outside the fixture bound.")
        if not 64 <= self.memory_mebibytes <= 1024:
            raise ValueError("memory_mebibytes is outside the fixture bound.")
        if not 16 <= self.ephemeral_storage_mebibytes <= 256:
            raise ValueError("ephemeral_storage_mebibytes is outside the fixture bound.")
        if self.network_mode not in {"none", "cluster"}:
            raise ValueError("network_mode must be explicit.")
        if self.output_pvc is None and self.output_mount_path is not None:
            raise ValueError("output_mount_path requires output_pvc.")
        if self.output_pvc is None and (
            self.output_pvc_uid is not None or self.output_pvc_spec_sha256 is not None
        ):
            raise ValueError("output PVC identity requires output_pvc.")
        if self.output_pvc is not None:
            _kubernetes_name(self.output_pvc, "output_pvc")
            if (
                not isinstance(self.output_mount_path, str)
                or not _MOUNT_PATH.fullmatch(self.output_mount_path)
                or self.output_mount_path == "/"
                or "//" in self.output_mount_path
                or "/../" in f"{self.output_mount_path}/"
                or "\0" in self.output_mount_path
                or len(self.output_mount_path.encode("utf-8")) > 256
            ):
                raise ValueError("output_mount_path must be a bounded absolute non-root path.")
        if (self.output_pvc_uid is None) != (self.output_pvc_spec_sha256 is None):
            raise ValueError("output PVC UID and spec digest must be supplied together.")
        if self.output_pvc_uid is not None:
            _required_string(self.output_pvc_uid, "output_pvc_uid", _UID)
            _digest(self.output_pvc_spec_sha256, "output_pvc_spec_sha256")

    @property
    def job_name(self) -> str:
        return f"tessera-op-{self.operation_id}"


@dataclass(frozen=True)
class KubernetesProviderIdentity:
    """Stable provider identity only after all three Kubernetes UIDs are observed."""

    cluster_uid: str
    namespace_uid: str
    job_uid: str

    def __post_init__(self) -> None:
        _required_string(self.cluster_uid, "cluster_uid", _UID)
        _required_string(self.namespace_uid, "namespace_uid", _UID)
        _required_string(self.job_uid, "job_uid", _UID)

    @property
    def provider_id(self) -> str:
        digest = hashlib.sha256(
            canonical_json(
                {
                    "cluster_uid": self.cluster_uid,
                    "job_uid": self.job_uid,
                    "namespace_uid": self.namespace_uid,
                    "version": "kubernetes-provider-v1",
                }
            )
        ).hexdigest()
        return f"kubernetes-v1:{digest}"


def cluster_identity_digest(cluster: KubernetesClusterConfig) -> str:
    """Fingerprint every explicit, secret-free cluster selection field."""
    return hashlib.sha256(
        canonical_json(
            {
                "ca_sha256": cluster.ca_sha256,
                "cluster_uid": cluster.cluster_uid,
                "context": cluster.context,
                "kubeconfig_sha256": cluster.kubeconfig_sha256,
                "namespace": cluster.namespace,
                "namespace_uid": cluster.namespace_uid,
                "profile": cluster.profile,
                "server": cluster.server,
                "server_sha256": cluster.server_sha256,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class KubernetesJobObservation:
    state: Literal["pending", "running", "succeeded", "failed", "unknown"]
    job_uid: str
    provider_identity: KubernetesProviderIdentity


@dataclass(frozen=True)
class KubernetesPodObservation:
    """An attributed owned-Pod observation; it is not an execution receipt."""

    state: Literal["pending", "running", "succeeded", "failed", "unknown"]
    pod_uid: str
    job_uid: str


def job_manifest(
    binding: KubernetesJobBinding, cluster: KubernetesClusterConfig
) -> dict[str, object]:
    """Build the sole canonical Job shape for this conformance profile."""
    labels = {
        "app.kubernetes.io/managed-by": "tessera",
        "tessera.dev/attempt": binding.attempt_id,
        "tessera.dev/epoch": str(binding.epoch),
        "tessera.dev/lease": binding.lease_id,
        "tessera.dev/operation": binding.operation_id,
    }
    annotations = {
        "tessera.dev/attempt": binding.attempt_id,
        "tessera.dev/ca-sha256": cluster.ca_sha256,
        "tessera.dev/cluster-config-sha256": cluster_identity_digest(cluster),
        "tessera.dev/cluster-uid": cluster.cluster_uid,
        "tessera.dev/epoch": str(binding.epoch),
        "tessera.dev/lease": binding.lease_id,
        "tessera.dev/namespace-uid": cluster.namespace_uid,
        "tessera.dev/network-mode": binding.network_mode,
        "tessera.dev/operation": binding.operation_id,
        "tessera.dev/package-sha256": binding.package_sha256,
        "tessera.dev/profile": cluster.profile,
        "tessera.dev/server-sha256": cluster.server_sha256,
    }
    if binding.output_pvc_uid is not None:
        annotations["tessera.dev/output-pvc-uid"] = binding.output_pvc_uid
        annotations["tessera.dev/output-pvc-spec-sha256"] = binding.output_pvc_spec_sha256
    manifest: dict[str, object] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "annotations": annotations,
            "labels": labels,
            "name": binding.job_name,
            "namespace": cluster.namespace,
        },
        "spec": {
            "activeDeadlineSeconds": binding.active_deadline_seconds,
            "backoffLimit": 0,
            "template": {
                "metadata": {"annotations": annotations, "labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "command": list(binding.command),
                            "image": binding.image,
                            "imagePullPolicy": "Never",
                            "name": "opencode",
                            "resources": {
                                "limits": {
                                    "cpu": f"{binding.cpu_millicores}m",
                                    "ephemeral-storage": f"{binding.ephemeral_storage_mebibytes}Mi",
                                    "memory": f"{binding.memory_mebibytes}Mi",
                                },
                                "requests": {
                                    "cpu": f"{binding.cpu_millicores}m",
                                    "ephemeral-storage": f"{binding.ephemeral_storage_mebibytes}Mi",
                                    "memory": f"{binding.memory_mebibytes}Mi",
                                },
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "privileged": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 65532,
                            },
                            "volumeMounts": [{"mountPath": "/work", "name": "work"}],
                        }
                    ],
                    "hostIPC": False,
                    "hostNetwork": False,
                    "hostPID": False,
                    "enableServiceLinks": False,
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {
                        "runAsGroup": 65532,
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumes": [{"emptyDir": {"medium": "Memory"}, "name": "work"}],
                },
            },
        },
    }
    if binding.output_pvc is not None:
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        volumes = manifest["spec"]["template"]["spec"]["volumes"]
        assert isinstance(container, dict) and isinstance(volumes, list)
        container["volumeMounts"].append(
            {"mountPath": binding.output_mount_path, "name": "persistent-output"}
        )
        volumes.append(
            {
                "name": "persistent-output",
                "persistentVolumeClaim": {"claimName": binding.output_pvc},
            }
        )
    annotations["tessera.dev/spec-sha256"] = job_manifest_digest(manifest)
    return manifest


def job_manifest_digest(manifest: Mapping[str, object]) -> str:
    """Digest the canonical intended manifest without its self-referential digest."""
    copied = json.loads(canonical_json(manifest))
    for annotation_map in (
        copied.get("metadata", {}).get("annotations", {}),
        copied.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}),
    ):
        if isinstance(annotation_map, dict):
            annotation_map.pop("tessera.dev/spec-sha256", None)
    return hashlib.sha256(canonical_json(copied)).hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KubernetesContractError(f"Kubernetes {field} is missing or malformed.")
    return value


def _matches_expected(expected: object, observed: object) -> bool:
    """Compare every contract field while permitting Kubernetes defaulted fields."""
    if isinstance(expected, Mapping):
        return isinstance(observed, Mapping) and all(
            key in observed and _matches_expected(value, observed[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(observed, list)
            and len(expected) == len(observed)
            and all(
                _matches_expected(item, candidate)
                for item, candidate in zip(expected, observed, strict=True)
            )
        )
    return expected == observed


def _matches_expected_exactly(expected: object, observed: object) -> bool:
    """Compare a normalized executable subtree without permitting extra fields."""
    if isinstance(expected, Mapping):
        return (
            isinstance(observed, Mapping)
            and set(expected) == set(observed)
            and all(
                _matches_expected_exactly(value, observed[key]) for key, value in expected.items()
            )
        )
    if isinstance(expected, list):
        return (
            isinstance(observed, list)
            and len(expected) == len(observed)
            and all(
                _matches_expected_exactly(item, candidate)
                for item, candidate in zip(expected, observed, strict=True)
            )
        )
    return expected == observed


_JOB_SPEC_DEFAULTS: dict[str, object] = {
    "completionMode": "NonIndexed",
    "completions": 1,
    "managedBy": "kubernetes.io/job-controller",
    "manualSelector": False,
    "parallelism": 1,
    "podReplacementPolicy": "TerminatingOrFailed",
    "suspend": False,
}
_POD_SPEC_DEFAULTS: dict[str, object] = {
    "dnsPolicy": "ClusterFirst",
    "preemptionPolicy": "PreemptLowerPriority",
    "priority": 0,
    "schedulerName": "default-scheduler",
    "serviceAccount": "default",
    "serviceAccountName": "default",
}
_CONTAINER_DEFAULTS: dict[str, object] = {
    "terminationMessagePath": "/dev/termination-log",
    "terminationMessagePolicy": "File",
}


def _reject_unallowlisted_fields(
    observed: Mapping[str, object], allowed: set[str], field: str
) -> None:
    unexpected = sorted(set(observed) - allowed)
    if unexpected:
        raise KubernetesContractError(
            f"Kubernetes {field} has unallowlisted executable fields: {', '.join(unexpected)}."
        )


def _validate_generated_selector(value: object, job_uid: str) -> None:
    """Accept only the API-server's UID-bound Job selector default."""
    selector = _mapping(value, "Job selector")
    _reject_unallowlisted_fields(selector, {"matchLabels"}, "Job selector")
    labels = _mapping(selector.get("matchLabels"), "Job selector matchLabels")
    allowed = {"batch.kubernetes.io/controller-uid", "controller-uid"}
    _reject_unallowlisted_fields(labels, allowed, "Job selector matchLabels")
    if not labels or any(label_value != job_uid for label_value in labels.values()):
        raise KubernetesContractError("Kubernetes Job selector is not the generated UID selector.")


def _validate_pod_spec(
    observed: object,
    expected: object,
    *,
    field: str,
    mismatch_message: str = "Kubernetes Pod spec conflicts with the operation binding.",
) -> Mapping[str, object]:
    observed_spec = _mapping(observed, field)
    expected_spec = _mapping(expected, f"expected {field}")
    _reject_unallowlisted_fields(
        observed_spec,
        set(expected_spec) | set(_POD_SPEC_DEFAULTS) | {"nodeName", "tolerations"},
        field,
    )
    for name, value in _POD_SPEC_DEFAULTS.items():
        if name in observed_spec and observed_spec[name] != value:
            raise KubernetesContractError(f"Kubernetes {field} default {name} conflicts.")
    if "nodeName" in observed_spec:
        try:
            _kubernetes_name(observed_spec["nodeName"], "nodeName")
        except ValueError as error:
            raise KubernetesContractError("Kubernetes Pod nodeName is malformed.") from error
    if "tolerations" in observed_spec and observed_spec["tolerations"] != [
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/not-ready",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/unreachable",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
    ]:
        raise KubernetesContractError("Kubernetes Pod default tolerations conflict.")
    containers = observed_spec.get("containers")
    expected_containers = expected_spec.get("containers")
    if not isinstance(containers, list) or not isinstance(expected_containers, list):
        raise KubernetesContractError(f"Kubernetes {field} containers are malformed.")
    if len(containers) != len(expected_containers):
        raise KubernetesContractError(f"Kubernetes {field} containers conflict.")
    for index, (container, expected_container) in enumerate(
        zip(containers, expected_containers, strict=True)
    ):
        observed_container = _mapping(container, f"{field} container {index}")
        expected_container_map = _mapping(expected_container, f"expected {field} container {index}")
        _reject_unallowlisted_fields(
            observed_container,
            set(expected_container_map) | set(_CONTAINER_DEFAULTS),
            f"{field} container {index}",
        )
        for name, value in _CONTAINER_DEFAULTS.items():
            if name in observed_container and observed_container[name] != value:
                raise KubernetesContractError(
                    f"Kubernetes {field} container {index} default {name} conflicts."
                )
    normalized_spec = dict(observed_spec)
    for name in _POD_SPEC_DEFAULTS:
        normalized_spec.pop(name, None)
    normalized_spec.pop("nodeName", None)
    normalized_spec.pop("tolerations", None)
    normalized_spec["containers"] = [
        {
            name: value
            for name, value in _mapping(container, f"{field} container {index}").items()
            if name not in _CONTAINER_DEFAULTS
        }
        for index, container in enumerate(containers)
    ]
    if not _matches_expected_exactly(expected_spec, normalized_spec):
        raise KubernetesContractError(mismatch_message)
    return observed_spec


def _validate_job_spec(observed: object, expected: object, job_uid: str) -> None:
    observed_spec = _mapping(observed, "Job spec")
    expected_spec = _mapping(expected, "expected Job spec")
    _reject_unallowlisted_fields(
        observed_spec, set(expected_spec) | set(_JOB_SPEC_DEFAULTS) | {"selector"}, "Job spec"
    )
    for name, value in _JOB_SPEC_DEFAULTS.items():
        if name in observed_spec and observed_spec[name] != value:
            raise KubernetesContractError(f"Kubernetes Job default {name} conflicts.")
    if "selector" in observed_spec:
        _validate_generated_selector(observed_spec["selector"], job_uid)
    template = _mapping(observed_spec.get("template"), "Job template")
    expected_template = _mapping(expected_spec.get("template"), "expected Job template")
    _reject_unallowlisted_fields(template, set(expected_template), "Job template")
    _validate_pod_spec(
        template.get("spec"),
        expected_template.get("spec"),
        field="Job Pod spec",
        mismatch_message="Kubernetes Job immutable spec conflicts with the operation binding.",
    )
    if not _matches_expected(expected_spec, observed_spec):
        raise KubernetesContractError(
            "Kubernetes Job immutable spec conflicts with the operation binding."
        )


def parse_job(
    observed: Mapping[str, object], binding: KubernetesJobBinding, cluster: KubernetesClusterConfig
) -> KubernetesJobObservation:
    """Validate immutable Job binding/spec and derive the provider identity from UIDs."""
    observed = _mapping(observed, "Job")
    expected = job_manifest(binding, cluster)
    if (
        observed.get("apiVersion") != expected["apiVersion"]
        or observed.get("kind") != expected["kind"]
    ):
        raise KubernetesContractError("Kubernetes Job type conflicts with the operation binding.")
    metadata = _mapping(observed.get("metadata"), "metadata")
    expected_metadata = _mapping(expected["metadata"], "expected metadata")
    if not _matches_expected(expected_metadata, metadata):
        raise KubernetesContractError(
            "Kubernetes Job name, namespace, labels, or annotations conflict."
        )
    try:
        job_uid = _required_string(metadata["uid"], "job_uid", _UID)
    except (KeyError, ValueError) as error:
        raise KubernetesContractError("Kubernetes Job UID is unavailable or malformed.") from error
    _validate_job_spec(observed.get("spec"), expected["spec"], job_uid)
    annotations = _mapping(metadata.get("annotations"), "metadata annotations")
    if annotations.get("tessera.dev/spec-sha256") != job_manifest_digest(expected):
        raise KubernetesContractError("Kubernetes Job canonical spec digest conflicts.")
    status = observed.get("status", {})
    if not isinstance(status, Mapping):
        state: Literal["pending", "running", "succeeded", "failed", "unknown"] = "unknown"
    elif type(status.get("succeeded", 0)) is int and status.get("succeeded", 0) > 0:
        state = "succeeded"
    elif type(status.get("failed", 0)) is int and status.get("failed", 0) > 0:
        state = "failed"
    elif type(status.get("active", 0)) is int and status.get("active", 0) > 0:
        state = "running"
    elif all(
        type(status.get(key, 0)) is int and status.get(key, 0) >= 0
        for key in ("succeeded", "failed", "active")
    ):
        state = "pending"
    else:
        state = "unknown"
    identity = KubernetesProviderIdentity(cluster.cluster_uid, cluster.namespace_uid, job_uid)
    return KubernetesJobObservation(state, job_uid, identity)


def _pod_is_owned_by(pod: Mapping[str, object], job_uid: str) -> bool:
    metadata = _mapping(pod.get("metadata"), "Pod metadata")
    references = metadata.get("ownerReferences")
    if not isinstance(references, Sequence) or isinstance(references, (str, bytes)):
        raise KubernetesContractError("Kubernetes Pod has no owner reference.")
    return any(
        isinstance(reference, Mapping)
        and reference.get("kind") == "Job"
        and reference.get("uid") == job_uid
        and reference.get("controller") is True
        for reference in references
    )


def parse_pod(
    observed: Mapping[str, object],
    binding: KubernetesJobBinding,
    cluster: KubernetesClusterConfig,
    job_uid: str,
) -> KubernetesPodObservation:
    """Validate an owned Pod against the frozen Job template and report its phase."""
    observed = _mapping(observed, "Pod")
    if observed.get("apiVersion") != "v1" or observed.get("kind") != "Pod":
        raise KubernetesContractError("Kubernetes Pod type conflicts with the operation binding.")
    metadata = _mapping(observed.get("metadata"), "Pod metadata")
    expected = job_manifest(binding, cluster)
    template = _mapping(
        _mapping(expected["spec"], "expected spec")["template"], "expected template"
    )
    expected_metadata = _mapping(template["metadata"], "expected Pod metadata")
    if metadata.get("namespace") != cluster.namespace or not _matches_expected(
        expected_metadata, metadata
    ):
        raise KubernetesContractError(
            "Kubernetes Pod metadata conflicts with the operation binding."
        )
    _validate_pod_spec(observed.get("spec"), template["spec"], field="Pod spec")
    if not _pod_is_owned_by(observed, job_uid):
        raise KubernetesContractError("Kubernetes Pod is not owned by the attributed Job.")
    try:
        pod_uid = _required_string(metadata["uid"], "pod_uid", _UID)
    except (KeyError, ValueError) as error:
        raise KubernetesContractError("Kubernetes Pod UID is unavailable or malformed.") from error
    status = observed.get("status")
    phase = status.get("phase") if isinstance(status, Mapping) else None
    states: dict[object, Literal["pending", "running", "succeeded", "failed", "unknown"]] = {
        "Pending": "pending",
        "Running": "running",
        "Succeeded": "succeeded",
        "Failed": "failed",
        "Unknown": "unknown",
    }
    return KubernetesPodObservation(states.get(phase, "unknown"), pod_uid, job_uid)


@dataclass(frozen=True)
class KubernetesCancellationObservation:
    requested: bool
    job_observed: bool
    pods_observed: bool
    termination_confirmed: bool


InspectJob = Callable[[str], Mapping[str, object] | None]
CreateJob = Callable[[Mapping[str, object]], Mapping[str, object] | None]
DeleteJob = Callable[[str, str], object]
ObservePods = Callable[[str], Sequence[Mapping[str, object]]]


class KubernetesJobJournal:
    """Durable one-create fence around a deterministic Job name.

    It intentionally resembles the existing external-operation journal semantics
    without adding a control schema: a lost create reply can only inspect the one
    deterministic name, never issue another create request.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS kubernetes_jobs (
                operation_id TEXT PRIMARY KEY, binding_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL, create_intent INTEGER NOT NULL
                CHECK (create_intent IN (0, 1)), job_uid TEXT)"""
            )

    def reconcile_create(
        self,
        binding: KubernetesJobBinding,
        cluster: KubernetesClusterConfig,
        inspect: InspectJob,
        create: CreateJob,
    ) -> KubernetesJobObservation:
        manifest = job_manifest(binding, cluster)
        self._bind(binding, manifest)
        found = inspect(binding.job_name)
        if found is not None:
            return self._record_observation(
                parse_job(found, binding, cluster), binding.operation_id
            )
        if self._create_intent(binding.operation_id):
            raise KubernetesUnresolvedError(
                "Kubernetes Job create remains unresolved; inspect only."
            )
        self._mark_create_intent(binding.operation_id)
        try:
            created = create(manifest)
        except Exception:
            created = None
        found = created if created is not None else inspect(binding.job_name)
        if found is None:
            raise KubernetesUnresolvedError("Kubernetes Job create reply was lost; inspect only.")
        return self._record_observation(parse_job(found, binding, cluster), binding.operation_id)

    def request_cancel(
        self,
        binding: KubernetesJobBinding,
        cluster: KubernetesClusterConfig,
        inspect: InspectJob,
        delete: DeleteJob,
        observe_pods: ObservePods,
    ) -> KubernetesCancellationObservation:
        """Cancel, then require *fresh* Job and owned-Pod observations.

        A deletion acknowledgement is deliberately discarded.  If either fresh
        observation fails, the cancellation remains unresolved.
        """
        self._bind(binding, job_manifest(binding, cluster))
        first = inspect(binding.job_name)
        if first is None:
            job_uid = self._recorded_job_uid(binding.operation_id)
            if job_uid is None:
                return KubernetesCancellationObservation(True, True, False, False)
            try:
                pods = observe_pods(job_uid)
                for pod in pods:
                    parse_pod(pod, binding, cluster, job_uid)
            except Exception:
                return KubernetesCancellationObservation(True, True, False, False)
            return KubernetesCancellationObservation(True, True, True, not pods)
        initial = self._record_observation(parse_job(first, binding, cluster), binding.operation_id)
        try:
            delete(binding.job_name, initial.job_uid)
        except Exception:
            return KubernetesCancellationObservation(True, False, False, False)
        try:
            after = inspect(binding.job_name)
            fresh_job = parse_job(after, binding, cluster) if after is not None else None
            pods = observe_pods(initial.job_uid)
        except Exception:
            return KubernetesCancellationObservation(True, False, False, False)
        if fresh_job is not None and fresh_job.job_uid != initial.job_uid:
            raise KubernetesContractError("Kubernetes Job UID changed during cancellation.")
        for pod in pods:
            parse_pod(pod, binding, cluster, initial.job_uid)
        # A delete acknowledgement is not termination.  This profile confirms a
        # cancellation only after both fresh queries show absence; terminal objects
        # can still be stale or retain owned Pods.
        confirmed = fresh_job is None and not pods
        return KubernetesCancellationObservation(True, True, True, confirmed)

    def _bind(self, binding: KubernetesJobBinding, manifest: Mapping[str, object]) -> None:
        encoded_binding = canonical_json(binding.__dict__).decode("utf-8")
        encoded_manifest = canonical_json(manifest).decode("utf-8")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT binding_json, manifest_json FROM kubernetes_jobs WHERE operation_id=?",
                (binding.operation_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO kubernetes_jobs VALUES (?, ?, ?, 0, NULL)",
                    (binding.operation_id, encoded_binding, encoded_manifest),
                )
            elif (row["binding_json"], row["manifest_json"]) != (encoded_binding, encoded_manifest):
                raise KubernetesContractError(
                    "Kubernetes operation binding conflicts with its durable intent."
                )

    def _create_intent(self, operation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT create_intent FROM kubernetes_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KubernetesContractError("Kubernetes operation intent disappeared.")
        return bool(row["create_intent"])

    def _mark_create_intent(self, operation_id: str) -> None:
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE kubernetes_jobs SET create_intent=1 "
                    "WHERE operation_id=? AND create_intent=0",
                    (operation_id,),
                ).rowcount
                != 1
            ):
                raise KubernetesUnresolvedError("Kubernetes Job create intent is already fenced.")

    def _record_observation(
        self, observation: KubernetesJobObservation, operation_id: str
    ) -> KubernetesJobObservation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid FROM kubernetes_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["job_uid"] not in {None, observation.job_uid}:
                raise KubernetesContractError(
                    "Kubernetes Job UID conflicts with the durable operation."
                )
            connection.execute(
                "UPDATE kubernetes_jobs SET job_uid=? WHERE operation_id=?",
                (observation.job_uid, operation_id),
            )
        return observation

    def _recorded_job_uid(self, operation_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_uid FROM kubernetes_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KubernetesContractError("Kubernetes operation intent disappeared.")
        job_uid = row["job_uid"]
        if job_uid is None:
            return None
        try:
            return _required_string(job_uid, "job_uid", _UID)
        except ValueError as error:
            raise KubernetesContractError(
                "Kubernetes Job UID in durable intent is malformed."
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


__all__ = [
    "KubernetesCancellationObservation",
    "KubernetesClusterConfig",
    "KubernetesContractError",
    "KubernetesJobBinding",
    "KubernetesJobJournal",
    "KubernetesJobObservation",
    "KubernetesPodObservation",
    "KubernetesProviderIdentity",
    "KubernetesUnresolvedError",
    "canonical_json",
    "cluster_identity_digest",
    "job_manifest",
    "job_manifest_digest",
    "parse_job",
    "parse_pod",
]
