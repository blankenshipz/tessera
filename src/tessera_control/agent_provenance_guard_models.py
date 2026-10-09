"""Optional closed guard observations, not admission or receipt authentication."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tessera_mcp.guard_phase_evidence import (
    CoreModelHookPhaseReceipt,
    Digest,
    Positive,
    _Closed,
    _origin,
)
from tessera_mcp.guard_phase_join import OwnerGuardReceipt


class PublicGuardPhaseBinding(_Closed):
    """Human-approved challenge/channel expectations; opaque refs do not prove origin."""

    issuer_origin: str = Field(max_length=512)
    enrollment_ref: Digest
    adapter_ref: Digest
    nonce: Digest
    session_ref: Digest
    proof_ref: Digest
    order_revision: Positive
    sequence: Positive
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    provider_sha256: Digest
    model_sha256: Digest
    tool_policy_sha256: Digest
    owner_issuer_origin: str | None = Field(default=None, max_length=512)
    owner_publisher_ref: Digest | None = None

    @model_validator(mode="after")
    def closed_origin(self):
        _origin(self.issuer_origin)
        _ = urlsplit(self.issuer_origin).port  # Reject malformed/non-numeric ports.
        if (self.owner_issuer_origin is None) != (self.owner_publisher_ref is None):
            raise ValueError(
                "Owner publisher expectations must be independently supplied together."
            )
        if self.owner_issuer_origin is not None:
            _origin(self.owner_issuer_origin)
            _ = urlsplit(self.owner_issuer_origin).port
        if max(self.order_revision, self.sequence) > 2**53 - 1:
            raise ValueError("Guard phase counters exceed the bounded channel contract.")
        return self


class GuardPhaseObservation(_Closed):
    protocol: Literal["tessera-provenance-guard-phase-v1"]
    phase: CoreModelHookPhaseReceipt
    owner_checks: OwnerGuardReceipt | None = None
    admission_allowed: Literal[False] = False
    production_verified: Literal[False] = False
    consumption_verified: Literal[False] = False
    financial_freshness: Literal["unknown"] = "unknown"
    proof_limit: Literal["unsigned_refs_do_not_prove_origin_guard_execution_or_final_transport"] = (
        "unsigned_refs_do_not_prove_origin_guard_execution_or_final_transport"
    )

    @model_validator(mode="before")
    @classmethod
    def literal_bools(cls, value):
        if isinstance(value, dict):
            for key in ("admission_allowed", "production_verified", "consumption_verified"):
                if key in value and type(value[key]) is not bool:
                    raise ValueError("Observation qualifiers require exact booleans.")
            phase = value.get("phase")
            if isinstance(phase, dict):
                for key in ("complete", "ordering_enforced"):
                    if key in phase and type(phase[key]) is not bool:
                        raise ValueError("Core phase qualifiers require exact booleans.")
        return value
