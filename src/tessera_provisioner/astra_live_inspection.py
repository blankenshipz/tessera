"""Bounded, read-only live inspection for an explicitly selected Kubernetes target.

This is evidence collection only.  It deliberately has no reconciliation or apply
API.  The command allowlist is closed and every request names its kubeconfig,
context, namespace, and resource names.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .astra_installation import (
    InstallationFinding,
    InstallationIdentity,
    InstallationInspection,
    InstallationPlan,
    ManagerConflict,
    ObservedInstallation,
    OwnershipConflict,
    ResourcePrecondition,
    TargetIdentity,
)
from .contracts import digest

LIVE_INSPECTION_VERSION = "astra-live-inspection-v1"
MAX_KUBECTL_OUTPUT_BYTES = 65_536
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_RESOURCE = re.compile(
    r"(deployment|persistentvolumeclaim|networkpolicy|serviceaccount)/([a-z0-9]([-a-z0-9]*[a-z0-9])?)\Z"
)
_SENSITIVE = re.compile(r"(?:token|secret|password|credential|key)", re.IGNORECASE)
_SAFE_STATUS = ("phase", "availableReplicas", "readyReplicas", "observedGeneration")


class LiveInspectionError(RuntimeError):
    """A live inspection was unsafe, incomplete, or did not match its plan."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    not_found: bool = False


Runner = Callable[[tuple[str, ...], float], CommandResult]


def _redact(value: str) -> str:
    """Do not publish stderr which can contain auth-plugin diagnostics or tokens."""
    return "[kubectl stderr redacted]" if value else ""


def subprocess_runner(argv: tuple[str, ...], timeout: float) -> CommandResult:
    """Capture each kubectl stream with a hard bound, not a truncation after read."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise LiveInspectionError("bounded kubectl inspection command failed") from error
    stdout, stderr = bytearray(), bytearray()
    exceeded = threading.Event()

    def collect(pipe: object, output: bytearray) -> None:
        assert hasattr(pipe, "read")
        while not exceeded.is_set():
            chunk = pipe.read(min(8192, MAX_KUBECTL_OUTPUT_BYTES + 1 - len(output)))
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > MAX_KUBECTL_OUTPUT_BYTES:
                exceeded.set()
                process.kill()
                return

    readers = [
        threading.Thread(target=collect, args=(process.stdout, stdout)),
        threading.Thread(target=collect, args=(process.stderr, stderr)),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    while process.poll() is None and not exceeded.is_set():
        if time.monotonic() >= deadline:
            process.kill()
            process.wait()
            for reader in readers:
                reader.join()
            raise LiveInspectionError("bounded kubectl inspection command failed")
        time.sleep(0.01)
    process.wait()
    for reader in readers:
        reader.join()
    if exceeded.is_set():
        raise LiveInspectionError("kubectl inspection output exceeded its byte bound")
    stdout_text = bytes(stdout).decode("utf-8", errors="replace")
    stderr_text = bytes(stderr).decode("utf-8", errors="replace")
    # Classify before redaction: auth/plugin stderr is never retained, but a
    # Kubernetes 404 is a normal inspected absence rather than an unsafe failure.
    not_found = process.returncode != 0 and "notfound" in stderr_text.lower()
    return CommandResult(argv, process.returncode, stdout_text, _redact(stderr_text), not_found)


def _name(value: str, field: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise LiveInspectionError(f"{field} must be a bounded Kubernetes name")
    return value


def _resource(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not (matched := _RESOURCE.fullmatch(value)):
        raise LiveInspectionError("resources must be explicit allowed TYPE/NAME values")
    return matched.group(1), matched.group(2)


def _safe_text(value: object) -> str:
    if not isinstance(value, str) or len(value) > 256 or _SENSITIVE.search(value):
        return "[redacted]"
    return value


def _metadata(value: Mapping[str, Any]) -> dict[str, object]:
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise LiveInspectionError("Kubernetes response is missing metadata")
    uid, version = metadata.get("uid"), metadata.get("resourceVersion")
    if not isinstance(uid, str) or not uid or not isinstance(version, str) or not version:
        raise LiveInspectionError("Kubernetes response is missing UID or resourceVersion")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(labels, Mapping) or not isinstance(annotations, Mapping):
        raise LiveInspectionError("Kubernetes metadata labels or annotations are invalid")
    deletion_timestamp = metadata.get("deletionTimestamp")
    if deletion_timestamp is not None and (
        not isinstance(deletion_timestamp, str) or len(deletion_timestamp) > 64
    ):
        raise LiveInspectionError("Kubernetes metadata lifecycle is invalid")
    safe_labels = {
        str(k): _safe_text(v) for k, v in labels.items() if not _SENSITIVE.search(str(k))
    }
    safe_annotations = {
        str(k): _safe_text(v) for k, v in annotations.items() if not _SENSITIVE.search(str(k))
    }
    managers = metadata.get("managedFields", [])
    if not isinstance(managers, list):
        managers = []
    safe_managers = sorted(
        {
            _safe_text(item.get("manager"))
            for item in managers
            if isinstance(item, Mapping) and isinstance(item.get("manager"), str)
        }
    )
    return {
        "annotations": safe_annotations,
        "labels": safe_labels,
        "managers": safe_managers,
        "resource_version": version,
        "uid": uid,
        "lifecycle": "terminating" if deletion_timestamp else "active",
    }


def _conflict_reference(value: object, kind: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        return value
    return f"unresolved-{kind}"


def _operationally_verified(value: Mapping[str, Any]) -> bool:
    """Return true only for the one status condition this inspector understands."""
    if value.get("kind") != "Deployment":
        return False
    spec, status = value.get("spec"), value.get("status")
    replicas = spec.get("replicas", 1) if isinstance(spec, Mapping) else None
    available = status.get("availableReplicas") if isinstance(status, Mapping) else None
    return type(replicas) is int and replicas >= 0 and available == replicas


def _precondition(identity: str, value: Mapping[str, Any] | None) -> ResourcePrecondition:
    if value is None:
        return ResourcePrecondition(identity, "absent")
    metadata = _metadata(value)
    state = "operationally_verified" if _operationally_verified(value) else "present"
    return ResourcePrecondition(
        identity,
        state,
        str(metadata["uid"]),
        str(metadata["resource_version"]),
        digest(value.get("spec", {})),
        metadata["lifecycle"],  # type: ignore[arg-type]
    )


def _config_identity(kubeconfig: Path, context: str) -> tuple[str, str, str]:
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise LiveInspectionError("kubeconfig must be an existing absolute file")
    try:
        parsed = yaml.safe_load(kubeconfig.read_bytes())
    except (OSError, yaml.YAMLError) as error:
        raise LiveInspectionError("kubeconfig cannot be safely read") from error
    if not isinstance(parsed, Mapping):
        raise LiveInspectionError("kubeconfig is invalid")
    contexts = {
        item.get("name"): item.get("context")
        for item in parsed.get("contexts", [])
        if isinstance(item, Mapping)
    }
    selected = contexts.get(context)
    if not isinstance(selected, Mapping) or not isinstance(selected.get("cluster"), str):
        raise LiveInspectionError("explicit context is absent from kubeconfig")
    clusters = {
        item.get("name"): item.get("cluster")
        for item in parsed.get("clusters", [])
        if isinstance(item, Mapping)
    }
    cluster = clusters.get(selected["cluster"])
    if not isinstance(cluster, Mapping) or not isinstance(cluster.get("server"), str):
        raise LiveInspectionError("selected kubeconfig cluster is invalid")
    server = cluster["server"]
    if not server.startswith("https://") or "@" in server:
        raise LiveInspectionError("selected kubeconfig server must be credential-free HTTPS")
    if (
        cluster.get("insecure-skip-tls-verify") is True
        or str(cluster.get("insecure-skip-tls-verify", "")).lower() == "true"
    ):
        raise LiveInspectionError("selected kubeconfig cluster cannot skip TLS verification")
    ca_data = cluster.get("certificate-authority-data")
    ca_file = cluster.get("certificate-authority")
    if bool(ca_data) == bool(ca_file):
        raise LiveInspectionError("selected kubeconfig cluster has no CA identity")
    try:
        if ca_data:
            if not isinstance(ca_data, str):
                raise ValueError
            ca = base64.b64decode(ca_data, validate=True)
        else:
            if not isinstance(ca_file, str) or not ca_file:
                raise ValueError
            path = Path(ca_file)
            if not path.is_absolute():
                path = kubeconfig.parent / path
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > MAX_KUBECTL_OUTPUT_BYTES
            ):
                raise ValueError
            ca = path.read_bytes()
    except (OSError, ValueError, base64.binascii.Error) as error:
        raise LiveInspectionError(
            "selected kubeconfig cluster has an invalid CA identity"
        ) from error
    if not ca:
        raise LiveInspectionError("selected kubeconfig cluster has no CA identity")
    return (
        hashlib.sha256(kubeconfig.read_bytes()).hexdigest(),
        server,
        hashlib.sha256(ca).hexdigest(),
    )


class LiveAstraInspector:
    """Run only an allowlisted set of GET requests through an injectable runner."""

    def __init__(
        self,
        kubeconfig: Path,
        context: str,
        *,
        runner: Runner = subprocess_runner,
        timeout: float = 10.0,
    ):
        self.kubeconfig = Path(kubeconfig)
        # Context labels commonly contain '/', '@', or dots.  The typed target
        # contract enforces a bounded non-control name instead of a DNS label.
        if (
            not isinstance(context, str)
            or not context
            or len(context) > 253
            or any(ord(character) < 32 or ord(character) == 127 for character in context)
        ):
            raise LiveInspectionError("context must be a bounded kubeconfig context name")
        self.context = context
        self.runner = runner
        if not 0 < timeout <= 30:
            raise LiveInspectionError("inspection timeout must be between zero and 30 seconds")
        self.timeout = timeout

    def _get(
        self, resource_type: str, name: str, namespace: str | None = None
    ) -> Mapping[str, Any] | None:
        if resource_type == "secret":
            raise LiveInspectionError("Secret inspection is forbidden")
        argv = [
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            self.context,
            "get",
            resource_type,
            name,
        ]
        if namespace is not None:
            argv.extend(("--namespace", namespace))
        argv.extend(("--output=json", "--request-timeout=10s"))
        result = self.runner(tuple(argv), self.timeout)
        if (
            len(result.stdout.encode("utf-8", errors="replace")) > MAX_KUBECTL_OUTPUT_BYTES
            or len(result.stderr.encode("utf-8", errors="replace")) > MAX_KUBECTL_OUTPUT_BYTES
        ):
            raise LiveInspectionError("kubectl inspection output exceeded its byte bound")
        if result.not_found or (result.returncode != 0 and "notfound" in result.stderr.lower()):
            return None
        if result.returncode != 0:
            raise LiveInspectionError("bounded kubectl GET failed; stderr redacted")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise LiveInspectionError("Kubernetes response was not JSON") from error
        expected_kind = {
            "namespace": "Namespace",
            "deployment": "Deployment",
            "persistentvolumeclaim": "PersistentVolumeClaim",
            "networkpolicy": "NetworkPolicy",
            "serviceaccount": "ServiceAccount",
        }.get(resource_type)
        metadata = value.get("metadata") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or value.get("kind") != expected_kind
            or not isinstance(metadata, Mapping)
            or metadata.get("name") != name
            or (resource_type == "namespace" and metadata.get("namespace") not in (None, ""))
            or (
                resource_type != "namespace"
                and (namespace is None or metadata.get("namespace") != namespace)
            )
        ):
            raise LiveInspectionError("unexpected or secret Kubernetes response")
        return value

    def inspect(
        self,
        installation: InstallationIdentity,
        namespace: str,
        installation_resource: str,
        resources: Sequence[str],
    ) -> dict[str, object]:
        namespace = _name(namespace, "namespace")
        installation_type, installation_name = _resource(installation_resource)
        parsed_resources = tuple(_resource(item) for item in resources)
        if len(parsed_resources) != len(set(parsed_resources)):
            raise LiveInspectionError("resources must be unique")
        config_digest, server, ca_digest = _config_identity(self.kubeconfig, self.context)
        cluster = self._get("namespace", "kube-system")
        target_namespace = self._get("namespace", namespace)
        if cluster is None or target_namespace is None:
            raise LiveInspectionError("cluster identity or target namespace was not found")
        cluster_metadata, namespace_metadata = _metadata(cluster), _metadata(target_namespace)
        if namespace_metadata["lifecycle"] != "active":
            raise LiveInspectionError("target namespace is terminating")
        target = TargetIdentity(
            config_digest,
            self.context,
            server,
            ca_digest,
            cluster_metadata["uid"],
            namespace,
            namespace_metadata["uid"],
        )
        objects: dict[str, Mapping[str, Any] | None] = {
            (installation_type + "/" + installation_name): self._get(
                installation_type, installation_name, namespace
            )
        }
        for kind, name in parsed_resources:
            objects.setdefault(kind + "/" + name, self._get(kind, name, namespace))
        conflicts: list[OwnershipConflict] = []
        managers: list[ManagerConflict] = []
        findings: list[InstallationFinding] = []
        installation_object = objects[installation_type + "/" + installation_name]
        observation = None
        if installation_object is not None:
            meta = _metadata(installation_object)
            observation = ObservedInstallation(
                installation,
                target,
                meta["uid"],
                digest(installation_object.get("spec", {})),
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                meta["resource_version"],
            )
        for identity, object_value in objects.items():
            kind, name = identity.split("/", 1)
            category = {
                "persistentvolumeclaim": "storage",
                "networkpolicy": "network",
                "serviceaccount": "permission",
            }.get(kind)
            if object_value is None:
                if category:
                    findings.append(InstallationFinding(category, "missing", name))
                continue
            meta = _metadata(object_value)
            owner = meta["labels"].get("app.kubernetes.io/managed-by") or meta["annotations"].get(
                "tessera.dev/owner"
            )
            if owner != "tessera":
                # Missing or redacted ownership cannot establish adoption authority.
                reference = _conflict_reference(owner, "owner")
                conflicts.append(
                    OwnershipConflict(installation, target, meta["uid"], reference, identity)
                )
            for manager in meta["managers"]:
                if manager not in {"tessera", "kube-controller-manager"}:
                    reference = _conflict_reference(manager, "manager")
                    managers.append(
                        ManagerConflict(installation, target, meta["uid"], reference, identity)
                    )
            if category:
                findings.append(
                    InstallationFinding(
                        category,
                        "unknown",
                        name,
                        meta["uid"],
                        digest(object_value.get("spec", {})),
                    )
                )
        inspection = InstallationInspection(
            installation,
            target,
            observation,
            tuple(sorted(conflicts, key=lambda x: (x.resource_identity, x.owner_reference))),
            tuple(sorted(managers, key=lambda x: (x.resource_identity, x.manager_reference))),
            tuple(sorted(findings, key=lambda x: x.identity)),
            tuple(
                sorted(
                    (_precondition(key, value) for key, value in objects.items()),
                    key=lambda x: x.resource_identity,
                )
            ),
        )
        return {
            "evidence": "live_inspection_not_apply_readiness",
            "inspection": inspection.payload(),
            "namespace_metadata": namespace_metadata,
            "resources": {key: _safe_resource(value) for key, value in objects.items()},
            "version": LIVE_INSPECTION_VERSION,
        }


def _safe_resource(value: Mapping[str, Any] | None) -> object:
    if value is None:
        return {"state": "missing"}
    metadata = _metadata(value)
    status = value.get("status", {})
    safe_status = {
        key: status[key] for key in _SAFE_STATUS if isinstance(status, Mapping) and key in status
    }
    return {
        "metadata": metadata,
        "lifecycle": metadata["lifecycle"],
        "state": "operationally_verified" if _operationally_verified(value) else "present",
        "spec_sha256": digest(value.get("spec", {})),
        "status": safe_status,
    }


def revalidate_plan(
    plan: InstallationPlan, live_evidence: Mapping[str, object], now: datetime
) -> None:
    """Fence a plan against fresh evidence; this authorizes no mutation."""
    plan.assert_current()
    plan.assert_approval_current(now)
    inspection_value = live_evidence.get("inspection")
    if not isinstance(inspection_value, Mapping):
        raise LiveInspectionError("live inspection evidence is malformed")
    # The offline decoder deliberately refuses live evidence labels; reconstruct only
    # the typed fields after asserting that this evidence came from this module.
    if live_evidence.get("evidence") != "live_inspection_not_apply_readiness":
        raise LiveInspectionError("live inspection evidence label is missing")
    from .astra_installation import inspection_from_payload

    live = inspection_from_payload({**inspection_value, "verification": "offline_unverified"})
    plan.assert_target_unchanged(live.target)
    if not plan.inspection.resources:
        raise LiveInspectionError("plan did not bind a complete inspected resource inventory")
    if plan.inspection.resources != live.resources:
        raise LiveInspectionError(
            "live inspected resource inventory was omitted, replaced, or drifted"
        )
    if live.mutation_blockers and plan.mode != "observe":
        raise LiveInspectionError("live ownership, manager, or resource conflict blocks mutation")
    if (plan.inspection.observation is None) != (live.observation is None):
        raise LiveInspectionError("live installation presence differs from plan")
    if plan.inspection.observation and live.observation:
        if (
            plan.inspection.observation.uid,
            plan.inspection.observation.resource_version,
            plan.inspection.observation.spec_sha256,
        ) != (
            live.observation.uid,
            live.observation.resource_version,
            live.observation.spec_sha256,
        ):
            raise LiveInspectionError(
                "live installation UID, resourceVersion, or spec digest drifted"
            )
