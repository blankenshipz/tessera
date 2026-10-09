"""Run an opt-in, isolated Docker Desktop Kubernetes quota conformance probe.

The fixture creates a newly named, run-labelled Namespace, PVC, Pods, and
NetworkPolicies only.  It never writes to a retained Astra namespace or PVC.
It is outside pytest/CI and does not attach a target, dispatch work, or invoke a
provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tessera_worker.docker_desktop_kubernetes import (
    CONTEXT,
    LiveKubernetesError,
    build_and_load_exact_image,
)

PROFILE = "astra-kubernetes-quota-conformance-v2"
QUOTA_CAPABILITY = "filesystem-quota-v1"
BYTE_QUOTA_ANNOTATION = "tessera.io/byte-quota-bytes"
INODE_QUOTA_ANNOTATION = "tessera.io/inode-quota-files"
QUOTA_CAPABILITY_ANNOTATION = "tessera.io/quota-enforcement"
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_QUANTITY = re.compile(r"([1-9][0-9]*)(Ki|Mi|Gi)\Z")
_MAX_OUTPUT = 32 * 1024
_MAX_BYTE_QUOTA = 64 * 1024 * 1024
_MAX_INODE_QUOTA = 10_000
_QUOTA_ERRNOS = {28, 122}  # ENOSPC and EDQUOT on Linux.
_NETWORK_TIMEOUT_SECONDS = 3


class ConformanceError(RuntimeError):
    """The live probe cannot safely continue or establish its stated result."""


class UnsupportedQuota(ConformanceError):
    """The selected StorageClass cannot prove the requested hard quotas."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _name(value: str, field: str) -> str:
    if len(value) > 63 or not _NAME.fullmatch(value):
        raise ConformanceError(
            f"{field} must be a lowercase Kubernetes name of at most 63 characters"
        )
    return value


def _positive_bound(value: int, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConformanceError(f"{field} must be between 1 and {maximum}")
    return value


def _storage_request(value: str) -> int:
    match = _QUANTITY.fullmatch(value)
    if match is None:
        raise ConformanceError("--pvc-storage must be an explicit Ki, Mi, or Gi quantity")
    multiplier = {"Ki": 1024, "Mi": 1024 * 1024, "Gi": 1024 * 1024 * 1024}[match.group(2)]
    return int(match.group(1)) * multiplier


def _metadata(name: str, run_id: str, role: str, namespace: str | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {
        "name": name,
        "labels": {
            "app.kubernetes.io/managed-by": "tessera-astra-conformance",
            "tessera.io/conformance-run": run_id,
            "tessera.io/conformance-role": role,
        },
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    return metadata


def namespace_manifest(name: str, run_id: str) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": _metadata(name, run_id, "probe-namespace"),
    }


def pvc_manifest(
    *, namespace: str, name: str, run_id: str, storage_class: str, storage: str
) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": _metadata(name, run_id, "probe-pvc", namespace),
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": storage}},
        },
    }


def network_manifests(namespace: str, run_id: str, prefix: str) -> tuple[dict[str, object], ...]:
    """Create the probe's own deny-all and narrowly-scoped allow policies."""
    server = {"tessera.io/conformance-run": run_id, "tessera.io/conformance-role": "network-server"}
    allowed = {
        "tessera.io/conformance-run": run_id,
        "tessera.io/conformance-role": "network-allowed",
    }
    return (
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _metadata(f"{prefix}-deny-all", run_id, "network-deny-all", namespace),
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _metadata(f"{prefix}-ingress", run_id, "network-ingress-policy", namespace),
            "spec": {
                "podSelector": {"matchLabels": server},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": allowed}}],
                        "ports": [{"protocol": "TCP", "port": 8080}],
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _metadata(f"{prefix}-egress", run_id, "network-egress-policy", namespace),
            "spec": {
                "podSelector": {"matchLabels": allowed},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"podSelector": {"matchLabels": server}}],
                        "ports": [{"protocol": "TCP", "port": 8080}],
                    }
                ],
            },
        },
    )


def pod_manifest(
    *,
    name: str,
    namespace: str,
    run_id: str,
    role: str,
    image: str,
    command: list[str],
    pvc: str | None = None,
) -> dict[str, object]:
    container: dict[str, object] = {
        "name": "probe",
        "image": image,
        "imagePullPolicy": "Never",
        "args": command,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    spec: dict[str, object] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
    }
    if pvc is not None:
        container["volumeMounts"] = [{"name": "outputs", "mountPath": "/outputs"}]
        spec["volumes"] = [{"name": "outputs", "persistentVolumeClaim": {"claimName": pvc}}]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": _metadata(name, run_id, role, namespace),
        "spec": spec,
    }


def _identity(
    resource: Mapping[str, Any], kind: str, name: str, namespace: str | None = None
) -> dict[str, str]:
    metadata = resource.get("metadata")
    if (
        resource.get("kind") != kind
        or not isinstance(metadata, Mapping)
        or metadata.get("name") != name
        or (namespace is not None and metadata.get("namespace") != namespace)
    ):
        raise ConformanceError(f"{kind}/{name} identity did not match the explicit target")
    uid = metadata.get("uid")
    created = metadata.get("creationTimestamp")
    if not isinstance(uid, str) or not uid or not isinstance(created, str) or not created:
        raise ConformanceError(f"{kind}/{name} did not return UID and creation timestamp")
    return {"name": name, "uid": uid, "creation_timestamp": created}


def _owned(resource: Mapping[str, Any], run_id: str) -> bool:
    metadata = resource.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
    return (
        isinstance(labels, Mapping)
        and labels.get("app.kubernetes.io/managed-by") == "tessera-astra-conformance"
        and labels.get("tessera.io/conformance-run") == run_id
    )


def quota_capability(
    storage_class: Mapping[str, Any], byte_quota: int, inode_quota: int
) -> dict[str, object]:
    """Require an explicit StorageClass declaration; PVC requests are not evidence."""
    annotations = (
        storage_class.get("metadata", {}).get("annotations")
        if isinstance(storage_class.get("metadata"), Mapping)
        else None
    )
    if not isinstance(annotations, Mapping):
        raise UnsupportedQuota("StorageClass has no explicit filesystem-quota-v1 declaration")
    if QUOTA_CAPABILITY_ANNOTATION not in annotations:
        raise UnsupportedQuota("StorageClass has no explicit filesystem-quota-v1 declaration")
    observed = {
        "quota_enforcement": annotations.get(QUOTA_CAPABILITY_ANNOTATION),
        "byte_quota_bytes": annotations.get(BYTE_QUOTA_ANNOTATION),
        "inode_quota_files": annotations.get(INODE_QUOTA_ANNOTATION),
    }
    expected = {
        "quota_enforcement": QUOTA_CAPABILITY,
        "byte_quota_bytes": str(byte_quota),
        "inode_quota_files": str(inode_quota),
    }
    if observed != expected:
        raise UnsupportedQuota(
            f"StorageClass quota declaration does not match explicit bounds: observed={observed!r}"
        )
    return {"declared": observed, "capability": QUOTA_CAPABILITY}


def classify_quota_evidence(kind: str, record: Mapping[str, Any], bound: int) -> dict[str, object]:
    """Classify a probe record without treating request/free space as a hard limit."""
    actual_key = "written_bytes" if kind == "byte" else "created_files"
    requested_key = "requested_bytes" if kind == "byte" else "requested_files"
    if record.get("kind") != kind or record.get("limit") != bound:
        raise ConformanceError(f"{kind} probe did not report its declared limit")
    actual = record.get(actual_key)
    requested = record.get(requested_key)
    if (
        isinstance(actual, bool)
        or not isinstance(actual, int)
        or isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested <= bound
    ):
        raise ConformanceError(f"{kind} probe did not attempt bounded work past the declared limit")
    outcome = record.get("outcome")
    cleanup_error = record.get("cleanup_error")
    if cleanup_error not in (None, ""):
        raise ConformanceError(f"{kind} probe did not clean its bounded probe data")
    if outcome == "not_enforced":
        if actual <= bound:
            raise ConformanceError(f"{kind} probe reported inconsistent non-enforcement evidence")
        return {
            "classification": "unsupported",
            "actual": actual,
            "requested": requested,
            "record": dict(record),
        }
    if outcome != "enforced" or actual > bound:
        raise ConformanceError(f"{kind} probe did not establish quota enforcement")
    failure_errno = record.get("failure_errno")
    failure = record.get("failure")
    if failure_errno not in _QUOTA_ERRNOS or not isinstance(failure, str) or not failure:
        raise ConformanceError(f"{kind} probe failed for a non-quota reason")
    return {
        "classification": "enforced",
        "actual": actual,
        "requested": requested,
        "record": dict(record),
    }


def classify_network_enforcement(
    *,
    declaration: Sequence[Mapping[str, str]],
    server: Mapping[str, str],
    allowed: Mapping[str, str],
    denied: Mapping[str, str],
) -> dict[str, object]:
    """Distinguish declared policies from two bounded observed connections.

    The probe image exits nonzero when an expected connection outcome is wrong,
    but inspecting its bounded output here prevents a Pod phase alone from being
    mislabelled as NetworkPolicy enforcement.
    """
    if len(declaration) != 3 or any(not item.get("uid") for item in declaration):
        raise ConformanceError("NetworkPolicy declaration identities are incomplete")
    if allowed.get("expected") != "allow" or allowed.get("log") != "allowed connection observed":
        raise ConformanceError("allowed connectivity was not observed through the declared policy")
    denied_log = denied.get("log")
    if (
        denied.get("expected") != "deny"
        or not isinstance(denied_log, str)
        or not denied_log.startswith("denied connection observed:")
    ):
        raise ConformanceError("denied connectivity was not observed through the declared policy")
    for identity in (server, allowed, denied):
        if not isinstance(identity.get("uid"), str) or not identity["uid"]:
            raise ConformanceError("network connectivity evidence has no attributed Pod UID")
    return {
        "declaration": {
            "classification": "declared",
            "policies": [dict(item) for item in declaration],
        },
        "enforcement": {
            "classification": "enforced",
            "timeout_seconds": _NETWORK_TIMEOUT_SECONDS,
            "server_pod_uid": server["uid"],
            "allowed_client_pod_uid": allowed["uid"],
            "denied_client_pod_uid": denied["uid"],
            "allowed": "observed",
            "denied": "observed",
        },
    }


class Kubectl:
    def __init__(self, kubeconfig: Path, namespace: str) -> None:
        self.kubeconfig = kubeconfig
        self.namespace = namespace

    def run(
        self, *args: str, input: str | None = None, allow_not_found: bool = False
    ) -> str | None:
        argv = (
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            CONTEXT,
            "--request-timeout=15s",
            *args,
        )
        try:
            result = subprocess.run(
                argv, input=input, text=True, capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConformanceError("bounded explicit kubectl command did not complete") from error
        if len(result.stdout) + len(result.stderr) > _MAX_OUTPUT:
            raise ConformanceError("bounded explicit kubectl command exceeded output limit")
        diagnostic = result.stderr.strip() or result.stdout.strip()
        if result.returncode:
            if allow_not_found and (
                "(NotFound)" in diagnostic or " not found" in diagnostic.lower()
            ):
                return None
            raise ConformanceError(f"explicit kubectl command failed: {diagnostic}")
        return result.stdout

    def get(self, kind: str, name: str, *, cluster_scoped: bool = False) -> Mapping[str, Any]:
        args = ["get", kind, name, "-o", "json"]
        if not cluster_scoped:
            args.extend(("--namespace", self.namespace))
        value = self.run(*args)
        try:
            result = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ConformanceError(f"{kind}/{name} returned malformed JSON") from error
        if not isinstance(result, Mapping):
            raise ConformanceError(f"{kind}/{name} returned malformed JSON")
        return result

    def is_absent(self, kind: str, name: str, *, cluster_scoped: bool = False) -> bool:
        args = ["get", kind, name, "-o", "name"]
        if not cluster_scoped:
            args.extend(("--namespace", self.namespace))
        return self.run(*args, allow_not_found=True) is None

    def create(
        self, manifest: Mapping[str, object], *, cluster_scoped: bool = False
    ) -> Mapping[str, Any]:
        args = ["create", "-f", "-", "-o", "json"]
        if not cluster_scoped:
            args.extend(("--namespace", self.namespace))
        value = self.run(*args, input=json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        try:
            result = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ConformanceError("probe resource create returned malformed JSON") from error
        if not isinstance(result, Mapping):
            raise ConformanceError("probe resource create returned malformed JSON")
        return result

    def logs(self, name: str) -> str:
        value = self.run("logs", name, "--namespace", self.namespace, "--tail=-1")
        if not isinstance(value, str):
            raise ConformanceError(f"Pod/{name} logs were unavailable")
        return value

    def delete_owned(
        self, kind: str, name: str, run_id: str, expected_uid: str, *, cluster_scoped: bool = False
    ) -> str:
        resource = self.get(kind, name, cluster_scoped=cluster_scoped)
        if not _owned(resource, run_id):
            raise ConformanceError(f"refusing to delete unowned {kind}/{name}")
        resource_kind = {
            "networkpolicy": "NetworkPolicy",
            "pod": "Pod",
            "pvc": "PersistentVolumeClaim",
            "namespace": "Namespace",
        }[kind]
        if (
            _identity(resource, resource_kind, name, None if cluster_scoped else self.namespace)[
                "uid"
            ]
            != expected_uid
        ):
            raise ConformanceError(f"refusing to delete replaced {kind}/{name}")
        args = ["delete", kind, name, "--wait=true", "--timeout=60s"]
        if not cluster_scoped:
            args.extend(("--namespace", self.namespace))
        self.run(*args)
        return "deleted_uid_fenced"

    def list_owned(self, kind: str, run_id: str) -> list[Mapping[str, Any]]:
        value = self.run(
            "get",
            kind,
            "--namespace",
            self.namespace,
            "-l",
            f"tessera.io/conformance-run={run_id}",
            "-o",
            "json",
        )
        try:
            items = json.loads(value)["items"]
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise ConformanceError(f"{kind} list returned malformed JSON") from error
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise ConformanceError(f"{kind} list returned malformed JSON")
        return list(items)


def _wait_for_pod(kubectl: Kubectl, name: str, wanted: str = "Succeeded") -> Mapping[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        resource = kubectl.get("pod", name)
        status = resource.get("status")
        phase = status.get("phase") if isinstance(status, Mapping) else None
        if phase == wanted:
            return resource
        if phase == "Failed":
            raise ConformanceError(f"probe Pod/{name} failed before {wanted}")
        time.sleep(1)
    raise ConformanceError(f"probe Pod/{name} did not reach {wanted} within 60 seconds")


def _wait_ready(kubectl: Kubectl, name: str) -> Mapping[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        resource = kubectl.get("pod", name)
        status = resource.get("status")
        conditions = status.get("conditions") if isinstance(status, Mapping) else None
        if isinstance(conditions, list) and any(
            isinstance(condition, Mapping)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        ):
            return resource
        if isinstance(status, Mapping) and status.get("phase") == "Failed":
            raise ConformanceError(f"probe Pod/{name} failed before becoming ready")
        time.sleep(1)
    raise ConformanceError(f"probe Pod/{name} did not become ready within 60 seconds")


def _wait_for_bound_pvc(kubectl: Kubectl, name: str) -> Mapping[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pvc = kubectl.get("pvc", name)
        status = pvc.get("status")
        if isinstance(status, Mapping) and status.get("phase") == "Bound":
            return pvc
        time.sleep(1)
    raise UnsupportedQuota(
        "new probe PVC did not bind to an enforceable quota-capable target within 60 seconds"
    )


def _probe_record(kubectl: Kubectl, name: str) -> Mapping[str, Any]:
    log = kubectl.logs(name).strip()
    try:
        record = json.loads(log)
    except json.JSONDecodeError as error:
        raise ConformanceError(f"probe Pod/{name} did not emit one JSON evidence record") from error
    if not isinstance(record, Mapping):
        raise ConformanceError(f"probe Pod/{name} emitted malformed evidence")
    return record


def validate_live_args(args: argparse.Namespace) -> tuple[Path, Path]:
    if not args.run:
        raise ConformanceError("--run is required; this fixture has no ambient live mode")
    if args.context != CONTEXT:
        raise ConformanceError("only the explicit docker-desktop context is permitted")
    kubeconfig = Path(args.kubeconfig).expanduser()
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise ConformanceError("--kubeconfig must name an existing absolute file")
    _name(args.probe_namespace, "--probe-namespace")
    _name(args.probe_pvc, "--probe-pvc")
    _name(args.storage_class, "--storage-class")
    storage = _storage_request(args.pvc_storage)
    byte_quota = _positive_bound(args.byte_quota_bytes, "--byte-quota-bytes", _MAX_BYTE_QUOTA)
    _positive_bound(args.inode_quota_files, "--inode-quota-files", _MAX_INODE_QUOTA)
    if byte_quota > storage:
        raise ConformanceError(
            "--byte-quota-bytes cannot exceed the explicit --pvc-storage request"
        )
    evidence = Path(args.evidence).expanduser()
    if (
        not evidence.is_absolute()
        or evidence.exists()
        or evidence.is_symlink()
        or not evidence.parent.is_dir()
    ):
        raise ConformanceError("--evidence must name a new file in an existing absolute directory")
    _private_evidence_parent(evidence.parent)
    return kubeconfig.resolve(), evidence


def _private_evidence_parent(parent: Path) -> None:
    """Require fixture evidence to stay in an operator-owned private directory.

    The evidence names a live target and created resource identities.  Refusing a
    symlink or shared parent prevents the runner from following an attacker-chosen
    output path before the bounded probe has started.
    """
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise ConformanceError("--evidence parent could not be inspected") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ConformanceError(
            "--evidence parent must be an existing private non-symlink directory "
            "owned by the operator"
        )


def _write_evidence(path: Path, value: Mapping[str, object]) -> None:
    encoded = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    _private_evidence_parent(path.parent)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(path.parent, directory_flags)
    except OSError as error:
        raise ConformanceError("--evidence parent could not be opened safely") from error
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
        except OSError as error:
            raise ConformanceError("--evidence could not be created safely") from error
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise ConformanceError("--evidence could not be written completely")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _add_created(
    created: list[tuple[str, str, str, bool]],
    result: dict[str, object],
    kind: str,
    identity: dict[str, str],
    cluster_scoped: bool = False,
) -> None:
    created.append((kind, identity["name"], identity["uid"], cluster_scoped))
    result["resources_created"].append({"kind": kind, **identity})  # type: ignore[index]


def _assert_no_owned_resources(kubectl: Kubectl, run_id: str) -> list[dict[str, str]]:
    survivors: list[dict[str, str]] = []
    for kind in ("pod", "networkpolicy", "pvc"):
        for resource in kubectl.list_owned(kind, run_id):
            metadata = resource.get("metadata")
            if isinstance(metadata, Mapping) and isinstance(metadata.get("name"), str):
                survivors.append({"kind": kind, "name": metadata["name"]})
    if survivors:
        raise ConformanceError(f"owned probe resources remain after cleanup: {survivors!r}")
    return survivors


def live_run(args: argparse.Namespace) -> dict[str, object]:
    kubeconfig, evidence_path = validate_live_args(args)
    run_id = uuid.uuid4().hex[:16]
    prefix = f"tessera-quota-{run_id[:8]}"
    kubectl = Kubectl(kubeconfig, args.probe_namespace)
    result: dict[str, object] = {
        "profile": PROFILE,
        "run_id": run_id,
        "started_at": _now(),
        "context": CONTEXT,
        "probe_namespace": args.probe_namespace,
        "probe_pvc": args.probe_pvc,
        "declared_bounds": {
            "pvc_storage": args.pvc_storage,
            "byte_quota_bytes": args.byte_quota_bytes,
            "inode_quota_files": args.inode_quota_files,
        },
        "evidence_path": str(evidence_path),
        "resources_created": [],
        "cleanup": {},
        "limitations": [
            "PVC requested storage and filesystem free space are recorded only as "
            "identity/context, never treated as a hard quota.",
            "A StorageClass declaration is only a capability prerequisite; both "
            "in-container exhaustion probes must observe ENOSPC or EDQUOT.",
            "The Kubernetes delete API has no UID precondition. This fixture "
            "rechecks UID and ownership immediately before each narrowly named delete.",
        ],
    }
    created: list[tuple[str, str, str, bool]] = []
    try:
        storage_class = kubectl.get("storageclass", args.storage_class, cluster_scoped=True)
        result["storage_class_identity"] = _identity(
            storage_class, "StorageClass", args.storage_class
        )
        result["quota_capability"] = quota_capability(
            storage_class, args.byte_quota_bytes, args.inode_quota_files
        )
        namespace = kubectl.create(
            namespace_manifest(args.probe_namespace, run_id), cluster_scoped=True
        )
        namespace_identity = _identity(namespace, "Namespace", args.probe_namespace)
        _add_created(created, result, "namespace", namespace_identity, True)
        pvc = kubectl.create(
            pvc_manifest(
                namespace=args.probe_namespace,
                name=args.probe_pvc,
                run_id=run_id,
                storage_class=args.storage_class,
                storage=args.pvc_storage,
            )
        )
        pvc_identity = _identity(pvc, "PersistentVolumeClaim", args.probe_pvc, args.probe_namespace)
        _add_created(created, result, "pvc", pvc_identity)
        image = build_and_load_exact_image(Path(__file__).resolve().parents[1], run_id)
        result["probe_image"] = {
            "reference": image.reference,
            "manifest_digest": image.manifest_digest,
            "config_digest": image.config_digest,
            "archive_sha256": image.archive_sha256,
        }
        # A first Pod is required for WaitForFirstConsumer StorageClasses to bind
        # the new PVC. It also becomes the durability writer we later remount.
        sentinel = f"tessera-quota-{run_id}"
        root = f"/outputs/{run_id}"
        writer_name = f"{prefix}-sentinel-write"
        writer = kubectl.create(
            pod_manifest(
                name=writer_name,
                namespace=args.probe_namespace,
                run_id=run_id,
                role="sentinel-writer",
                image=image.reference,
                command=["--write-sentinel", f"{root}/sentinel", sentinel],
                pvc=args.probe_pvc,
            )
        )
        writer_identity = _identity(writer, "Pod", writer_name, args.probe_namespace)
        _add_created(created, result, "pod", writer_identity)
        result["sentinel_writer"] = {
            **_identity(
                _wait_for_pod(kubectl, writer_name), "Pod", writer_name, args.probe_namespace
            ),
            "checksum": kubectl.logs(writer_name).strip(),
        }
        pvc_bound = _wait_for_bound_pvc(kubectl, args.probe_pvc)
        spec = pvc_bound.get("spec")
        if (
            not isinstance(spec, Mapping)
            or not isinstance(spec.get("volumeName"), str)
            or not spec["volumeName"]
        ):
            raise UnsupportedQuota("new probe PVC did not report a bound PersistentVolume")
        pv = kubectl.get("pv", spec["volumeName"], cluster_scoped=True)
        result["storage_identity"] = {
            "namespace": namespace_identity,
            "pvc": _identity(
                pvc_bound, "PersistentVolumeClaim", args.probe_pvc, args.probe_namespace
            ),
            "pv": _identity(pv, "PersistentVolume", spec["volumeName"]),
            "storage_class": _identity(storage_class, "StorageClass", args.storage_class),
            "requested_storage": args.pvc_storage,
            "observed_capacity": pvc_bound.get("status", {}).get("capacity", {}).get("storage")
            if isinstance(pvc_bound.get("status"), Mapping)
            and isinstance(pvc_bound["status"].get("capacity"), Mapping)
            else None,
        }
        result["cleanup"][writer_name] = kubectl.delete_owned(
            "pod", writer_name, run_id, writer_identity["uid"]
        )  # type: ignore[index]
        created.remove(("pod", writer_name, writer_identity["uid"], False))
        network_declaration: list[dict[str, str]] = []
        for manifest in network_manifests(args.probe_namespace, run_id, prefix):
            name = str(manifest["metadata"]["name"])  # type: ignore[index]
            policy = kubectl.create(manifest)
            identity = _identity(policy, "NetworkPolicy", name, args.probe_namespace)
            network_declaration.append(identity)
            _add_created(
                created,
                result,
                "networkpolicy",
                identity,
            )
        server_name = f"{prefix}-network-server"
        server = kubectl.create(
            pod_manifest(
                name=server_name,
                namespace=args.probe_namespace,
                run_id=run_id,
                role="network-server",
                image=image.reference,
                command=["--http-server"],
            )
        )
        _add_created(
            created,
            result,
            "pod",
            _identity(server, "Pod", server_name, args.probe_namespace),
        )
        server_ready = _wait_ready(kubectl, server_name)
        result["network_server"] = _identity(server_ready, "Pod", server_name, args.probe_namespace)
        status = server_ready.get("status")
        pod_ip = status.get("podIP") if isinstance(status, Mapping) else None
        if not isinstance(pod_ip, str) or not pod_ip:
            raise ConformanceError("ready network server did not report a Pod IP")
        network_clients: dict[str, dict[str, str]] = {}
        for expectation in ("allow", "deny"):
            name = f"{prefix}-network-{expectation}"
            role = "network-allowed" if expectation == "allow" else "network-denied"
            client = kubectl.create(
                pod_manifest(
                    name=name,
                    namespace=args.probe_namespace,
                    run_id=run_id,
                    role=role,
                    image=image.reference,
                    command=["--http-probe", f"{pod_ip}:8080", expectation],
                )
            )
            _add_created(
                created,
                result,
                "pod",
                _identity(client, "Pod", name, args.probe_namespace),
            )
            network_clients[expectation] = {
                **_identity(_wait_for_pod(kubectl, name), "Pod", name, args.probe_namespace),
                "expected": expectation,
                "log": kubectl.logs(name).strip(),
            }
            result[f"network_{expectation}"] = network_clients[expectation]
        result["network_policy_evidence"] = classify_network_enforcement(
            declaration=network_declaration,
            server=result["network_server"],  # type: ignore[arg-type]
            allowed=network_clients["allow"],
            denied=network_clients["deny"],
        )
        for kind, command in (
            ("byte", ["--probe-byte-quota", f"{root}/byte-payload", str(args.byte_quota_bytes)]),
            (
                "inode",
                ["--probe-inode-quota", f"{root}/inode-payload", str(args.inode_quota_files)],
            ),
        ):
            name = f"{prefix}-{kind}-quota"
            pod = kubectl.create(
                pod_manifest(
                    name=name,
                    namespace=args.probe_namespace,
                    run_id=run_id,
                    role=f"{kind}-quota-probe",
                    image=image.reference,
                    command=command,
                    pvc=args.probe_pvc,
                )
            )
            _add_created(created, result, "pod", _identity(pod, "Pod", name, args.probe_namespace))
            _wait_for_pod(kubectl, name)
            bound = args.byte_quota_bytes if kind == "byte" else args.inode_quota_files
            classified = classify_quota_evidence(kind, _probe_record(kubectl, name), bound)
            result[f"{kind}_quota"] = classified
            if classified["classification"] != "enforced":
                raise UnsupportedQuota(f"{kind} quota was not enforced beyond the declared bound")
        reader_name = f"{prefix}-sentinel-remount"
        reader = kubectl.create(
            pod_manifest(
                name=reader_name,
                namespace=args.probe_namespace,
                run_id=run_id,
                role="sentinel-remount-reader",
                image=image.reference,
                command=["--verify-sentinel", f"{root}/sentinel", sentinel],
                pvc=args.probe_pvc,
            )
        )
        _add_created(
            created, result, "pod", _identity(reader, "Pod", reader_name, args.probe_namespace)
        )
        result["sentinel_remount"] = {
            **_identity(
                _wait_for_pod(kubectl, reader_name), "Pod", reader_name, args.probe_namespace
            ),
            "checksum": kubectl.logs(reader_name).strip(),
        }
        result["pvc_durability"] = (
            "observed_sentinel_and_checksum_after_probe_pod_restart_and_remount"
        )
        result["state"] = "passed"
    except UnsupportedQuota as error:
        result["state"] = "unsupported"
        result["error"] = str(error)
    except (ConformanceError, LiveKubernetesError, OSError, ValueError) as error:
        result["state"] = "failed"
        result["error"] = str(error)
    finally:
        namespace_entry = next((entry for entry in created if entry[0] == "namespace"), None)
        for kind, name, expected_uid, cluster_scoped in reversed(created):
            if kind == "namespace":
                continue
            try:
                result["cleanup"][name] = kubectl.delete_owned(
                    kind, name, run_id, expected_uid, cluster_scoped=cluster_scoped
                )  # type: ignore[index]
            except ConformanceError as error:
                result["cleanup"][name] = f"failed: {error}"  # type: ignore[index]
                result["state"] = "failed"
        if namespace_entry is not None and result["state"] != "failed":
            try:
                result["owned_resources_after_cleanup"] = _assert_no_owned_resources(
                    kubectl, run_id
                )
            except ConformanceError as error:
                result["cleanup_verification"] = f"failed: {error}"
                result["state"] = "failed"
        if namespace_entry is not None and result["state"] != "failed":
            _, namespace_name, namespace_uid, _ = namespace_entry
            try:
                result["cleanup"][namespace_name] = kubectl.delete_owned(
                    "namespace",
                    namespace_name,
                    run_id,
                    namespace_uid,
                    cluster_scoped=True,
                )
            except ConformanceError as error:
                result["cleanup"][namespace_name] = f"failed: {error}"
                result["state"] = "failed"
        if namespace_entry is not None and result["state"] != "failed":
            _, namespace_name, namespace_uid, _ = namespace_entry
            if not kubectl.is_absent("namespace", namespace_name, cluster_scoped=True):
                result["cleanup_verification"] = (
                    f"failed: Namespace/{namespace_name} remained after UID-fenced cleanup"
                )
                result["state"] = "failed"
            else:
                result["namespace_cleanup"] = {
                    "name": namespace_name,
                    "uid": namespace_uid,
                    "status": "absent",
                }
        elif namespace_entry is not None:
            result["namespace_cleanup"] = "not deleted after failed UID-fenced cleanup"
        result["finished_at"] = _now()
        _write_evidence(evidence_path, result)
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run", action="store_true", help="perform the bounded live probe")
    value.add_argument("--kubeconfig", required=True, help="existing absolute kubeconfig path")
    value.add_argument("--context", required=True, choices=[CONTEXT])
    value.add_argument(
        "--probe-namespace", required=True, help="new, uniquely named Namespace owned by this probe"
    )
    value.add_argument(
        "--probe-pvc", required=True, help="new, uniquely named PVC owned by this probe"
    )
    value.add_argument(
        "--storage-class",
        required=True,
        help="explicit StorageClass with filesystem-quota-v1 annotations",
    )
    value.add_argument(
        "--pvc-storage", required=True, help="explicit new PVC request, for example 8Mi"
    )
    value.add_argument(
        "--byte-quota-bytes", required=True, type=int, help="declared enforced byte quota (1..64Mi)"
    )
    value.add_argument(
        "--inode-quota-files",
        required=True,
        type=int,
        help="declared enforced inode quota (1..10000)",
    )
    value.add_argument("--evidence", required=True, help="new absolute JSON evidence path")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = live_run(args)
    except ConformanceError as error:
        print(
            json.dumps({"profile": PROFILE, "state": "failed", "error": str(error)}, sort_keys=True)
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
