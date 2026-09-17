"""Astra's narrow, operator-only Docker Desktop Kubernetes setup boundary.

The module intentionally owns neither an execution target nor research authority.
It accepts an explicit kubeconfig/context, creates only the three resources named
by a versioned profile, and leaves them retained for operator reconciliation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import canonical_json

SETUP_VERSION = "astra-operator-setup-v2"
DOCKER_DESKTOP_PROFILE = "docker-desktop-kubernetes-v1"
DOCKER_DESKTOP_CONTEXT = "docker-desktop"
_NAME = re.compile(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_IMAGE = re.compile(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}\Z")
_RUNTIME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_MAX_OUTPUT = 16_384
_TIMEOUT = 30


class AstraSetupError(RuntimeError):
    """A setup precondition or bounded Kubernetes operation failed closed."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CommandResult]


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise AstraSetupError(f"{field} must be a bounded lowercase Kubernetes name")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AstraSetupError(f"{field} must be a SHA-256 digest")
    return value


@dataclass(frozen=True)
class SetupProfile:
    version: str
    name: str
    required_context: str
    namespace: str
    network_policy: str
    pvc: str
    pvc_storage: str
    pvc_storage_class: str
    pvc_volume_mode: str
    image: str
    runtime_identity: str
    retain_resources: bool = True

    def __post_init__(self) -> None:
        if self.version != SETUP_VERSION or self.name != DOCKER_DESKTOP_PROFILE:
            raise AstraSetupError("unsupported Astra setup profile version")
        if self.required_context != DOCKER_DESKTOP_CONTEXT:
            raise AstraSetupError("Docker Desktop setup requires the docker-desktop context")
        _name(self.namespace, "namespace")
        _name(self.network_policy, "network policy")
        _name(self.pvc, "PVC")
        if (
            self.pvc_storage != "64Mi"
            or self.pvc_storage_class != "standard"
            or self.pvc_volume_mode != "Filesystem"
            or not self.retain_resources
        ):
            raise AstraSetupError("only the retained 64Mi Astra setup profile is supported")
        if not isinstance(self.image, str) or not _IMAGE.fullmatch(self.image):
            raise AstraSetupError("image must be an exact repository@sha256 reference")
        if not isinstance(self.runtime_identity, str) or not _RUNTIME.fullmatch(
            self.runtime_identity
        ):
            raise AstraSetupError("runtime_identity must be a bounded identifier")

    @classmethod
    def docker_desktop(cls, namespace: str, image: str, runtime_identity: str) -> SetupProfile:
        return cls(
            SETUP_VERSION,
            DOCKER_DESKTOP_PROFILE,
            DOCKER_DESKTOP_CONTEXT,
            namespace,
            "astra-deny-all",
            "astra-outputs",
            "64Mi",
            "standard",
            "Filesystem",
            image,
            runtime_identity,
        )

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SetupDiscovery:
    version: str
    profile: str
    kubeconfig: str
    kubeconfig_sha256: str
    context: str
    server: str
    ca_sha256: str
    cluster_uid: str
    namespace_uid: str | None
    namespace_exists: bool

    def __post_init__(self) -> None:
        if self.version != SETUP_VERSION or self.profile != DOCKER_DESKTOP_PROFILE:
            raise AstraSetupError("unsupported setup discovery version")
        if not Path(self.kubeconfig).is_absolute() or self.context != DOCKER_DESKTOP_CONTEXT:
            raise AstraSetupError("discovery requires an explicit Docker Desktop target")
        if not self.server.startswith("https://"):
            raise AstraSetupError("target API server must use HTTPS")
        for value, field in ((self.kubeconfig_sha256, "kubeconfig"), (self.ca_sha256, "CA")):
            _sha256(value, field)
        if not self.cluster_uid or (self.namespace_exists != (self.namespace_uid is not None)):
            raise AstraSetupError("discovery identity is incomplete")

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SetupPlan:
    version: str
    profile: SetupProfile
    discovery: SetupDiscovery
    digest: str

    def __post_init__(self) -> None:
        if self.version != SETUP_VERSION or self.profile.name != self.discovery.profile:
            raise AstraSetupError("setup plan profile does not match discovery")
        _sha256(self.digest, "plan")
        if self.digest != _digest(self.payload()):
            raise AstraSetupError("setup plan digest does not match its immutable payload")

    def payload(self) -> dict[str, object]:
        return {
            "discovery": self.discovery.payload(),
            "profile": self.profile.payload(),
            "version": self.version,
        }

    @classmethod
    def create(cls, profile: SetupProfile, discovery: SetupDiscovery) -> SetupPlan:
        payload = {
            "discovery": discovery.payload(),
            "profile": profile.payload(),
            "version": SETUP_VERSION,
        }
        return cls(SETUP_VERSION, profile, discovery, _digest(payload))


def profile_catalog() -> tuple[dict[str, object], ...]:
    """List supported profiles without reading the host or Kubernetes."""
    return (
        {
            "version": SETUP_VERSION,
            "name": DOCKER_DESKTOP_PROFILE,
            "context": DOCKER_DESKTOP_CONTEXT,
            "resources": ["Namespace", "NetworkPolicy", "PersistentVolumeClaim"],
            "mutations": (
                "allowlisted create only; retained; no delete, Helm, SSH, cloud, Secret, "
                "or workload"
            ),
        },
    )


def _bounded_runner(
    argv: tuple[str, ...], *, input: str | None = None, timeout: int = _TIMEOUT
) -> CommandResult:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise AstraSetupError("required local command could not be started") from error
    assert process.stdout is not None and process.stderr is not None
    captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name: str, stream: Any) -> None:
        while chunk := stream.read(1024):
            room = _MAX_OUTPUT - len(captured[name])
            captured[name].extend(chunk[: max(room, 0)])
            if len(chunk) > room:
                overflow.set()
                process.kill()
                return

    threads = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for thread in threads:
        thread.start()
    try:
        if input is not None:
            assert process.stdin is not None
            process.stdin.write(input.encode())
            process.stdin.close()
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        process.kill()
        process.wait()
        raise AstraSetupError("bounded local command timed out or could not complete") from error
    finally:
        for thread in threads:
            thread.join(timeout=1)
    if overflow.is_set():
        raise AstraSetupError("bounded local command exceeded its output limit")
    return CommandResult(
        argv,
        process.returncode,
        bytes(captured["stdout"]).decode("utf-8", "replace"),
        bytes(captured["stderr"]).decode("utf-8", "replace"),
    )


class AstraDockerDesktopSetup:
    """Discover and reconcile only one explicit Docker Desktop profile."""

    def __init__(self, kubeconfig: Path, context: str, runner: Runner = _bounded_runner) -> None:
        self.kubeconfig = Path(kubeconfig)
        self.context = context
        self._runner = runner
        if not self.kubeconfig.is_absolute() or context != DOCKER_DESKTOP_CONTEXT:
            raise AstraSetupError(
                "an absolute kubeconfig and explicit docker-desktop context are required"
            )

    def _kubectl(self, *args: str, input: str | None = None) -> CommandResult:
        result = self._runner(
            (
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "--context",
                self.context,
                "--request-timeout=15s",
                *args,
            ),
            input=input,
            timeout=_TIMEOUT,
        )
        if result.returncode:
            raise AstraSetupError("explicit Kubernetes command failed; effects are uncertain")
        return result

    @staticmethod
    def _json(result: CommandResult, subject: str) -> Mapping[str, Any]:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AstraSetupError(f"{subject} returned malformed JSON") from error
        if not isinstance(value, dict):
            raise AstraSetupError(f"{subject} returned malformed JSON")
        return value

    @staticmethod
    def _uid(value: Mapping[str, Any], kind: str, name: str, namespace: str | None = None) -> str:
        metadata = value.get("metadata")
        if (
            value.get("kind") != kind
            or not isinstance(metadata, Mapping)
            or metadata.get("name") != name
            or (namespace is not None and metadata.get("namespace") != namespace)
        ):
            raise AstraSetupError(f"{kind} identity did not match the explicit plan")
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise AstraSetupError(f"{kind} did not return a UID")
        return uid

    def _get(self, kind: str, name: str, namespace: str | None = None) -> Mapping[str, Any] | None:
        args = ["get", kind, name, "-o", "json"]
        if namespace is not None:
            args.extend(("--namespace", namespace))
        result = self._runner(
            (
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "--context",
                self.context,
                "--request-timeout=15s",
                *args,
            ),
            timeout=_TIMEOUT,
        )
        if result.returncode:
            if "notfound" in result.stderr.lower() or "not found" in result.stderr.lower():
                return None
            raise AstraSetupError("explicit Kubernetes read failed")
        return self._json(result, kind)

    def discover(self, profile: SetupProfile) -> SetupDiscovery:
        try:
            raw = self._kubectl("config", "view", "--raw", "--minify", "-o", "json")
            config = self._json(raw, "explicit kubeconfig")
            contexts = config["contexts"]
            clusters = config["clusters"]
            context = contexts[0]["context"]
            cluster = next(
                entry["cluster"] for entry in clusters if entry["name"] == context["cluster"]
            )
            server = cluster["server"]
            ca = base64.b64decode(cluster["certificate-authority-data"], validate=True)
        except (KeyError, IndexError, StopIteration, TypeError, ValueError) as error:
            raise AstraSetupError("explicit Docker Desktop target identity is malformed") from error
        if not isinstance(server, str) or not server.startswith("https://") or not ca:
            raise AstraSetupError("explicit Docker Desktop target must have an HTTPS server and CA")
        cluster = self._get("namespace", "kube-system")
        if cluster is None:
            raise AstraSetupError("explicit cluster identity is unavailable")
        namespace = self._get("namespace", profile.namespace)
        namespace_uid = (
            None if namespace is None else self._uid(namespace, "Namespace", profile.namespace)
        )
        path = self.kubeconfig.resolve(strict=True)
        return SetupDiscovery(
            SETUP_VERSION,
            profile.name,
            str(path),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            self.context,
            server,
            hashlib.sha256(ca).hexdigest(),
            self._uid(cluster, "Namespace", "kube-system"),
            namespace_uid,
            namespace is not None,
        )

    def validate(self, profile: SetupProfile) -> dict[str, object]:
        discovery = self.discover(profile)
        return {
            "state": "valid",
            "read_only": True,
            "discovery": discovery.payload(),
            "network_enforcement": "not_tested",
            "storage_enforcement": "not_tested",
        }

    @staticmethod
    def _manifests(
        profile: SetupProfile,
    ) -> tuple[tuple[str, str, str | None, dict[str, object]], ...]:
        return (
            (
                "Namespace",
                profile.namespace,
                None,
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": profile.namespace}},
            ),
            (
                "NetworkPolicy",
                profile.network_policy,
                profile.namespace,
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": {"name": profile.network_policy, "namespace": profile.namespace},
                    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
                },
            ),
            (
                "PersistentVolumeClaim",
                profile.pvc,
                profile.namespace,
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": profile.pvc, "namespace": profile.namespace},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": profile.pvc_storage}},
                        "storageClassName": profile.pvc_storage_class,
                        "volumeMode": profile.pvc_volume_mode,
                    },
                },
            ),
        )

    @staticmethod
    def _spec_matches(kind: str, expected: object, observed: object) -> bool:
        """Compare the PVC fields this profile owns, not binding state."""
        if kind != "PersistentVolumeClaim":
            return observed == expected
        if not isinstance(expected, Mapping) or not isinstance(observed, Mapping):
            return False
        try:
            expected_request = expected["resources"]["requests"]["storage"]
            observed_request = observed["resources"]["requests"]["storage"]
        except (KeyError, TypeError):
            return False
        return (
            observed.get("accessModes") == expected.get("accessModes")
            and observed_request == expected_request
            and observed.get("storageClassName") == expected.get("storageClassName")
            and observed.get("volumeMode") == expected.get("volumeMode")
        )

    def _reconcile_custody(
        self, plan: SetupPlan, identities: Mapping[tuple[str, str, str | None], str]
    ) -> None:
        """Fence repeat apply before it can adopt a replacement resource."""
        for kind, name, namespace, manifest in self._manifests(plan.profile):
            singular = {
                "Namespace": "namespace",
                "NetworkPolicy": "networkpolicy",
                "PersistentVolumeClaim": "pvc",
            }[kind]
            observed = self._get(singular, name, namespace)
            if observed is None:
                raise AstraSetupError(
                    f"retained {kind} identity is missing; reconciliation is required"
                )
            if self._uid(observed, kind, name, namespace) != identities[(kind, name, namespace)]:
                raise AstraSetupError(
                    f"retained {kind} identity was replaced; reconciliation is required"
                )
            expected_spec = manifest.get("spec")
            if expected_spec is not None and not self._spec_matches(
                kind, expected_spec, observed.get("spec")
            ):
                raise AstraSetupError(f"retained {kind} spec conflicts with the approved plan")

    def apply(
        self, plan: SetupPlan, approved_digest: str, journal: SetupJournal
    ) -> dict[str, object]:
        if approved_digest != plan.digest:
            raise AstraSetupError("apply requires the exact human-approved setup plan digest")
        fresh = self.discover(plan.profile)
        if fresh.payload() != plan.discovery.payload():
            raise AstraSetupError("setup discovery changed; create and approve a fresh plan")
        custody = journal.applied_resource_identities(plan)
        if custody is not None:
            self._reconcile_custody(plan, custody)
        journal.record({"event": "apply_intent", "plan_digest": plan.digest, "at": _now()})
        resources: list[dict[str, object]] = []
        namespace_uid: str | None = None
        for kind, name, namespace, manifest in self._manifests(plan.profile):
            if namespace is not None:
                current_namespace = self._get("namespace", plan.profile.namespace)
                if current_namespace is None or namespace_uid is None:
                    raise AstraSetupError("namespace identity is unavailable during setup")
                if (
                    self._uid(current_namespace, "Namespace", plan.profile.namespace)
                    != namespace_uid
                ):
                    raise AstraSetupError("namespace identity changed during setup")
            singular = {
                "Namespace": "namespace",
                "NetworkPolicy": "networkpolicy",
                "PersistentVolumeClaim": "pvc",
            }[kind]
            observed = self._get(singular, name, namespace)
            expected_spec = manifest.get("spec")
            created = observed is None
            if observed is None:
                create_args = ["create", "-f", "-", "-o", "json"]
                if namespace is not None:
                    create_args.extend(("--namespace", namespace))
                observed = self._json(
                    self._kubectl(*create_args, input=canonical_json(manifest).decode()), kind
                )
            uid = self._uid(observed, kind, name, namespace)
            if kind == "Namespace":
                namespace_uid = uid
            actual_spec = observed.get("spec")
            if expected_spec is not None and not self._spec_matches(
                kind, expected_spec, actual_spec
            ):
                raise AstraSetupError(f"pre-existing {kind} spec conflicts with the approved plan")
            resource = {
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "uid": uid,
                "created": created,
                "spec_sha256": _digest(actual_spec) if expected_spec is not None else None,
            }
            resources.append(resource)
            journal.record(
                {
                    "event": "resource_observed",
                    "plan_digest": plan.digest,
                    "at": _now(),
                    "resource": resource,
                }
            )
        result = {
            "state": "ready",
            "plan_digest": plan.digest,
            "resources": resources,
            "readiness": "resource identities and declared specs observed",
            "network_enforcement": "declared_by_NetworkPolicy_not_proven",
            "storage_enforcement": "PVC_bound_or_quota_not_proven",
            "uncertainty": [
                "Kubernetes NetworkPolicy implementation is not tested by setup",
                "PVC binding, capacity, quotas, and durability are not proven by setup",
            ],
            "retained": True,
        }
        journal.record({"event": "apply_complete", "at": _now(), **result})
        return result

    def status(self, plan: SetupPlan, journal: SetupJournal | None = None) -> dict[str, object]:
        fresh = self.discover(plan.profile)
        if (
            fresh.kubeconfig_sha256 != plan.discovery.kubeconfig_sha256
            or fresh.ca_sha256 != plan.discovery.ca_sha256
            or fresh.cluster_uid != plan.discovery.cluster_uid
        ):
            raise AstraSetupError("target identity changed; setup status is unsafe")
        if journal is None:
            raise AstraSetupError("setup status requires recorded apply-time resource identities")
        custody = journal.applied_resource_identities(plan)
        if custody is None:
            raise AstraSetupError("setup status requires recorded apply-time resource identities")
        resources = []
        for kind, name, namespace, manifest in self._manifests(plan.profile):
            singular = {
                "Namespace": "namespace",
                "NetworkPolicy": "networkpolicy",
                "PersistentVolumeClaim": "pvc",
            }[kind]
            observed = self._get(singular, name, namespace)
            expected_uid = custody[(kind, name, namespace)]
            if observed is None:
                resources.append(
                    {
                        "kind": kind,
                        "name": name,
                        "namespace": namespace,
                        "state": "missing",
                        "expected_uid": expected_uid,
                    }
                )
                continue
            expected_spec = manifest.get("spec")
            uid = self._uid(observed, kind, name, namespace)
            matches_custody = uid == expected_uid
            resources.append(
                {
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "state": "observed" if matches_custody else "replaced",
                    "uid": uid,
                    "expected_uid": expected_uid,
                    "matches_custody": matches_custody,
                    "spec_sha256": _digest(observed.get("spec"))
                    if expected_spec is not None
                    else None,
                    "matches_plan": expected_spec is None
                    or self._spec_matches(kind, expected_spec, observed.get("spec")),
                }
            )
        ready = all(
            resource.get("matches_custody", False) and resource.get("matches_plan", False)
            for resource in resources
        )
        return {
            "state": "ready" if ready else "reconciliation_required",
            "plan_digest": plan.digest,
            "resources": resources,
            "network_enforcement": "declared_by_NetworkPolicy_not_proven",
            "storage_enforcement": "PVC_bound_or_quota_not_proven",
            "retained": True,
        }


class SetupJournal:
    """Append-only, private, fsync'd local setup evidence; never a credential store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise AstraSetupError("setup journal must be an absolute path outside the repository")
        parent = self.path.parent
        parent_stat = parent.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(parent_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.getuid()
            or parent_stat.st_mode & 0o077
        ):
            raise AstraSetupError(
                "setup journal parent must be an existing private directory owned by the operator"
            )
        if self.path.exists() and (
            self.path.is_symlink()
            or not self.path.is_file()
            or self.path.stat().st_uid != os.getuid()
            or self.path.stat().st_mode & 0o077
        ):
            raise AstraSetupError("setup journal must be an operator-owned regular file")

    def record(self, value: Mapping[str, object]) -> None:
        encoded = canonical_json(value) + b"\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
            try:
                if os.fstat(fd).st_nlink != 1:
                    raise AstraSetupError("setup journal has an unsafe link count")
                written = 0
                while written < len(encoded):
                    count = os.write(fd, encoded[written:])
                    if count <= 0:
                        raise AstraSetupError("setup journal could not be durably recorded")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as error:
            raise AstraSetupError("setup journal could not be durably recorded") from error

    def applied_resource_identities(
        self, plan: SetupPlan
    ) -> dict[tuple[str, str, str | None], str] | None:
        """Return the last complete apply's immutable resource identities.

        An interrupted apply has unknown external effects.  Do not let a later
        apply or status silently adopt whatever happens to have the same name.
        """
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        except OSError as error:
            raise AstraSetupError("setup journal could not be read for identity custody") from error
        latest: object | None = None
        pending = False
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AstraSetupError(
                    "setup journal is malformed; identity custody is unsafe"
                ) from error
            if not isinstance(event, Mapping) or event.get("plan_digest") != plan.digest:
                continue
            if event.get("event") == "apply_intent":
                pending = True
            elif event.get("event") == "apply_complete":
                latest = event.get("resources")
                pending = False
        if pending:
            raise AstraSetupError("prior setup apply is incomplete; reconciliation is required")
        if latest is None:
            return None
        expected = {
            (kind, name, namespace)
            for kind, name, namespace, _ in AstraDockerDesktopSetup._manifests(plan.profile)
        }
        identities: dict[tuple[str, str, str | None], str] = {}
        if not isinstance(latest, list):
            raise AstraSetupError("setup journal lacks complete resource identity custody")
        for resource in latest:
            if not isinstance(resource, Mapping):
                raise AstraSetupError("setup journal lacks complete resource identity custody")
            kind, name, namespace, uid = (
                resource.get("kind"),
                resource.get("name"),
                resource.get("namespace"),
                resource.get("uid"),
            )
            key = (kind, name, namespace)
            if (
                not isinstance(kind, str)
                or not isinstance(name, str)
                or (namespace is not None and not isinstance(namespace, str))
                or not isinstance(uid, str)
                or not uid
                or key not in expected
                or key in identities
            ):
                raise AstraSetupError("setup journal lacks complete resource identity custody")
            identities[key] = uid
        if set(identities) != expected:
            raise AstraSetupError("setup journal lacks complete resource identity custody")
        return identities


def plan_from_json(path: Path) -> SetupPlan:
    try:
        value = json.loads(Path(path).read_text())
        profile = SetupProfile(**value["profile"])
        discovery = SetupDiscovery(**value["discovery"])
        return SetupPlan(value["version"], profile, discovery, value["digest"])
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        raise AstraSetupError("setup plan file is malformed") from error


def plan_json(plan: SetupPlan) -> str:
    return canonical_json({**plan.payload(), "digest": plan.digest}).decode()


__all__ = [
    "AstraDockerDesktopSetup",
    "AstraSetupError",
    "CommandResult",
    "DOCKER_DESKTOP_CONTEXT",
    "DOCKER_DESKTOP_PROFILE",
    "SETUP_VERSION",
    "SetupDiscovery",
    "SetupJournal",
    "SetupPlan",
    "SetupProfile",
    "plan_from_json",
    "plan_json",
    "profile_catalog",
]
