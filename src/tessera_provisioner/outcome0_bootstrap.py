"""Offline, fake-effect contract for an Outcome 0 environment bootstrap.

The contract binds an approved capability declaration to an already-issued
execution lease and immutable runtime package.  It deliberately has no network,
subprocess, container, credential, or provider implementation.  A caller that
eventually supplies a live operator must preserve these identities and route
produced bytes through the normal quarantined artifact-custody path.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from urllib.parse import urlparse

from .contracts import digest

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!\-]{0,127}\Z")
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024


class Outcome0BootstrapError(ValueError):
    """The requested environment bootstrap is invalid or cannot proceed."""


class Outcome0BootstrapLostReply(Outcome0BootstrapError):
    """A fake effect may have happened and must be reconciled by its stable key."""


class Outcome0BootstrapCancelled(Outcome0BootstrapError):
    """Cancellation won before the next fake external effect."""


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise Outcome0BootstrapError(f"{field} must be a bounded lowercase identifier.")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise Outcome0BootstrapError(f"{field} must be a SHA-256 digest.")
    return value


def _package(value: object, field: str) -> str:
    if not isinstance(value, str) or not _PACKAGE.fullmatch(value):
        raise Outcome0BootstrapError(f"{field} must be a bounded package name.")
    return value


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise Outcome0BootstrapError(f"{field} must be a bounded package version.")
    return value


def _pinned_image(value: object) -> str:
    if not isinstance(value, str) or len(value) > 255 or "@sha256:" not in value:
        raise Outcome0BootstrapError("image must be a digest-pinned image reference.")
    repository, marker, image_digest = value.partition("@sha256:")
    if not repository or marker != "@sha256:" or not _DIGEST.fullmatch(image_digest):
        raise Outcome0BootstrapError("image must be a digest-pinned image reference.")
    if any(char.isspace() or char == "\0" for char in repository):
        raise Outcome0BootstrapError("image must be a safe image reference.")
    return value


def _source_uri(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048:
        raise Outcome0BootstrapError(f"{field} must be a bounded source URI.")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "fixture"} or not parsed.netloc or parsed.username:
        raise Outcome0BootstrapError(f"{field} must use an approved https or fixture URI.")
    if (
        parsed.query
        or parsed.fragment
        or "%" in value
        or "\\" in value
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise Outcome0BootstrapError(
            f"{field} must be a canonical URI without query, fragment, or traversal."
        )
    return value


def _source_contains(prefix: str, value: str) -> bool:
    allowed = urlparse(prefix)
    candidate = urlparse(value)
    if (allowed.scheme, allowed.netloc) != (candidate.scheme, candidate.netloc):
        return False
    base_path = allowed.path.rstrip("/")
    return candidate.path == base_path or candidate.path.startswith(base_path + "/")


@dataclass(frozen=True)
class LeasePackageBinding:
    """The immutable execution package identity that owns bootstrap effects."""

    lease_id: str
    operation_id: str
    package_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.lease_id, str)
            or not self.lease_id
            or len(self.lease_id.encode("utf-8")) > 200
            or "\0" in self.lease_id
        ):
            raise Outcome0BootstrapError("lease_id must be a bounded execution lease identity.")
        if not isinstance(self.operation_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", self.operation_id
        ):
            raise Outcome0BootstrapError("operation_id must be a 32-character operation identity.")
        _sha256(self.package_digest, "package_digest")

    def payload(self) -> dict[str, str]:
        return {
            "lease_id": self.lease_id,
            "operation_id": self.operation_id,
            "package_digest": self.package_digest,
        }


@dataclass(frozen=True)
class ApprovedSource:
    """One human-approved model or data source prefix."""

    id: str
    uri_prefix: str

    def __post_init__(self) -> None:
        _identifier(self.id, "source id")
        _source_uri(self.uri_prefix, "source uri_prefix")

    def payload(self) -> dict[str, str]:
        return {"id": self.id, "uri_prefix": self.uri_prefix.rstrip("/")}


@dataclass(frozen=True)
class DeclaredPackage:
    """A package exactly named in a human-approved capability declaration."""

    name: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        _package(self.name, "package name")
        _version(self.version, "package version")
        _sha256(self.sha256, "package sha256")

    def payload(self) -> dict[str, str]:
        return {"name": self.name, "sha256": self.sha256, "version": self.version}


@dataclass(frozen=True)
class DeclaredArtifact:
    """A digest/size-pinned model or data artifact from an approved source."""

    name: str
    kind: Literal["model", "data"]
    source_id: str
    uri: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _identifier(self.name, "artifact name")
        if self.kind not in {"model", "data"}:
            raise Outcome0BootstrapError("artifact kind must be model or data.")
        _identifier(self.source_id, "artifact source_id")
        _source_uri(self.uri, "artifact uri")
        _sha256(self.sha256, "artifact sha256")
        if type(self.size) is not int or not 1 <= self.size <= MAX_ARTIFACT_BYTES:
            raise Outcome0BootstrapError("artifact size exceeds the bootstrap limit.")

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "source_id": self.source_id,
            "uri": self.uri,
        }


@dataclass(frozen=True)
class CapabilityDeclaration:
    """Immutable human approval; this is capability scope, not scientific approval."""

    id: str
    approval_id: str
    approved_by: str
    sources: tuple[ApprovedSource, ...]
    python_packages: tuple[DeclaredPackage, ...]
    system_packages: tuple[DeclaredPackage, ...]
    artifacts: tuple[DeclaredArtifact, ...]

    def __post_init__(self) -> None:
        _identifier(self.id, "capability declaration id")
        _identifier(self.approval_id, "capability approval_id")
        _identifier(self.approved_by, "capability approved_by")
        source_ids = [source.id for source in self.sources]
        if not source_ids or len(source_ids) != len(set(source_ids)):
            raise Outcome0BootstrapError(
                "capability declaration must have unique approved sources."
            )
        for artifact in self.artifacts:
            source = next((item for item in self.sources if item.id == artifact.source_id), None)
            if source is None or not _source_contains(source.uri_prefix, artifact.uri):
                raise Outcome0BootstrapError("artifact URI is not within its approved source.")
        for packages, label in (
            (self.python_packages, "python"),
            (self.system_packages, "system"),
        ):
            names = [item.name for item in packages]
            if len(names) != len(set(names)):
                raise Outcome0BootstrapError(f"{label} package names must be unique.")
        artifact_names = [item.name for item in self.artifacts]
        if len(artifact_names) != len(set(artifact_names)):
            raise Outcome0BootstrapError("artifact names must be unique.")
        # Preserve one canonical declaration representation so ordering cannot
        # alter the plan identity, effect order, logs, or manifest provenance.
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=lambda item: item.id)))
        object.__setattr__(
            self, "python_packages", tuple(sorted(self.python_packages, key=lambda item: item.name))
        )
        object.__setattr__(
            self, "system_packages", tuple(sorted(self.system_packages, key=lambda item: item.name))
        )
        object.__setattr__(
            self, "artifacts", tuple(sorted(self.artifacts, key=lambda item: item.name))
        )

    def payload(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "approved_by": self.approved_by,
            "artifacts": [
                item.payload() for item in sorted(self.artifacts, key=lambda item: item.name)
            ],
            "id": self.id,
            "python_packages": [
                item.payload() for item in sorted(self.python_packages, key=lambda item: item.name)
            ],
            "sources": [item.payload() for item in sorted(self.sources, key=lambda item: item.id)],
            "system_packages": [
                item.payload() for item in sorted(self.system_packages, key=lambda item: item.name)
            ],
        }


@dataclass(frozen=True)
class PinnedEnvironment:
    image: str
    lock_digest: str

    def __post_init__(self) -> None:
        _pinned_image(self.image)
        _sha256(self.lock_digest, "lock_digest")

    def payload(self) -> dict[str, str]:
        return {"image": self.image, "lock_digest": self.lock_digest}


@dataclass(frozen=True)
class BootstrapLimits:
    max_workspace_bytes: int
    max_artifact_bytes: int
    max_artifacts: int
    max_python_packages: int
    max_system_packages: int

    def __post_init__(self) -> None:
        values = (
            self.max_workspace_bytes,
            self.max_artifact_bytes,
            self.max_artifacts,
            self.max_python_packages,
            self.max_system_packages,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise Outcome0BootstrapError("bootstrap limits must be positive integers.")
        if self.max_workspace_bytes > MAX_ARTIFACT_BYTES * 2:
            raise Outcome0BootstrapError("workspace limit exceeds the offline bootstrap bound.")
        if self.max_artifact_bytes > MAX_ARTIFACT_BYTES or self.max_artifacts > 128:
            raise Outcome0BootstrapError("artifact limits exceed the offline bootstrap bound.")
        if self.max_python_packages > 256 or self.max_system_packages > 256:
            raise Outcome0BootstrapError("package limits exceed the offline bootstrap bound.")

    def payload(self) -> dict[str, int]:
        return {
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_artifacts": self.max_artifacts,
            "max_python_packages": self.max_python_packages,
            "max_system_packages": self.max_system_packages,
            "max_workspace_bytes": self.max_workspace_bytes,
        }


@dataclass(frozen=True)
class BootstrapPlan:
    """The only replay identity for one isolated Outcome 0 workspace."""

    binding: LeasePackageBinding
    declaration: CapabilityDeclaration
    environment: PinnedEnvironment
    limits: BootstrapLimits
    digest: str

    def __post_init__(self) -> None:
        _sha256(self.digest, "bootstrap plan digest")
        if self.digest != digest(self.payload()):
            raise Outcome0BootstrapError(
                "bootstrap plan digest does not match its immutable payload."
            )
        self._check_limits()

    @classmethod
    def create(
        cls,
        binding: LeasePackageBinding,
        declaration: CapabilityDeclaration,
        environment: PinnedEnvironment,
        limits: BootstrapLimits,
    ) -> BootstrapPlan:
        payload = {
            "binding": binding.payload(),
            "declaration": declaration.payload(),
            "environment": environment.payload(),
            "limits": limits.payload(),
        }
        return cls(binding, declaration, environment, limits, digest(payload))

    def payload(self) -> dict[str, object]:
        return {
            "binding": self.binding.payload(),
            "declaration": self.declaration.payload(),
            "environment": self.environment.payload(),
            "limits": self.limits.payload(),
        }

    def _check_limits(self) -> None:
        artifacts = self.declaration.artifacts
        if len(artifacts) > self.limits.max_artifacts:
            raise Outcome0BootstrapError(
                "declared artifact count exceeds the approved bootstrap limit."
            )
        total_bytes = sum(item.size for item in artifacts)
        if total_bytes > self.limits.max_artifact_bytes:
            raise Outcome0BootstrapError("declared artifacts exceed the approved byte limit.")
        if total_bytes > self.limits.max_workspace_bytes:
            raise Outcome0BootstrapError("declared artifacts exceed the isolated workspace limit.")
        if len(self.declaration.python_packages) > self.limits.max_python_packages:
            raise Outcome0BootstrapError("declared Python packages exceed the approved limit.")
        if len(self.declaration.system_packages) > self.limits.max_system_packages:
            raise Outcome0BootstrapError("declared system packages exceed the approved limit.")

    def assert_current(self) -> None:
        if self.digest != digest(self.payload()):
            raise Outcome0BootstrapError("bootstrap plan payload changed after approval.")
        self._check_limits()


@dataclass(frozen=True)
class CustodyRecord:
    """Quarantined environment bytes, ready for existing artifact-custody intake."""

    artifact_id: str
    lease_id: str
    operation_id: str
    package_digest: str
    name: str
    sha256: str
    size: int
    custody_state: Literal["quarantined_unreleased"] = "quarantined_unreleased"


class ArtifactCustodySink(Protocol):
    """Adapter boundary to Tessera's existing worker artifact-custody upload."""

    def record(self, custody: CustodyRecord) -> None: ...


@dataclass(frozen=True)
class FakeBootstrapEffect:
    key: str
    kind: Literal["workspace", "system_package", "python_package", "artifact"]
    name: str
    digest: str


@dataclass(frozen=True)
class BootstrapEvidence:
    workspace_id: str
    manifest: dict[str, object]
    manifest_digest: str
    log: tuple[str, ...]
    log_digest: str
    custody: tuple[CustodyRecord, ...]


class FakeOutcome0BootstrapAdapter:
    """Deterministic offline effects using explicitly supplied local fixture bytes."""

    live_effects = False

    def __init__(self, fixtures: Mapping[str, bytes], custody: ArtifactCustodySink) -> None:
        self.fixtures = dict(fixtures)
        self.custody = custody
        self.effects: list[FakeBootstrapEffect] = []
        self._effects_by_key: dict[str, FakeBootstrapEffect] = {}
        self._custody_by_key: dict[str, CustodyRecord] = {}
        self._logs_by_plan: dict[str, list[str]] = {}
        self.fail_once_at: str | None = None
        self.lose_next_reply = False
        self.before_effect: Callable[[], None] | None = None

    @staticmethod
    def _effect_key(plan: BootstrapPlan, kind: str, name: str, item_digest: str) -> str:
        return digest(
            {
                "bootstrap_plan_digest": plan.digest,
                "item_digest": item_digest,
                "kind": kind,
                "name": name,
            }
        )

    def _record_effect(
        self,
        plan: BootstrapPlan,
        kind: Literal["workspace", "system_package", "python_package", "artifact"],
        name: str,
        item_digest: str,
        allowed: Callable[[], bool],
    ) -> FakeBootstrapEffect:
        if self.before_effect is not None:
            self.before_effect()
        if not allowed():
            raise Outcome0BootstrapCancelled("bootstrap was cancelled before the next effect.")
        key = self._effect_key(plan, kind, name, item_digest)
        existing = self._effects_by_key.get(key)
        if existing is not None:
            return existing
        if self.fail_once_at == f"{kind}:{name}":
            self.fail_once_at = None
            raise Outcome0BootstrapError(f"fake bootstrap failed at {kind}:{name}.")
        effect = FakeBootstrapEffect(key, kind, name, item_digest)
        self._effects_by_key[key] = effect
        self.effects.append(effect)
        self._logs_by_plan.setdefault(plan.digest, []).append(f"applied {kind}:{name}")
        return effect

    def reconcile(self, plan: BootstrapPlan, *, allowed: Callable[[], bool]) -> BootstrapEvidence:
        plan.assert_current()
        self._record_effect(plan, "workspace", "isolated", plan.digest, allowed)
        for package in plan.declaration.system_packages:
            self._record_effect(plan, "system_package", package.name, package.sha256, allowed)
        for package in plan.declaration.python_packages:
            self._record_effect(plan, "python_package", package.name, package.sha256, allowed)
        for artifact in plan.declaration.artifacts:
            content = self.fixtures.get(artifact.uri)
            if content is None:
                raise Outcome0BootstrapError(
                    "approved artifact is unavailable from the local fixture."
                )
            if (
                len(content) != artifact.size
                or hashlib.sha256(content).hexdigest() != artifact.sha256
            ):
                raise Outcome0BootstrapError(
                    "local fixture does not match the declared artifact hash."
                )
            # Verify bytes before recording the completed download effect.  A
            # retry after an unavailable/corrupt fixture must not leave a log
            # or fake effect that claims a download completed.
            effect = self._record_effect(plan, "artifact", artifact.name, artifact.sha256, allowed)
            if effect.key not in self._custody_by_key:
                custody = CustodyRecord(
                    artifact_id="environment_artifact_" + effect.key[:32],
                    lease_id=plan.binding.lease_id,
                    operation_id=plan.binding.operation_id,
                    package_digest=plan.binding.package_digest,
                    name=artifact.name,
                    sha256=artifact.sha256,
                    size=artifact.size,
                )
                self.custody.record(custody)
                self._custody_by_key[effect.key] = custody
        evidence = self._evidence(plan)
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise Outcome0BootstrapLostReply(
                "fake bootstrap reply was lost; reconcile the stable bootstrap plan identity."
            )
        return evidence

    def _evidence(self, plan: BootstrapPlan) -> BootstrapEvidence:
        custody = tuple(
            self._custody_by_key[self._effect_key(plan, "artifact", item.name, item.sha256)]
            for item in plan.declaration.artifacts
        )
        manifest = {
            "version": 1,
            "bootstrap_plan_digest": plan.digest,
            "execution": plan.binding.payload(),
            "environment": plan.environment.payload(),
            "capability_declaration": {
                "id": plan.declaration.id,
                "approval_id": plan.declaration.approval_id,
                "approved_by": plan.declaration.approved_by,
            },
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "custody_state": item.custody_state,
                    "name": item.name,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in custody
            ],
        }
        log = tuple(self._logs_by_plan.get(plan.digest, ()))
        log_bytes = "\n".join(log).encode()
        if len(log_bytes) > MAX_LOG_BYTES:
            raise Outcome0BootstrapError("bootstrap log exceeds the offline bootstrap limit.")
        return BootstrapEvidence(
            workspace_id="outcome0-workspace-" + plan.digest[:24],
            manifest=manifest,
            manifest_digest=digest(manifest),
            log=log,
            log_digest=hashlib.sha256(log_bytes).hexdigest(),
            custody=custody,
        )


@dataclass(frozen=True)
class BootstrapOperation:
    id: str
    plan: BootstrapPlan
    revision: int
    desired_state: Literal["requested", "approved", "cancel_requested", "ready"]
    observed_state: Literal["pending", "ready", "failed", "unknown", "cancelled"]
    evidence: BootstrapEvidence | None = None
    failure: str | None = None


class Outcome0BootstrapStore:
    """Small state machine that exposes readiness, never experimental outcomes."""

    def __init__(self, adapter: FakeOutcome0BootstrapAdapter) -> None:
        self.adapter = adapter
        self._operations: dict[str, BootstrapOperation] = {}

    @staticmethod
    def _operation_id(plan: BootstrapPlan) -> str:
        return "outcome0-bootstrap-" + plan.digest[:32]

    def request(self, plan: BootstrapPlan) -> BootstrapOperation:
        plan.assert_current()
        operation_id = self._operation_id(plan)
        existing = self._operations.get(operation_id)
        if existing is not None:
            if existing.plan != plan:
                raise Outcome0BootstrapError(
                    "bootstrap operation identity conflicts with its plan."
                )
            return existing
        operation = BootstrapOperation(operation_id, plan, 1, "requested", "pending")
        self._operations[operation_id] = operation
        return operation

    def approve(self, operation_id: str, expected_revision: int) -> BootstrapOperation:
        operation = self._operation(operation_id)
        if operation.revision != expected_revision or operation.desired_state != "requested":
            raise Outcome0BootstrapError("bootstrap approval is fenced by the current revision.")
        approved = replace(operation, revision=operation.revision + 1, desired_state="approved")
        self._operations[operation_id] = approved
        return approved

    def retry(self, operation_id: str, expected_revision: int) -> BootstrapOperation:
        operation = self._operation(operation_id)
        if operation.revision != expected_revision or operation.observed_state not in {
            "failed",
            "unknown",
        }:
            raise Outcome0BootstrapError(
                "only failed or unknown bootstrap operations may be retried."
            )
        retried = replace(
            operation,
            revision=operation.revision + 1,
            desired_state="approved",
            observed_state="pending",
            failure=None,
        )
        self._operations[operation_id] = retried
        return retried

    def cancel(self, operation_id: str, expected_revision: int) -> BootstrapOperation:
        operation = self._operation(operation_id)
        if operation.revision != expected_revision:
            raise Outcome0BootstrapError(
                "bootstrap cancellation is fenced by the current revision."
            )
        if operation.desired_state == "ready":
            raise Outcome0BootstrapError(
                "a ready bootstrap is immutable; do not reclaim its custody here."
            )
        cancelled = replace(
            operation,
            revision=operation.revision + 1,
            desired_state="cancel_requested",
            observed_state="cancelled" if operation.observed_state != "unknown" else "unknown",
        )
        self._operations[operation_id] = cancelled
        return cancelled

    def reconcile(self, operation_id: str) -> BootstrapOperation:
        operation = self._operation(operation_id)
        if operation.desired_state == "ready":
            return operation
        if operation.desired_state != "approved":
            return operation

        def allowed() -> bool:
            current = self._operations.get(operation_id)
            return current == operation and current.desired_state == "approved"

        try:
            evidence = self.adapter.reconcile(operation.plan, allowed=allowed)
        except Outcome0BootstrapLostReply as error:
            unknown = replace(operation, observed_state="unknown", failure=str(error))
            self._operations[operation_id] = unknown
            return unknown
        except Outcome0BootstrapCancelled:
            return self._operations[operation_id]
        except Outcome0BootstrapError as error:
            failed = replace(operation, observed_state="failed", failure=str(error)[:500])
            self._operations[operation_id] = failed
            return failed
        ready = replace(
            operation,
            revision=operation.revision + 1,
            desired_state="ready",
            observed_state="ready",
            evidence=evidence,
        )
        self._operations[operation_id] = ready
        return ready

    def readiness(self, operation_id: str) -> dict[str, object]:
        """A control-plane-safe projection; no environmental success is a scientific result."""
        operation = self._operation(operation_id)
        evidence = operation.evidence
        return {
            "bootstrap_operation_id": operation.id,
            "lease_id": operation.plan.binding.lease_id,
            "operation_id": operation.plan.binding.operation_id,
            "package_digest": operation.plan.binding.package_digest,
            "bootstrap_plan_digest": operation.plan.digest,
            "provider_enabled": False,
            "live_effects": False,
            "desired_state": operation.desired_state,
            "observed_state": operation.observed_state,
            "ready": operation.desired_state == "ready" and operation.observed_state == "ready",
            "manifest_digest": evidence.manifest_digest if evidence else None,
            "log_digest": evidence.log_digest if evidence else None,
            "custody": [
                {
                    "artifact_id": item.artifact_id,
                    "custody_state": item.custody_state,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in evidence.custody
            ]
            if evidence
            else [],
            "scientific_results": {
                "status": "not_collected",
                "reason": (
                    "environment bootstrap does not execute, measure, or accept an experiment"
                ),
            },
            "failure": operation.failure,
        }

    def _operation(self, operation_id: str) -> BootstrapOperation:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise Outcome0BootstrapError("bootstrap operation was not found.")
        return operation


__all__ = [
    "ApprovedSource",
    "ArtifactCustodySink",
    "BootstrapEvidence",
    "BootstrapLimits",
    "BootstrapOperation",
    "BootstrapPlan",
    "CapabilityDeclaration",
    "CustodyRecord",
    "DeclaredArtifact",
    "DeclaredPackage",
    "FakeBootstrapEffect",
    "FakeOutcome0BootstrapAdapter",
    "LeasePackageBinding",
    "Outcome0BootstrapCancelled",
    "Outcome0BootstrapError",
    "Outcome0BootstrapLostReply",
    "Outcome0BootstrapStore",
    "PinnedEnvironment",
]
