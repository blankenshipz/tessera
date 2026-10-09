"""Pure, offline Astra installation inspection and approval-plan contracts.

This module records caller-supplied facts only.  It does not read a kubeconfig,
invoke Kubernetes, materialize credentials, or select an execution target.  A
future operator may use these immutable records as inputs, but must independently
perform its own explicit target validation and authorization checks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from .contracts import digest

INSTALLATION_CONTRACT_VERSION = "astra-installation-inspection-v3"
MAX_SNAPSHOT_BYTES = 65_536
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_CREDENTIAL_REFERENCE = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_CONTEXT = re.compile(r"[^\x00-\x1f\x7f]{1,253}\Z")
_RESOURCE_IDENTITY = re.compile(
    r"(deployment|persistentvolumeclaim|networkpolicy|serviceaccount)/([a-z0-9]([-a-z0-9]*[a-z0-9])?)\Z"
)
_SENSITIVE_REFERENCE_FRAGMENT = re.compile(r"(?:api-?key|password|secret|token)", re.IGNORECASE)
InstallationMode = Literal["create", "observe", "adopt", "adapt"]
FindingKind = Literal["storage", "network", "permission"]
FindingState = Literal["satisfied", "missing", "incompatible", "unknown"]


class AstraInstallationError(ValueError):
    """Offline installation facts or an approval plan fail closed validation."""


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _NAME.fullmatch(value):
        raise AstraInstallationError(f"{field} must be a bounded Kubernetes name.")
    return value


def _uid(value: object, field: str) -> str:
    if not isinstance(value, str) or not _UID.fullmatch(value):
        raise AstraInstallationError(f"{field} must be a bounded resource UID.")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise AstraInstallationError(f"{field} must be a SHA-256 digest.")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise AstraInstallationError(f"{field} must be an RFC 3339 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AstraInstallationError(f"{field} must be an RFC 3339 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AstraInstallationError(f"{field} must include an offset.")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _https_server(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
        raise AstraInstallationError("target server must be a bounded explicit HTTPS endpoint.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AstraInstallationError(
            "target server must be an explicit HTTPS endpoint without credentials."
        )
    return value


def _context(value: object) -> str:
    """Kubeconfig contexts are display names, not Kubernetes object names."""
    if not isinstance(value, str) or not _CONTEXT.fullmatch(value):
        raise AstraInstallationError(
            "target context must be a bounded non-control kubeconfig name."
        )
    return value


def _resource_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _RESOURCE_IDENTITY.fullmatch(value):
        raise AstraInstallationError(f"{field} must be an allowed TYPE/NAME resource identity.")
    return value


@dataclass(frozen=True)
class InstallationIdentity:
    """The requested Astra installation, independent of any live client."""

    name: str
    profile: str

    def __post_init__(self) -> None:
        _name(self.name, "installation name")
        _name(self.profile, "installation profile")

    def payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class TargetIdentity:
    """Explicit cluster and namespace identity; never an ambient current context."""

    kubeconfig_reference_sha256: str
    context: str
    server: str
    ca_sha256: str
    cluster_uid: str
    namespace: str
    namespace_uid: str

    def __post_init__(self) -> None:
        _sha256(self.kubeconfig_reference_sha256, "kubeconfig reference")
        _context(self.context)
        _https_server(self.server)
        _sha256(self.ca_sha256, "target CA")
        _uid(self.cluster_uid, "cluster UID")
        _name(self.namespace, "target namespace")
        _uid(self.namespace_uid, "namespace UID")

    def payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ObservedInstallation:
    """A bounded, caller-supplied observation of one named installation."""

    installation: InstallationIdentity
    target: TargetIdentity
    uid: str
    spec_sha256: str
    observed_at: str
    resource_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.installation, InstallationIdentity):
            raise AstraInstallationError("observation requires a typed installation identity.")
        if not isinstance(self.target, TargetIdentity):
            raise AstraInstallationError("observation requires a typed target identity.")
        _uid(self.uid, "observed installation UID")
        _sha256(self.spec_sha256, "observed installation spec")
        _timestamp(self.observed_at, "observed_at")
        if self.resource_version is not None and (
            not isinstance(self.resource_version, str)
            or not self.resource_version
            or len(self.resource_version) > 256
        ):
            raise AstraInstallationError("observed installation resourceVersion must be bounded.")

    def payload(self) -> dict[str, object]:
        return {
            "installation": self.installation.payload(),
            "observed_at": _timestamp(self.observed_at, "observed_at"),
            "spec_sha256": self.spec_sha256,
            "target": self.target.payload(),
            "uid": self.uid,
            "resource_version": self.resource_version,
        }


@dataclass(frozen=True)
class OwnershipConflict:
    """An observed owner that prevents silent adoption or adaptation."""

    installation: InstallationIdentity
    target: TargetIdentity
    observed_uid: str
    owner_reference: str
    resource_identity: str = "deployment/installation"

    def __post_init__(self) -> None:
        if not isinstance(self.installation, InstallationIdentity):
            raise AstraInstallationError(
                "ownership conflict requires a typed installation identity."
            )
        if not isinstance(self.target, TargetIdentity):
            raise AstraInstallationError("ownership conflict requires a typed target identity.")
        _uid(self.observed_uid, "ownership conflict UID")
        _resource_identity(self.resource_identity, "ownership conflict resource")
        if not isinstance(self.owner_reference, str) or not _CREDENTIAL_REFERENCE.fullmatch(
            self.owner_reference
        ):
            raise AstraInstallationError("ownership conflict owner must be a bounded reference.")

    def payload(self) -> dict[str, object]:
        return {
            "installation": self.installation.payload(),
            "observed_uid": self.observed_uid,
            "owner_reference": self.owner_reference,
            "resource_identity": self.resource_identity,
            "target": self.target.payload(),
        }


@dataclass(frozen=True)
class ManagerConflict:
    """A server-side field manager that makes mutation ownership ambiguous."""

    installation: InstallationIdentity
    target: TargetIdentity
    observed_uid: str
    manager_reference: str
    resource_identity: str = "deployment/installation"

    def __post_init__(self) -> None:
        if not isinstance(self.installation, InstallationIdentity):
            raise AstraInstallationError("manager conflict requires a typed installation identity.")
        if not isinstance(self.target, TargetIdentity):
            raise AstraInstallationError("manager conflict requires a typed target identity.")
        _uid(self.observed_uid, "manager conflict UID")
        _resource_identity(self.resource_identity, "manager conflict resource")
        if not isinstance(self.manager_reference, str) or not _CREDENTIAL_REFERENCE.fullmatch(
            self.manager_reference
        ):
            raise AstraInstallationError("manager conflict manager must be a bounded reference.")

    def payload(self) -> dict[str, object]:
        return {
            "installation": self.installation.payload(),
            "manager_reference": self.manager_reference,
            "observed_uid": self.observed_uid,
            "resource_identity": self.resource_identity,
            "target": self.target.payload(),
        }


@dataclass(frozen=True)
class ResourcePrecondition:
    """A complete inspected resource fact used to fence a plan replay."""

    resource_identity: str
    state: Literal["absent", "present", "compatible", "operationally_verified"]
    observed_uid: str | None = None
    resource_version: str | None = None
    spec_sha256: str | None = None
    lifecycle: Literal["active", "terminating"] = "active"

    def __post_init__(self) -> None:
        _resource_identity(self.resource_identity, "resource precondition")
        if self.state not in {"absent", "present", "compatible", "operationally_verified"}:
            raise AstraInstallationError("resource precondition state is unsupported.")
        values = (self.observed_uid, self.resource_version, self.spec_sha256)
        if self.state == "absent":
            if any(value is not None for value in values):
                raise AstraInstallationError(
                    "absent resource preconditions cannot have observation values."
                )
        else:
            if any(value is None for value in values):
                raise AstraInstallationError(
                    "present resource preconditions require UID, resourceVersion, and spec digest."
                )
            _uid(self.observed_uid, "resource precondition UID")
            if not isinstance(self.resource_version, str) or not self.resource_version:
                raise AstraInstallationError(
                    "resource precondition resourceVersion must be bounded."
                )
            if len(self.resource_version) > 256:
                raise AstraInstallationError(
                    "resource precondition resourceVersion must be bounded."
                )
            _sha256(self.spec_sha256, "resource precondition spec")
        if self.lifecycle not in {"active", "terminating"}:
            raise AstraInstallationError("resource precondition lifecycle is unsupported.")

    def payload(self) -> dict[str, object]:
        return {
            "lifecycle": self.lifecycle,
            "observed_uid": self.observed_uid,
            "resource_identity": self.resource_identity,
            "resource_version": self.resource_version,
            "spec_sha256": self.spec_sha256,
            "state": self.state,
        }


@dataclass(frozen=True)
class InstallationFinding:
    """A bounded, non-secret storage, network, or permission inspection finding."""

    kind: FindingKind
    state: FindingState
    resource_reference: str
    observed_uid: str | None = None
    spec_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"storage", "network", "permission"}:
            raise AstraInstallationError("inspection finding kind is unsupported.")
        if self.state not in {"satisfied", "missing", "incompatible", "unknown"}:
            raise AstraInstallationError("inspection finding state is unsupported.")
        if not isinstance(self.resource_reference, str) or not _CREDENTIAL_REFERENCE.fullmatch(
            self.resource_reference
        ):
            raise AstraInstallationError("inspection finding resource must be a bounded reference.")
        if self.observed_uid is not None:
            _uid(self.observed_uid, "finding observed UID")
        if self.spec_sha256 is not None:
            _sha256(self.spec_sha256, "finding spec")

    @property
    def identity(self) -> str:
        return f"{self.kind}/{self.resource_reference}"

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "observed_uid": self.observed_uid,
            "resource_reference": self.resource_reference,
            "spec_sha256": self.spec_sha256,
            "state": self.state,
        }


@dataclass(frozen=True)
class CredentialReference:
    """A sanitized opaque credential handle, never a credential value or location."""

    kind: Literal["credential-lease", "external-reference"]
    reference: str

    def __post_init__(self) -> None:
        if self.kind not in {"credential-lease", "external-reference"}:
            raise AstraInstallationError("credential reference kind is unsupported.")
        if (
            not isinstance(self.reference, str)
            or not _CREDENTIAL_REFERENCE.fullmatch(self.reference)
            or _SENSITIVE_REFERENCE_FRAGMENT.search(self.reference)
        ):
            raise AstraInstallationError(
                "credential reference must be a bounded opaque identifier."
            )

    def payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class InstallationInspection:
    """The offline result of classifying an absent, observed, or conflicted install."""

    installation: InstallationIdentity
    target: TargetIdentity
    observation: ObservedInstallation | None
    ownership_conflicts: tuple[OwnershipConflict, ...] = ()
    manager_conflicts: tuple[ManagerConflict, ...] = ()
    findings: tuple[InstallationFinding, ...] = ()
    resources: tuple[ResourcePrecondition, ...] = ()
    source: Literal["offline-input", "offline-snapshot"] = "offline-input"
    contract_version: str = INSTALLATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != INSTALLATION_CONTRACT_VERSION:
            raise AstraInstallationError("unsupported installation inspection contract version.")
        if not isinstance(self.installation, InstallationIdentity) or not isinstance(
            self.target, TargetIdentity
        ):
            raise AstraInstallationError(
                "inspection requires typed installation and target identities."
            )
        if self.observation is not None:
            if not isinstance(self.observation, ObservedInstallation):
                raise AstraInstallationError("inspection observation must be typed.")
            if (
                self.observation.installation != self.installation
                or self.observation.target != self.target
            ):
                raise AstraInstallationError(
                    "observation identity must match the installation inspection."
                )
        if not isinstance(self.ownership_conflicts, tuple) or not all(
            isinstance(conflict, OwnershipConflict) for conflict in self.ownership_conflicts
        ):
            raise AstraInstallationError("ownership conflicts must be typed and immutable.")
        if (
            tuple(
                sorted(
                    self.ownership_conflicts,
                    key=lambda item: (item.resource_identity, item.owner_reference),
                )
            )
            != self.ownership_conflicts
        ):
            raise AstraInstallationError(
                "ownership conflicts must use canonical owner reference order."
            )
        owners = [
            (item.resource_identity, item.owner_reference) for item in self.ownership_conflicts
        ]
        if len(owners) != len(set(owners)):
            raise AstraInstallationError("ownership conflicts must have unique owner references.")
        for conflict in self.ownership_conflicts:
            if (
                not isinstance(conflict, OwnershipConflict)
                or conflict.installation != self.installation
                or conflict.target != self.target
            ):
                raise AstraInstallationError(
                    "ownership conflict identity must match the inspection."
                )
        if not isinstance(self.manager_conflicts, tuple) or not all(
            isinstance(conflict, ManagerConflict) for conflict in self.manager_conflicts
        ):
            raise AstraInstallationError("manager conflicts must be typed and immutable.")
        if (
            tuple(
                sorted(
                    self.manager_conflicts,
                    key=lambda item: (item.resource_identity, item.manager_reference),
                )
            )
            != self.manager_conflicts
        ):
            raise AstraInstallationError(
                "manager conflicts must use canonical manager reference order."
            )
        managers = [
            (item.resource_identity, item.manager_reference) for item in self.manager_conflicts
        ]
        if len(managers) != len(set(managers)):
            raise AstraInstallationError("manager conflicts must have unique manager references.")
        for conflict in self.manager_conflicts:
            if conflict.installation != self.installation or conflict.target != self.target:
                raise AstraInstallationError("manager conflict identity must match the inspection.")
        if not isinstance(self.findings, tuple) or not all(
            isinstance(finding, InstallationFinding) for finding in self.findings
        ):
            raise AstraInstallationError("inspection findings must be typed and immutable.")
        if tuple(sorted(self.findings, key=lambda item: item.identity)) != self.findings:
            raise AstraInstallationError("inspection findings must use canonical identity order.")
        identities = [item.identity for item in self.findings]
        if len(identities) != len(set(identities)):
            raise AstraInstallationError("inspection findings must have unique identities.")
        if not isinstance(self.resources, tuple) or not all(
            isinstance(resource, ResourcePrecondition) for resource in self.resources
        ):
            raise AstraInstallationError("inspection resources must be typed and immutable.")
        if tuple(sorted(self.resources, key=lambda item: item.resource_identity)) != self.resources:
            raise AstraInstallationError("inspection resources must use canonical identity order.")
        resource_identities = [item.resource_identity for item in self.resources]
        if len(resource_identities) != len(set(resource_identities)):
            raise AstraInstallationError("inspection resources must have unique identities.")
        if self.source not in {"offline-input", "offline-snapshot"}:
            raise AstraInstallationError("inspection source must be explicitly offline.")

    @property
    def state(self) -> Literal["absent", "observed", "ownership_conflict"]:
        if self.ownership_conflicts or self.manager_conflicts:
            return "ownership_conflict"
        return "observed" if self.observation is not None else "absent"

    @property
    def mutation_blockers(self) -> tuple[str, ...]:
        """Reasons a later mutating adapter must not reconcile this inspection."""
        blockers: list[str] = []
        if self.ownership_conflicts:
            blockers.append("ownership_conflict")
        if self.manager_conflicts:
            blockers.append("manager_conflict")
        if any(finding.state == "incompatible" for finding in self.findings):
            blockers.append("incompatible_resource")
        if any(resource.lifecycle == "terminating" for resource in self.resources):
            blockers.append("terminating_resource")
        return tuple(blockers)

    def payload(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "installation": self.installation.payload(),
            "findings": [item.payload() for item in self.findings],
            "manager_conflicts": [item.payload() for item in self.manager_conflicts],
            "observation": self.observation.payload() if self.observation is not None else None,
            "ownership_conflicts": [item.payload() for item in self.ownership_conflicts],
            "resources": [item.payload() for item in self.resources],
            "source": self.source,
            "target": self.target.payload(),
            "verification": "offline_unverified",
        }


@dataclass(frozen=True)
class InstallationPlan:
    """A digest-bound offline request for one explicit installation mode.

    ``observe`` records only a supplied observation. ``create`` requires an absent
    inspection. ``adopt`` requires a matching observed spec. ``adapt`` requires an
    observed base installation and may name a different desired spec.  Conflict
    observations fail closed in every mode except ``observe``.
    """

    mode: InstallationMode
    inspection: InstallationInspection
    desired_spec_sha256: str | None
    credential_references: tuple[CredentialReference, ...]
    approval_expires_at: str
    digest: str
    contract_version: str = INSTALLATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != INSTALLATION_CONTRACT_VERSION:
            raise AstraInstallationError("unsupported installation plan contract version.")
        if self.mode not in {"create", "observe", "adopt", "adapt"}:
            raise AstraInstallationError("installation plan mode is unsupported.")
        if not isinstance(self.inspection, InstallationInspection):
            raise AstraInstallationError("installation plan requires a typed inspection.")
        if self.desired_spec_sha256 is not None:
            _sha256(self.desired_spec_sha256, "desired installation spec")
        if not isinstance(self.credential_references, tuple) or not all(
            isinstance(reference, CredentialReference) for reference in self.credential_references
        ):
            raise AstraInstallationError(
                "credential references must be typed sanitized references."
            )
        if self.mode == "observe":
            if self.desired_spec_sha256 is not None or self.credential_references:
                raise AstraInstallationError(
                    "observe plans cannot request a spec or credential reference."
                )
        else:
            if self.desired_spec_sha256 is None:
                raise AstraInstallationError(
                    "mutating installation plans require a desired spec digest."
                )
            if self.inspection.mutation_blockers:
                raise AstraInstallationError(
                    "ownership conflicts, manager conflicts, or incompatible resource conflicts "
                    "require explicit "
                    "reconciliation outside this plan."
                )
            if self.mode == "create" and self.inspection.observation is not None:
                raise AstraInstallationError(
                    "create plans require an absent installation observation."
                )
            if self.mode in {"adopt", "adapt"} and self.inspection.observation is None:
                raise AstraInstallationError(f"{self.mode} plans require an observed installation.")
            if (
                self.mode == "adopt"
                and self.desired_spec_sha256 != self.inspection.observation.spec_sha256
            ):
                raise AstraInstallationError(
                    "adopt plans require the observed and desired spec digests to match."
                )
        references = [reference.reference for reference in self.credential_references]
        if references != sorted(references) or len(references) != len(set(references)):
            raise AstraInstallationError(
                "credential references must be unique and canonical ordered."
            )
        _timestamp(self.approval_expires_at, "approval_expires_at")
        _sha256(self.digest, "installation plan digest")
        if self.digest != digest(self.payload()):
            raise AstraInstallationError(
                "installation plan digest does not match its immutable payload."
            )

    def payload(self) -> dict[str, object]:
        return {
            "approval_expires_at": _timestamp(self.approval_expires_at, "approval_expires_at"),
            "contract_version": self.contract_version,
            "credential_references": [
                reference.payload() for reference in self.credential_references
            ],
            "desired_spec_sha256": self.desired_spec_sha256,
            "inspection": self.inspection.payload(),
            "mode": self.mode,
        }

    @classmethod
    def create(
        cls,
        *,
        mode: InstallationMode,
        inspection: InstallationInspection,
        desired_spec_sha256: str | None,
        credential_references: tuple[CredentialReference, ...] = (),
        approval_expires_at: str,
    ) -> InstallationPlan:
        if not isinstance(inspection, InstallationInspection):
            raise AstraInstallationError("installation plan requires a typed inspection.")
        if not isinstance(credential_references, tuple) or not all(
            isinstance(reference, CredentialReference) for reference in credential_references
        ):
            raise AstraInstallationError(
                "credential references must be typed sanitized references."
            )
        ordered_references = tuple(sorted(credential_references, key=lambda item: item.reference))
        payload = {
            "approval_expires_at": _timestamp(approval_expires_at, "approval_expires_at"),
            "contract_version": INSTALLATION_CONTRACT_VERSION,
            "credential_references": [reference.payload() for reference in ordered_references],
            "desired_spec_sha256": desired_spec_sha256,
            "inspection": inspection.payload(),
            "mode": mode,
        }
        return cls(
            mode=mode,
            inspection=inspection,
            desired_spec_sha256=desired_spec_sha256,
            credential_references=ordered_references,
            approval_expires_at=payload["approval_expires_at"],
            digest=digest(payload),
        )

    def is_approval_expired(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise AstraInstallationError("approval comparison time must include an offset.")
        expiry = datetime.fromisoformat(self.approval_expires_at.replace("Z", "+00:00"))
        return expiry <= now.astimezone(UTC)

    def assert_current(self) -> None:
        """Reject a mutable nested payload that changed after its digest was issued."""
        try:
            current = digest(self.payload())
        except (TypeError, ValueError) as error:
            raise AstraInstallationError(
                "installation plan payload is no longer canonical JSON."
            ) from error
        if self.digest != current:
            raise AstraInstallationError(
                "installation plan payload changed after its digest was issued."
            )

    def assert_target_unchanged(self, target: TargetIdentity) -> None:
        """Reject a plan replayed against any different cluster/context/namespace identity."""
        if not isinstance(target, TargetIdentity):
            raise AstraInstallationError("target comparison requires a typed target identity.")
        if target != self.inspection.target:
            raise AstraInstallationError("installation plan target identity changed.")

    def assert_approval_current(self, now: datetime) -> None:
        if self.is_approval_expired(now):
            raise AstraInstallationError("installation plan approval has expired.")


def _object(value: object, subject: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AstraInstallationError(f"{subject} must be a JSON object.")
    return value


def _keys(value: Mapping[str, object], expected: set[str], subject: str) -> None:
    if set(value) != expected:
        raise AstraInstallationError(f"{subject} has unsupported or missing fields.")


def _identity_from_payload(value: object) -> InstallationIdentity:
    payload = _object(value, "installation identity")
    _keys(payload, {"name", "profile"}, "installation identity")
    return InstallationIdentity(payload["name"], payload["profile"])


def _target_from_payload(value: object) -> TargetIdentity:
    payload = _object(value, "target identity")
    _keys(
        payload,
        {
            "kubeconfig_reference_sha256",
            "context",
            "server",
            "ca_sha256",
            "cluster_uid",
            "namespace",
            "namespace_uid",
        },
        "target identity",
    )
    return TargetIdentity(**payload)  # type: ignore[arg-type]


def _observation_from_payload(value: object) -> ObservedInstallation:
    payload = _object(value, "installation observation")
    _keys(
        payload,
        {"installation", "target", "uid", "spec_sha256", "observed_at", "resource_version"},
        "installation observation",
    )
    return ObservedInstallation(
        _identity_from_payload(payload["installation"]),
        _target_from_payload(payload["target"]),
        payload["uid"],
        payload["spec_sha256"],
        payload["observed_at"],
        payload["resource_version"],
    )


def _ownership_conflict_from_payload(value: object) -> OwnershipConflict:
    payload = _object(value, "ownership conflict")
    _keys(
        payload,
        {"installation", "target", "observed_uid", "owner_reference", "resource_identity"},
        "ownership conflict",
    )
    return OwnershipConflict(
        _identity_from_payload(payload["installation"]),
        _target_from_payload(payload["target"]),
        payload["observed_uid"],
        payload["owner_reference"],
        payload["resource_identity"],
    )


def _manager_conflict_from_payload(value: object) -> ManagerConflict:
    payload = _object(value, "manager conflict")
    _keys(
        payload,
        {"installation", "target", "observed_uid", "manager_reference", "resource_identity"},
        "manager conflict",
    )
    return ManagerConflict(
        _identity_from_payload(payload["installation"]),
        _target_from_payload(payload["target"]),
        payload["observed_uid"],
        payload["manager_reference"],
        payload["resource_identity"],
    )


def _finding_from_payload(value: object) -> InstallationFinding:
    payload = _object(value, "inspection finding")
    _keys(
        payload,
        {"kind", "state", "resource_reference", "observed_uid", "spec_sha256"},
        "inspection finding",
    )
    return InstallationFinding(**payload)  # type: ignore[arg-type]


def _resource_precondition_from_payload(value: object) -> ResourcePrecondition:
    payload = _object(value, "resource precondition")
    _keys(
        payload,
        {
            "resource_identity",
            "state",
            "observed_uid",
            "resource_version",
            "spec_sha256",
            "lifecycle",
        },
        "resource precondition",
    )
    return ResourcePrecondition(**payload)  # type: ignore[arg-type]


def inspection_from_payload(value: object) -> InstallationInspection:
    """Decode a strict, bounded offline snapshot; it is never live verification."""
    payload = _object(value, "installation inspection")
    _keys(
        payload,
        {
            "contract_version",
            "installation",
            "target",
            "observation",
            "ownership_conflicts",
            "manager_conflicts",
            "findings",
            "resources",
            "source",
            "verification",
        },
        "installation inspection",
    )
    if payload["verification"] != "offline_unverified":
        raise AstraInstallationError("inspection snapshots cannot claim live verification.")
    observation_value = payload["observation"]
    if observation_value is not None and not isinstance(observation_value, Mapping):
        raise AstraInstallationError("installation observation must be an object or null.")
    for field in ("ownership_conflicts", "manager_conflicts", "findings", "resources"):
        if not isinstance(payload[field], list):
            raise AstraInstallationError(f"{field} must be an array.")
    return InstallationInspection(
        installation=_identity_from_payload(payload["installation"]),
        target=_target_from_payload(payload["target"]),
        observation=(
            _observation_from_payload(observation_value) if observation_value is not None else None
        ),
        ownership_conflicts=tuple(
            _ownership_conflict_from_payload(item) for item in payload["ownership_conflicts"]
        ),
        manager_conflicts=tuple(
            _manager_conflict_from_payload(item) for item in payload["manager_conflicts"]
        ),
        findings=tuple(_finding_from_payload(item) for item in payload["findings"]),
        resources=tuple(_resource_precondition_from_payload(item) for item in payload["resources"]),
        source=payload["source"],  # type: ignore[arg-type]
        contract_version=payload["contract_version"],  # type: ignore[arg-type]
    )


def _read_json(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise AstraInstallationError("offline inspection snapshot exceeds its byte limit.")
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AstraInstallationError("offline inspection snapshot is not valid JSON.") from error
    return value


def inspection_from_json(path: Path) -> InstallationInspection:
    """Load one bounded caller-selected offline snapshot without Kubernetes access."""
    return inspection_from_payload(_read_json(Path(path)))


def inspection_json(inspection: InstallationInspection) -> str:
    """Serialize an inspection deterministically for an offline snapshot file."""
    if not isinstance(inspection, InstallationInspection):
        raise AstraInstallationError("inspection serialization requires a typed inspection.")
    return json.dumps(inspection.payload(), sort_keys=True, separators=(",", ":"))


def plan_json(plan: InstallationPlan) -> str:
    """Serialize an immutable installation plan deterministically, including its digest."""
    if not isinstance(plan, InstallationPlan):
        raise AstraInstallationError("plan serialization requires a typed installation plan.")
    plan.assert_current()
    return json.dumps(
        {**plan.payload(), "digest": plan.digest}, sort_keys=True, separators=(",", ":")
    )


def plan_from_payload(value: object) -> InstallationPlan:
    payload = _object(value, "installation plan")
    _keys(
        payload,
        {
            "approval_expires_at",
            "contract_version",
            "credential_references",
            "desired_spec_sha256",
            "inspection",
            "mode",
            "digest",
        },
        "installation plan",
    )
    if not isinstance(payload["credential_references"], list):
        raise AstraInstallationError("credential references must be an array.")
    references: list[CredentialReference] = []
    for item in payload["credential_references"]:
        reference = _object(item, "credential reference")
        _keys(reference, {"kind", "reference"}, "credential reference")
        references.append(CredentialReference(**reference))  # type: ignore[arg-type]
    return InstallationPlan(
        mode=payload["mode"],  # type: ignore[arg-type]
        inspection=inspection_from_payload(payload["inspection"]),
        desired_spec_sha256=payload["desired_spec_sha256"],  # type: ignore[arg-type]
        credential_references=tuple(references),
        approval_expires_at=payload["approval_expires_at"],  # type: ignore[arg-type]
        digest=payload["digest"],  # type: ignore[arg-type]
        contract_version=payload["contract_version"],  # type: ignore[arg-type]
    )


def plan_from_json(path: Path) -> InstallationPlan:
    """Load one bounded serialized offline plan; this never applies or verifies it."""
    return plan_from_payload(_read_json(Path(path)))


__all__ = [
    "INSTALLATION_CONTRACT_VERSION",
    "MAX_SNAPSHOT_BYTES",
    "AstraInstallationError",
    "CredentialReference",
    "FindingKind",
    "FindingState",
    "InstallationFinding",
    "InstallationIdentity",
    "InstallationInspection",
    "InstallationMode",
    "InstallationPlan",
    "ManagerConflict",
    "ObservedInstallation",
    "OwnershipConflict",
    "ResourcePrecondition",
    "TargetIdentity",
    "inspection_from_json",
    "inspection_from_payload",
    "inspection_json",
    "plan_from_json",
    "plan_from_payload",
    "plan_json",
]
