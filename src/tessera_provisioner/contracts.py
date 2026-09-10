"""Versioned, secret-free contracts for fake environment provisioning.

These structures intentionally do not describe Kubernetes clients, namespaces,
PVCs, Secrets, or credentials.  They make a selected target and every intended
resource digestible before a human authorizes fake reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

CONTRACT_VERSION = "environment-provisioning-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_FORBIDDEN_RESOURCE_TYPES = frozenset({"namespace", "persistentvolumeclaim", "secret"})


class ProvisioningContractError(ValueError):
    """A plan cannot be safely constructed or reconciled."""


class LostReplyError(ProvisioningContractError):
    """A fake side effect may have occurred and must be reconciled by identity."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ProvisioningContractError(f"{field} must be a bounded lowercase identifier.")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ProvisioningContractError(f"{field} must be a SHA-256 digest.")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProvisioningContractError(f"{field} must be an RFC 3339 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProvisioningContractError(f"{field} must be an RFC 3339 timestamp.") from error
    if parsed.tzinfo is None:
        raise ProvisioningContractError(f"{field} must include an offset.")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ResourceIntent:
    """One named, target-scoped fake resource, with no namespace or Secret support."""

    resource_type: str
    name: str
    spec: dict[str, Any]

    def __post_init__(self) -> None:
        resource_type = _identifier(self.resource_type, "resource_type")
        if resource_type in _FORBIDDEN_RESOURCE_TYPES:
            raise ProvisioningContractError(
                "Phase 1 resource intents cannot provision namespaces, PVCs, or Secrets."
            )
        _identifier(self.name, "resource name")
        if not isinstance(self.spec, dict):
            raise ProvisioningContractError("resource spec must be an object.")
        try:
            encoded = canonical_json(self.spec)
        except (TypeError, ValueError) as error:
            raise ProvisioningContractError("resource spec must be canonical JSON.") from error
        if len(encoded) > 16_384:
            raise ProvisioningContractError("resource spec exceeds the Phase 1 bound.")

    @property
    def identity(self) -> str:
        return f"{self.resource_type}/{self.name}"

    def payload(self) -> dict[str, object]:
        return {"name": self.name, "resource_type": self.resource_type, "spec": self.spec}


@dataclass(frozen=True)
class ProvisioningPlan:
    """An immutable plan whose digest is the sole approval and replay basis."""

    tenant_id: str
    target_id: str
    target_revision: int
    target_identity_digest: str
    expires_at: str
    resource_intents: tuple[ResourceIntent, ...]
    digest: str
    contract_version: str = CONTRACT_VERSION
    namespace: None = None
    persistent_volume_claim: None = None

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "tenant_id")
        _identifier(self.target_id, "target_id")
        if type(self.target_revision) is not int or self.target_revision < 1:
            raise ProvisioningContractError("target_revision must be a positive integer.")
        _sha256(self.target_identity_digest, "target_identity_digest")
        _timestamp(self.expires_at, "expires_at")
        if self.contract_version != CONTRACT_VERSION:
            raise ProvisioningContractError("unsupported provisioning contract version.")
        if self.namespace is not None or self.persistent_volume_claim is not None:
            raise ProvisioningContractError("Phase 1 plans must omit namespace and PVC selection.")
        identities = [intent.identity for intent in self.resource_intents]
        if len(identities) != len(set(identities)):
            raise ProvisioningContractError("resource intents must have unique identities.")
        if identities != sorted(identities):
            raise ProvisioningContractError("resource intents must use canonical identity order.")
        _sha256(self.digest, "plan digest")
        if self.digest != digest(self.payload()):
            raise ProvisioningContractError(
                "plan digest does not match the immutable plan payload."
            )

    def payload(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "expires_at": _timestamp(self.expires_at, "expires_at"),
            "namespace": None,
            "persistent_volume_claim": None,
            "resource_intents": [intent.payload() for intent in self.resource_intents],
            "target_id": self.target_id,
            "target_identity_digest": self.target_identity_digest,
            "target_revision": self.target_revision,
            "tenant_id": self.tenant_id,
        }

    def assert_current(self) -> None:
        """Reject a plan whose nested mutable input was changed after hashing."""
        try:
            current_digest = digest(self.payload())
        except (TypeError, ValueError) as error:
            raise ProvisioningContractError("plan payload is no longer canonical JSON.") from error
        if self.digest != current_digest:
            raise ProvisioningContractError("plan payload changed after its digest was issued.")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        target_id: str,
        target_revision: int,
        target_identity_digest: str,
        expires_at: str,
        resource_intents: tuple[ResourceIntent, ...],
    ) -> ProvisioningPlan:
        ordered = tuple(sorted(resource_intents, key=lambda intent: intent.identity))
        payload = {
            "contract_version": CONTRACT_VERSION,
            "expires_at": _timestamp(expires_at, "expires_at"),
            "namespace": None,
            "persistent_volume_claim": None,
            "resource_intents": [intent.payload() for intent in ordered],
            "target_id": target_id,
            "target_identity_digest": target_identity_digest,
            "target_revision": target_revision,
            "tenant_id": tenant_id,
        }
        return cls(
            tenant_id=tenant_id,
            target_id=target_id,
            target_revision=target_revision,
            target_identity_digest=target_identity_digest,
            expires_at=payload["expires_at"],
            resource_intents=ordered,
            digest=digest(payload),
        )

    def is_expired(self, now: datetime) -> bool:
        return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")) <= now.astimezone(UTC)


__all__ = [
    "CONTRACT_VERSION",
    "LostReplyError",
    "ProvisioningContractError",
    "ProvisioningPlan",
    "ResourceIntent",
    "canonical_json",
    "digest",
]
