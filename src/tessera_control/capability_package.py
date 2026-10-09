"""Immutable broker wire contract. Signing keys stay on the control side."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .capability_models import (
    AdmitCapability,
    CapabilityDeclaration,
    Digest,
    Identifier,
    Strict,
)
from .store import canonical_digest


class BrokerPackage(Strict):
    version: Literal[1] = 1
    mode: Literal["synthetic", "isolated"]
    org_id: Identifier
    project_id: Identifier
    task_id: Identifier
    worker_id: Identifier
    operation_id: Identifier
    policy_id: Identifier
    grant_id: Identifier
    plan_digest: Digest
    declaration_digest: Digest
    declaration: CapabilityDeclaration
    binding: AdmitCapability
    expires_at: str = Field(max_length=40)
    max_concurrency: Literal[1] = 1

    @model_validator(mode="after")
    def complete(self):
        d, b = self.declaration, self.binding
        if (
            d.broker_basis is None
            or canonical_digest(d.model_dump(mode="json")) != self.declaration_digest
            or b.action not in d.actions
            or b.environment_id != d.environment_id
            or b.image_digest != d.image_digest
            or b.action.expected_size is None
            or b.action.content_type is None
            or b.action.expected_size > b.action.max_bytes
            or b.action.max_bytes > 16 * 1024 * 1024
            or d.credential_refs
            or b.credential_access_lease_id is not None
            or any(a.credential_ref for a in d.actions)
        ):
            raise ValueError("broker requires complete anonymous pinned authority")
        if datetime.fromisoformat(self.expires_at).tzinfo is None:
            raise ValueError("absolute expiry required")
        return self


@dataclass(frozen=True)
class SignedCapabilityPackage:
    """Only immutable canonical bytes cross the boundary; no mutable policy objects."""

    payload: bytes
    digest: str
    signature: str

    def decode(self) -> BrokerPackage:
        if type(self.payload) is not bytes or len(self.payload) > 512 * 1024:
            raise ValueError("invalid package")
        if hashlib.sha256(self.payload).hexdigest() != self.digest:
            raise ValueError("package digest mismatch")
        body = BrokerPackage.model_validate_json(self.payload)
        if encode(body) != self.payload:
            raise ValueError("noncanonical package")
        return body


def encode(body: BrokerPackage) -> bytes:
    return json.dumps(
        body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
