"""Phase-one wire contract; DTOs do not implement admission or native capabilities."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _wire_time(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value
    raise ValueError("Offset-aware ISO timestamp required")


Timestamp = Annotated[AwareDatetime, BeforeValidator(_wire_time)]

Ref = Annotated[str, Field(min_length=1, max_length=160)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
Units = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Phase = Literal["session-preparation", "terminal-http", "terminal-ws"]
NativePhase = Literal["native-http-fetch", "native-ws-handshake", "native-ws-frame"]
RuntimeAction = Literal["runtime:prepare", "runtime:invoke"]


class RuntimeBindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def public_fields(cls, value):
        if isinstance(value, str) and (len(value) > 512 or any(c in value for c in "\x00\r\n")):
            raise ValueError("Bounded public references only")
        return value


class RuntimePins(RuntimeBindingDTO):
    """Same field names as runtime_admission.Pins; measured by trusted owner adapter."""

    instance: Ref
    build: Digest
    sdk: Digest
    harness_schema: Digest
    effective_config: Digest
    loaded_skill: Digest
    typed_catalog: Digest
    tool_mapping: Digest
    binding: Digest
    terminal_implementation: Digest


class RuntimeScope(RuntimeBindingDTO):
    """Server-resolved runtime_admission.Scope, not caller-selected actor identity."""

    issuer: Ref
    publisher_ref: Digest
    implementation: Digest
    enrollment: Ref
    principal: Ref
    credential: Ref
    opaque_owner_ref: Digest
    project: Ref
    destination: Ref
    session_ref: Digest
    grant: Ref
    grant_revision: Revision
    grant_epoch: Revision
    policy_ref: Ref
    policy_digest: Digest
    actions_digest: Digest
    role: Literal["planner", "critic"]
    context_ref: Ref
    context_digest: Digest


class IssueRuntimePolicy(RuntimeBindingDTO):
    version: Literal["tessera-runtime-admission-v2"]
    assurance: Literal["local-enforcement-not-provider-billing"]
    max_financial_age_seconds: int = Field(strict=True, gt=0, le=86400 * 31)
    max_evidence_age_seconds: int = Field(strict=True, gt=0, le=30)
    provider_unknown: Literal["explicitly-approved"]
    max_operation_age_seconds: int = Field(strict=True, gt=0, le=30)
    allowed_actions: Annotated[tuple[RuntimeAction, ...], Field(min_length=1, max_length=2)]
    allowed_native_phases: Annotated[tuple[NativePhase, ...], Field(min_length=1, max_length=3)]
    model: Ref
    max_native_operations: int = Field(strict=True, ge=1, le=2**31 - 1)
    max_exposure_cents: Units
    exposure_per_operation_cents: Units
    expires_at: Timestamp

    @field_validator("allowed_actions", "allowed_native_phases", mode="before")
    @classmethod
    def wire_arrays(cls, value):
        return tuple(value) if type(value) is list else value

    @model_validator(mode="after")
    def policy_bounds(self):
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("Actions must be unique")
        if len(set(self.allowed_native_phases)) != len(self.allowed_native_phases):
            raise ValueError("Native phases must be unique")
        if self.exposure_per_operation_cents > self.max_exposure_cents:
            raise ValueError("Per-operation exposure exceeds policy bound")
        return self


class IssueRuntimeEnrollment(RuntimeBindingDTO):
    policy_ref: Ref
    expected_policy_revision: Revision
    subject_credential_id: Ref
    publisher_credential_id: Ref
    publisher_audience: Literal["control", "worker"]
    publisher_ref: Digest
    opaque_owner_ref: Digest
    session_ref: Digest
    destination: Ref
    role: Literal["planner", "critic"]
    context_ref: Ref
    context_digest: Digest
    parent_agent_authority_grant_id: Ref
    expected_parent_revision: Revision
    expected_parent_epoch: Revision
    pins: RuntimePins
    expires_at: Timestamp

    @model_validator(mode="after")
    def independent_publisher(self):
        if self.subject_credential_id == self.publisher_credential_id:
            raise ValueError("Publisher must be separately enrolled from model agent")
        return self


class IssueRuntimeFinancialAssertion(RuntimeBindingDTO):
    enrollment_ref: Ref
    expected_enrollment_revision: Revision
    policy_ref: Ref
    expected_policy_revision: Revision
    observed_at: Timestamp
    valid_until: Timestamp
    purchased_credit_balance: Literal[0]
    auto_top_up_off: Literal[True]
    automatic_reload_off: Literal[True]

    @field_validator(
        "purchased_credit_balance", "auto_top_up_off", "automatic_reload_off", mode="before"
    )
    @classmethod
    def exact_attestation_types(cls, value, info):
        required = int if info.field_name == "purchased_credit_balance" else bool
        if type(value) is not required:
            raise ValueError("Exact financial attestation type required")
        return value

    @model_validator(mode="after")
    def observed_before_expiry(self):
        if self.observed_at >= self.valid_until:
            raise ValueError("Observation must precede expiry")
        return self


class RevokeRuntimeRecord(RuntimeBindingDTO):
    expected_revision: Revision


class RuntimePolicyRecord(RuntimeBindingDTO):
    policy_ref: Ref
    revision: Revision
    issuer: Ref
    operator_issuer: Ref
    project: Ref
    admission_policy_digest: Digest
    profile_digest: Digest
    issued_at: Timestamp
    policy: IssueRuntimePolicy


class RuntimeFinancialRecord(RuntimeBindingDTO):
    """Immutable Financial projection; actor/owner/project/scope are server-derived."""

    evidence_class: Literal["authenticated-operator-attestation"]
    attestation_ref: Ref
    issuer: Ref
    opaque_owner_ref: Digest
    project: Ref
    destination: Ref
    policy_revision: Revision
    observed_at: Timestamp
    asserted_at: Timestamp
    valid_until: Timestamp
    purchased_credit_balance: Literal[0]
    auto_top_up_off: Literal[True]
    automatic_reload_off: Literal[True]

    @field_validator(
        "purchased_credit_balance", "auto_top_up_off", "automatic_reload_off", mode="before"
    )
    @classmethod
    def exact_attestation_types(cls, value, info):
        return IssueRuntimeFinancialAssertion.exact_attestation_types(value, info)


class RuntimeEnrollmentRecord(RuntimeBindingDTO):
    enrollment_ref: Ref
    revision: Revision
    issuer: Ref
    issued_at: Timestamp
    publisher_credential_id: Ref
    publisher_audience: Literal["control", "worker"]
    scope: RuntimeScope
    pins: RuntimePins
    expires_at: Timestamp


class ResolveRuntimeBinding(RuntimeBindingDTO):
    enrollment_ref: Ref
    expected_enrollment_revision: Revision
    financial_ref: Ref
    expected_grant_revision: Revision
    expected_grant_epoch: Revision


class RuntimeBindingSnapshot(RuntimeBindingDTO):
    version: Literal["tessera-runtime-binding-v1"]
    scope: RuntimeScope
    pins: RuntimePins
    enrollment_revision: Revision
    policy_revision: Revision
    financial_ref: Ref
    financial_issuer: Ref
    financial_observed_at: Timestamp
    financial_asserted_at: Timestamp
    financial_valid_until: Timestamp
    expires_at: Timestamp
    remaining_native_operations: Units
    remaining_exposure_cents: Units
    provider_entitlement: Literal["unknown"]
    provider_billing_guarantee: Literal["unknown"]


class AllocateRuntimeOperation(ResolveRuntimeBinding):
    """Server allocates operation/root/nonce/ticket; client never supplies retry IDs."""

    action: RuntimeAction
    phase: Phase
    native_phase: NativePhase | None = None
    pins: RuntimePins
    request_ref: Digest
    final_fingerprint: Digest
    order_revision: Revision
    sequence: Revision

    @model_validator(mode="after")
    def phase_alignment(self):
        if self.phase == "session-preparation":
            if self.action != "runtime:prepare" or self.native_phase is not None:
                raise ValueError("Preparation is not a native invocation")
        else:
            expected = (
                "terminal-http" if self.native_phase == "native-http-fetch" else "terminal-ws"
            )
            if (
                self.action != "runtime:invoke"
                or self.native_phase is None
                or self.phase != expected
            ):
                raise ValueError("Native effect phase mismatch")
        return self


class RuntimeOperation(RuntimeBindingDTO):
    operation_ref: Digest
    root_ref: Digest
    nonce: Digest
    ticket: Digest
    actor_credential_id: Ref
    binding: RuntimeBindingSnapshot
    expected: AllocateRuntimeOperation
    expires_at: Timestamp
    revision: Revision
    state: Literal["allocated", "claimed", "native-returned", "uncertain"]


class ClaimRuntimeOperation(RuntimeBindingDTO):
    expected_operation_revision: Revision
    nonce: Digest
    ticket: Digest
    expected_grant_revision: Revision
    expected_grant_epoch: Revision
    expected_enrollment_revision: Revision
    financial_ref: Ref
    local_check_ref: Digest
    quota_check_ref: Digest
    pins: RuntimePins
    request_ref: Digest
    final_fingerprint: Digest
    order_revision: Revision
    sequence: Revision


class RuntimeClaimReceipt(RuntimeBindingDTO):
    """Authenticated server readback, never a serialized native continuation permit."""

    version: Literal["tessera-runtime-claim-v1"]
    operation_ref: Digest
    root_ref: Digest
    claim_ref: Digest
    claim_revision: Revision
    actor_credential_id: Ref
    scope: RuntimeScope
    pins: RuntimePins
    nonce: Digest
    ticket: Digest
    request_ref: Digest
    final_fingerprint: Digest
    financial_ref: Ref
    local_check_ref: Digest
    quota_check_ref: Digest
    order_revision: Revision
    sequence: Revision
    reserved_native_operations: int = Field(strict=True, ge=1)
    reserved_exposure_cents: Units
    remaining_native_operations: Units
    remaining_exposure_cents: Units
    expires_at: Timestamp
    delivery: Literal["first-claim", "readback-only"]
    state: Literal["claimed", "native-returned", "uncertain"]


class SettleRuntimeOperation(RuntimeBindingDTO):
    expected_claim_revision: Revision
    claim_ref: Digest
    disposition: Literal["native-returned", "uncertain"]


class RuntimeBindingError(RuntimeBindingDTO):
    code: Literal[
        "binding_not_found",
        "actor_not_eligible",
        "binding_mismatch",
        "revision_conflict",
        "revoked",
        "expired",
        "stale_financial",
        "stale_evidence",
        "budget_exhausted",
        "operation_replayed",
        "operation_uncertain",
        "claim_mismatch",
        "idempotency_conflict",
        "native_bridge_unavailable",
    ]
