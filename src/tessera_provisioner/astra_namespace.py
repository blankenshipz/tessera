"""Narrow, operator-only lifecycle for one Kubernetes Namespace.

This is deliberately not an installer.  It reads exactly named Namespace objects
and, after an exact unexpired plan approval, can create one generated Namespace
object.  It never creates or reads Secrets and has no cleanup operation.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .astra_installation import _context, _name, _timestamp, _uid
from .astra_live_inspection import CommandResult, LiveInspectionError, _config_identity
from .contracts import digest

NAMESPACE_LIFECYCLE_VERSION = "astra-namespace-lifecycle-v1"
_MAX_OUTPUT = 65_536
NamespaceMode = Literal["use-existing", "create-dedicated"]
NamespaceState = Literal["absent", "present", "terminating"]
Runner = Callable[[tuple[str, ...], float, bytes | None], CommandResult]


class NamespaceLifecycleError(ValueError):
    """A namespace lifecycle request was incomplete, stale, or unsafe."""


def _bounded_json(value: str) -> Mapping[str, object]:
    if len(value.encode("utf-8", errors="replace")) > _MAX_OUTPUT:
        raise NamespaceLifecycleError("namespace command output exceeded its byte bound")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise NamespaceLifecycleError("namespace command did not return JSON") from error
    if not isinstance(decoded, Mapping):
        raise NamespaceLifecycleError("namespace command returned an invalid object")
    return decoded


def subprocess_namespace_runner(
    argv: tuple[str, ...], timeout: float, stdin: bytes | None
) -> CommandResult:
    try:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LiveInspectionError("namespace command response is uncertain") from error
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if len(completed.stdout) > _MAX_OUTPUT or len(completed.stderr) > _MAX_OUTPUT:
        raise LiveInspectionError("namespace command output exceeded its byte bound")
    return CommandResult(
        argv,
        completed.returncode,
        stdout,
        "[kubectl stderr redacted]" if stderr else "",
        completed.returncode != 0 and "notfound" in stderr.lower(),
    )


@dataclass(frozen=True)
class NamespaceTarget:
    kubeconfig_reference_sha256: str
    context: str
    server: str
    ca_sha256: str
    cluster_uid: str
    namespace: str

    def __post_init__(self) -> None:
        from .astra_installation import _https_server, _sha256

        _sha256(self.kubeconfig_reference_sha256, "kubeconfig reference")
        _context(self.context)
        _https_server(self.server)
        _sha256(self.ca_sha256, "target CA")
        _uid(self.cluster_uid, "cluster UID")
        _name(self.namespace, "namespace")

    def payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class NamespaceInspection:
    target: NamespaceTarget
    state: NamespaceState
    uid: str | None = None
    resource_version: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"absent", "present", "terminating"}:
            raise NamespaceLifecycleError("namespace inspection state is unsupported")
        if self.state == "absent":
            if self.uid is not None or self.resource_version is not None:
                raise NamespaceLifecycleError("an absent namespace cannot have a receipt")
        elif not isinstance(self.uid, str) or not isinstance(self.resource_version, str):
            raise NamespaceLifecycleError("a present namespace requires UID and resourceVersion")
        if self.uid is not None:
            _uid(self.uid, "namespace UID")
        if self.resource_version is not None and (
            not self.resource_version or len(self.resource_version) > 256
        ):
            raise NamespaceLifecycleError("namespace resourceVersion must be bounded")

    def payload(self) -> dict[str, object]:
        return {
            "resource_version": self.resource_version,
            "state": self.state,
            "target": self.target.payload(),
            "uid": self.uid,
        }


@dataclass(frozen=True)
class NamespacePlan:
    mode: NamespaceMode
    inspection: NamespaceInspection
    installation: str | None
    operation: str | None
    approval_expires_at: str
    digest: str

    def __post_init__(self) -> None:
        if self.mode not in {"use-existing", "create-dedicated"}:
            raise NamespaceLifecycleError("namespace plan mode is unsupported")
        if self.mode == "use-existing":
            if (
                self.inspection.state != "present"
                or self.installation is not None
                or self.operation is not None
            ):
                raise NamespaceLifecycleError(
                    "use-existing requires an explicitly active existing namespace"
                )
        else:
            if self.inspection.state != "absent":
                raise NamespaceLifecycleError(
                    "create-dedicated requires an explicitly absent namespace"
                )
            _name(self.installation, "installation")
            _name(self.operation, "operation")
        _timestamp(self.approval_expires_at, "approval_expires_at")
        if self.digest != digest(self.payload()):
            raise NamespaceLifecycleError(
                "namespace plan digest does not match its immutable payload"
            )

    @classmethod
    def create(
        cls,
        *,
        mode: NamespaceMode,
        inspection: NamespaceInspection,
        approval_expires_at: str,
        installation: str | None = None,
        operation: str | None = None,
    ) -> NamespacePlan:
        payload = {
            "approval_expires_at": _timestamp(approval_expires_at, "approval_expires_at"),
            "inspection": inspection.payload(),
            "installation": installation,
            "mode": mode,
            "operation": operation,
            "version": NAMESPACE_LIFECYCLE_VERSION,
        }
        return cls(
            mode,
            inspection,
            installation,
            operation,
            payload["approval_expires_at"],
            digest(payload),
        )

    def payload(self) -> dict[str, object]:
        return {
            "approval_expires_at": _timestamp(self.approval_expires_at, "approval_expires_at"),
            "inspection": self.inspection.payload(),
            "installation": self.installation,
            "mode": self.mode,
            "operation": self.operation,
            "version": NAMESPACE_LIFECYCLE_VERSION,
        }

    def assert_approved(self, approved_digest: str, now: datetime) -> None:
        if approved_digest != self.digest:
            raise NamespaceLifecycleError("namespace approval must name the exact plan digest")
        if now.tzinfo is None or now.utcoffset() is None:
            raise NamespaceLifecycleError(
                "namespace approval comparison time must include an offset"
            )
        if datetime.fromisoformat(
            self.approval_expires_at.replace("Z", "+00:00")
        ) <= now.astimezone(UTC):
            raise NamespaceLifecycleError("namespace plan approval has expired")

    @property
    def labels(self) -> dict[str, str]:
        if self.mode != "create-dedicated":
            return {}
        return {
            "app.kubernetes.io/managed-by": "tessera",
            "tessera.dev/installation": self.installation or "",
            "tessera.dev/operation": self.operation or "",
        }


@dataclass(frozen=True)
class NamespaceReceipt:
    plan_digest: str
    target: NamespaceTarget
    uid: str
    resource_version: str

    def __post_init__(self) -> None:
        _uid(self.uid, "namespace receipt UID")
        if not self.resource_version or len(self.resource_version) > 256:
            raise NamespaceLifecycleError("namespace receipt resourceVersion must be bounded")

    def payload(self) -> dict[str, object]:
        return {
            "plan_digest": self.plan_digest,
            "resource_version": self.resource_version,
            "target": self.target.payload(),
            "uid": self.uid,
        }


class NamespaceOperator:
    """Explicit-target Namespace reader/creator with no retry or cleanup behavior."""

    def __init__(
        self,
        kubeconfig: Path,
        context: str,
        *,
        runner: Runner = subprocess_namespace_runner,
        timeout: float = 10.0,
    ):
        self.kubeconfig, self.context, self.runner = Path(kubeconfig), _context(context), runner
        if not 0 < timeout <= 30:
            raise NamespaceLifecycleError("namespace timeout must be between zero and 30 seconds")
        self.timeout = timeout

    def _get(self, name: str) -> Mapping[str, object] | None:
        argv = (
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            self.context,
            "get",
            "namespace",
            name,
            "--output=json",
            "--request-timeout=10s",
        )
        result = self.runner(argv, self.timeout, None)
        if result.not_found:
            return None
        if result.returncode != 0:
            raise LiveInspectionError("namespace GET failed; stderr redacted")
        value = _bounded_json(result.stdout)
        metadata = value.get("metadata")
        if (
            value.get("kind") != "Namespace"
            or not isinstance(metadata, Mapping)
            or metadata.get("name") != name
            or metadata.get("namespace") not in (None, "")
        ):
            raise NamespaceLifecycleError("namespace GET returned a mismatched object")
        return value

    def inspect(self, namespace: str) -> NamespaceInspection:
        namespace = _name(namespace, "namespace")
        config_digest, server, ca_sha256 = _config_identity(self.kubeconfig, self.context)
        cluster = self._get("kube-system")
        if cluster is None:
            raise NamespaceLifecycleError("cluster identity namespace was not found")
        cluster_meta = cluster.get("metadata")
        if not isinstance(cluster_meta, Mapping) or not isinstance(cluster_meta.get("uid"), str):
            raise NamespaceLifecycleError("cluster identity is missing a UID")
        target = NamespaceTarget(
            config_digest, self.context, server, ca_sha256, cluster_meta["uid"], namespace
        )
        resource = self._get(namespace)
        if resource is None:
            return NamespaceInspection(target, "absent")
        metadata = resource["metadata"]
        assert isinstance(metadata, Mapping)
        lifecycle: NamespaceState = (
            "terminating" if metadata.get("deletionTimestamp") else "present"
        )
        return NamespaceInspection(
            target, lifecycle, metadata.get("uid"), metadata.get("resourceVersion")
        )

    def create(
        self, plan: NamespacePlan, approved_digest: str, now: datetime
    ) -> NamespaceReceipt | dict[str, str]:
        if plan.mode != "create-dedicated":
            raise NamespaceLifecycleError("only a create-dedicated plan can create a namespace")
        plan.assert_approved(approved_digest, now)
        # A duplicate or changed precondition is never adopted or retried.
        if self.inspect(plan.inspection.target.namespace) != plan.inspection:
            return {"plan_digest": plan.digest, "state": "reconciliation_required"}
        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"labels": plan.labels, "name": plan.inspection.target.namespace},
        }
        argv = (
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            self.context,
            "create",
            "--filename=-",
            "--output=json",
            "--request-timeout=10s",
        )
        try:
            result = self.runner(
                argv,
                self.timeout,
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            )
            if result.returncode != 0:
                return {"plan_digest": plan.digest, "state": "reconciliation_required"}
            value = _bounded_json(result.stdout)
            metadata = value.get("metadata")
            if (
                value.get("kind") != "Namespace"
                or not isinstance(metadata, Mapping)
                or metadata.get("name") != plan.inspection.target.namespace
                or metadata.get("namespace") not in (None, "")
                or any(metadata.get("labels", {}).get(k) != v for k, v in plan.labels.items())
            ):
                return {"plan_digest": plan.digest, "state": "reconciliation_required"}
            return NamespaceReceipt(
                plan.digest,
                plan.inspection.target,
                metadata.get("uid"),
                metadata.get("resourceVersion"),
            )
        except (LiveInspectionError, NamespaceLifecycleError, OSError):
            return {"plan_digest": plan.digest, "state": "reconciliation_required"}

    def status(
        self, plan: NamespacePlan, receipt: NamespaceReceipt | None = None
    ) -> dict[str, object]:
        observed = self.inspect(plan.inspection.target.namespace)
        if observed.target != plan.inspection.target:
            return {"plan_digest": plan.digest, "state": "target_drift"}
        if receipt is None:
            return {"plan_digest": plan.digest, "state": "reconciliation_required"}
        if (
            receipt.plan_digest != plan.digest
            or receipt.target != observed.target
            or observed.uid != receipt.uid
        ):
            return {"plan_digest": plan.digest, "state": "reconciliation_required"}
        resource = self._get(plan.inspection.target.namespace)
        metadata = resource.get("metadata") if resource is not None else None
        labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
        if not isinstance(labels, Mapping) or any(
            labels.get(key) != value for key, value in plan.labels.items()
        ):
            return {"plan_digest": plan.digest, "state": "reconciliation_required"}
        return {
            "plan_digest": plan.digest,
            "resource_version": observed.resource_version,
            "state": "namespace_created_control_plane_not_installed",
            "uid": observed.uid,
        }


def plan_from_payload(value: object) -> NamespacePlan:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "approval_expires_at",
            "digest",
            "inspection",
            "installation",
            "mode",
            "operation",
            "version",
        }
        or value["version"] != NAMESPACE_LIFECYCLE_VERSION
    ):
        raise NamespaceLifecycleError("namespace plan has unsupported or missing fields")
    inspection = value["inspection"]
    if not isinstance(inspection, Mapping) or set(inspection) != {
        "resource_version",
        "state",
        "target",
        "uid",
    }:
        raise NamespaceLifecycleError("namespace inspection has unsupported or missing fields")
    target = inspection["target"]
    if not isinstance(target, Mapping):
        raise NamespaceLifecycleError("namespace target is invalid")
    return NamespacePlan(
        value["mode"],  # type: ignore[arg-type]
        NamespaceInspection(
            NamespaceTarget(**target),
            inspection["state"],
            inspection["uid"],
            inspection["resource_version"],
        ),  # type: ignore[arg-type]
        value["installation"],  # type: ignore[arg-type]
        value["operation"],  # type: ignore[arg-type]
        value["approval_expires_at"],  # type: ignore[arg-type]
        value["digest"],  # type: ignore[arg-type]
    )


def plan_from_json(path: Path) -> NamespacePlan:
    try:
        if path.stat().st_size > _MAX_OUTPUT:
            raise NamespaceLifecycleError("namespace plan exceeds its byte bound")
        return plan_from_payload(json.loads(path.read_bytes()))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NamespaceLifecycleError("namespace plan is not valid JSON") from error


def receipt_from_json(path: Path) -> NamespaceReceipt:
    try:
        if path.stat().st_size > _MAX_OUTPUT:
            raise NamespaceLifecycleError("namespace receipt exceeds its byte bound")
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NamespaceLifecycleError("namespace receipt is not valid JSON") from error
    if (
        not isinstance(value, Mapping)
        or set(value) != {"plan_digest", "resource_version", "target", "uid"}
        or not isinstance(value["target"], Mapping)
    ):
        raise NamespaceLifecycleError("namespace receipt has unsupported or missing fields")
    return NamespaceReceipt(
        value["plan_digest"],
        NamespaceTarget(**value["target"]),
        value["uid"],
        value["resource_version"],
    )  # type: ignore[arg-type]


__all__ = [
    "NAMESPACE_LIFECYCLE_VERSION",
    "NamespaceInspection",
    "NamespaceLifecycleError",
    "NamespaceOperator",
    "NamespacePlan",
    "NamespaceReceipt",
    "NamespaceTarget",
    "plan_from_json",
    "plan_from_payload",
    "receipt_from_json",
]
