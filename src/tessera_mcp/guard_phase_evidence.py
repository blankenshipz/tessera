"""Closed, privacy-safe phase receipt contract; never an admission verifier.

The isolated core partial producer has no installed authenticated channel. A parsed
receipt is an unverified source claim, including when carried by an authenticated
origin (which proves who, not truth). Existing admission and billing are unchanged.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Positive = Annotated[int, Field(strict=True, ge=1)]
Timestamp = Annotated[str, Field(min_length=20, max_length=40)]
Outcome = Literal["passed", "denied", "unavailable"]
Check = Literal["passed", "failed", "unknown"]
PHASE = "post-model-request-hooks"


class PhaseEvidenceRejected(ValueError):
    def __init__(self):
        super().__init__("guard_phase_evidence_invalid_or_not_admitting")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LocalChecks(_Closed):
    evidence_class: Literal["local_measured_enforcement"]
    openai_only: Check
    chatgpt_route: Check
    chatgpt_oauth_method: Check
    account_match: Check
    api_key_overrides_absent: Check


class FinancialSettings(_Closed):
    evidence_class: Literal["operator_attested_financial_settings"]
    auto_top_up_off: bool
    automatic_reload_off: bool
    purchased_credit_balance_zero: bool
    verified_at: Annotated[str, Field(min_length=10, max_length=40)] | None
    freshness: Literal["unknown"]


class ProviderFacts(_Closed):
    evidence_class: Literal["provider_unknown"]
    entitlement: Literal["unknown"]
    billing_guarantee: Literal["unknown"]


class QuotaCheck(_Closed):
    evidence_class: Literal["local_measured_enforcement"]
    outcome: Outcome
    checked_at: Timestamp
    snapshot_checked_at: Timestamp | None
    valid_until: Timestamp


class Projection(_Closed):
    provider: Literal["openai"]
    model_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]
    provider_sha256: Digest
    model_sha256: Digest
    tool_policy_sha256: Digest
    fingerprint: Digest


class GuardPhaseReceipt(_Closed):
    protocol: Literal["tessera-guard-phase-evidence-v1"]
    phase: Literal["post-model-request-hooks"]
    complete: Literal[False]
    phase_finality: Literal["unproven"]
    ordering_enforced: Literal[False]
    nonce: Digest
    session_ref: Digest
    proof_ref: Digest
    issuer_origin: Annotated[str, Field(max_length=512)]
    order_revision: Positive
    sequence: Positive
    observed_at: Timestamp
    valid_until: Timestamp
    projection: Projection
    local_checks: LocalChecks
    financial_settings: FinancialSettings
    provider_facts: ProviderFacts
    quota: QuotaCheck


@dataclass(frozen=True)
class GuardPhaseExpectation:
    """Explicit caller expectations; not producer observations or trusted proof."""

    issuer_origin: str
    nonce: str
    session_ref: str
    proof_ref: str
    order_revision: int
    sequence: int
    model_id: str
    provider_sha256: str
    model_sha256: str
    tool_policy_sha256: str


@dataclass(frozen=True)
class GuardPhaseReport:
    receipt: GuardPhaseReceipt
    production_verified: Literal[False] = False
    admission_allowed: Literal[False] = False
    observation_provenance: str = "unverified_typed_phase_receipt"
    origin_authentication: str = "not_observed_by_parser"
    financial_freshness: str = "unknown"

    def admission_fields(self):
        raise PhaseEvidenceRejected()


class CoreLocalEnforcement(_Closed):
    evidence_class: Literal["local_measured_enforcement"]
    model_request_hook_executor: Literal["ordered_snapshot"]
    scope: Literal["model-request-hook-stage-only"]


class CoreFinancialNotSupplied(_Closed):
    evidence_class: Literal["operator_attested_financial_settings"]
    observation: Literal["not_supplied"]
    freshness: Literal["unknown"]


class CoreModelHookPhaseReceipt(_Closed):
    protocol: Literal["opencode-model-hook-phase-v1"]
    phase: Literal["post-model-request-hooks"]
    complete: Literal[False]
    phase_finality: Literal["unproven"]
    ordering_enforced: Literal[False]
    nonce: Digest
    session_ref: Digest
    proof_ref: Digest
    issuer_origin: Annotated[str, Field(max_length=512)]
    order_revision: Positive
    sequence: Positive
    observed_at: Timestamp
    valid_until: Timestamp
    projection: Projection
    local_enforcement: CoreLocalEnforcement
    financial_settings: CoreFinancialNotSupplied
    provider_facts: ProviderFacts


@dataclass(frozen=True)
class CorePhaseContext:
    """Independent caller channel/enrollment/challenge binding, not input JSON.

    The adapter caller is responsible for establishing this context. Its opaque
    references do not turn unsigned JSON into authentication or consumption proof.
    """

    bound_origin: str
    enrollment_ref: str
    adapter_ref: str
    nonce: str
    session_ref: str
    proof_ref: str


@dataclass(frozen=True)
class CorePhaseReport:
    receipt: CoreModelHookPhaseReceipt
    context: CorePhaseContext
    production_verified: Literal[False] = False
    admission_allowed: Literal[False] = False
    consumption_verified: Literal[False] = False
    observation_provenance: str = "unsigned_core_phase_receipt_bound_to_caller_context"
    origin_authentication: str = "caller_context_not_independently_verified_by_parser"
    missing: tuple[str, ...] = (
        "guard_account_route_checks",
        "quota_outcome",
        "financial_attestation",
        "authenticated_producer_consumption",
        "final_transport",
        "provider_entitlement",
    )

    def admission_fields(self):
        raise PhaseEvidenceRejected()


def phase_fingerprint(
    *, order_revision, sequence, model_id, provider_sha256, model_sha256, tool_policy_sha256
):
    """Digest of safe phase/policy projection, not config/options or secret values."""
    value = {
        "phase": PHASE,
        "order_revision": order_revision,
        "sequence": sequence,
        "provider": "openai",
        "model_id": model_id,
        "provider_sha256": provider_sha256,
        "model_sha256": model_sha256,
        "tool_policy_sha256": tool_policy_sha256,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _time(value):
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError()
    return timestamp


def _origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError()
    return value


def parse_guard_phase_evidence(
    raw: bytes, *, expected: GuardPhaseExpectation, now: datetime
) -> GuardPhaseReport:
    """Validate phase, binding, freshness and closed shapes, but never establish truth.

    Bytes-only boundary rejects caller config mappings without traversing getters.
    Native source outcomes are sanitized enums; no auth/account IDs, headers,
    prompts, frames, credential/config objects or inferred provider facts accepted.
    No network, file/environment/credential access, retries or side effects.
    """
    try:
        if (
            type(raw) is not bytes
            or not 1 <= len(raw) <= 16384
            or type(expected) is not GuardPhaseExpectation
            or now.tzinfo is None
        ):
            raise ValueError()

        # Reject duplicate keys rather than accepting JSON's last-write-wins shape.
        def closed_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value

        value = json.loads(
            raw,
            object_pairs_hook=closed_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if (
            type(value.get("complete")) is not bool
            or type(value.get("ordering_enforced")) is not bool
        ):
            raise ValueError()
        receipt = GuardPhaseReceipt.model_validate(value)
        if _origin(receipt.issuer_origin) != _origin(expected.issuer_origin):
            raise ValueError()
        for field in ("nonce", "session_ref", "proof_ref", "order_revision", "sequence"):
            if getattr(receipt, field) != getattr(expected, field):
                raise ValueError()
        for field in ("model_id", "provider_sha256", "model_sha256", "tool_policy_sha256"):
            if getattr(receipt.projection, field) != getattr(expected, field):
                raise ValueError()
        fingerprint = phase_fingerprint(
            **{
                field: getattr(expected, field)
                for field in (
                    "order_revision",
                    "sequence",
                    "model_id",
                    "provider_sha256",
                    "model_sha256",
                    "tool_policy_sha256",
                )
            }
        )
        if receipt.projection.fingerprint != fingerprint:
            raise ValueError()
        observed, expires = _time(receipt.observed_at), _time(receipt.valid_until)
        if not observed <= now < expires or not 0 < (expires - observed).total_seconds() <= 30:
            raise ValueError()
        quota = receipt.quota
        checked, quota_expiry = _time(quota.checked_at), _time(quota.valid_until)
        if (
            not checked <= now < quota_expiry
            or not 0 < (quota_expiry - checked).total_seconds() <= 30
        ):
            raise ValueError()
        if quota.snapshot_checked_at is None:
            if quota.outcome != "unavailable":
                raise ValueError()
        else:
            snapshot = _time(quota.snapshot_checked_at)
            if (
                not 0 <= (now - snapshot).total_seconds() <= 30
                or snapshot > checked
                or quota_expiry > snapshot + timedelta(seconds=30)
            ):
                raise ValueError()
        verified = receipt.financial_settings.verified_at
        if verified is not None:
            # Preserve date-only operator evidence; never invent verification time/age.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified):
                date.fromisoformat(verified)
            else:
                _time(verified)
        return GuardPhaseReport(receipt)
    except Exception:
        raise PhaseEvidenceRejected() from None


def parse_core_model_hook_phase(
    raw: bytes, *, expected: GuardPhaseExpectation, context: CorePhaseContext, now: datetime
) -> CorePhaseReport:
    """Adapt exact core partial protocol, never synthesize a full guard receipt.

    Requires separately supplied origin/enrollment/challenge context. Even valid
    JSON cannot establish that lease.consume() rechecked live core ordering; the
    producer/channel caller must do that before serialization. Truth stays unproven.
    """
    try:
        if (
            type(raw) is not bytes
            or not 1 <= len(raw) <= 16384
            or type(context) is not CorePhaseContext
            or type(expected) is not GuardPhaseExpectation
            or now.tzinfo is None
        ):
            raise ValueError()

        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value

        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if (
            type(value.get("complete")) is not bool
            or type(value.get("ordering_enforced")) is not bool
        ):
            raise ValueError()
        receipt = CoreModelHookPhaseReceipt.model_validate(value)
        if (
            _origin(context.bound_origin) != _origin(expected.issuer_origin)
            or _origin(receipt.issuer_origin) != context.bound_origin
            or any(
                not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref)
                for ref in (context.enrollment_ref, context.adapter_ref)
            )
        ):
            raise ValueError()
        for field in ("nonce", "session_ref", "proof_ref"):
            if getattr(receipt, field) != getattr(expected, field) or getattr(
                context, field
            ) != getattr(expected, field):
                raise ValueError()
        for field in ("order_revision", "sequence"):
            if type(getattr(expected, field)) is not int or getattr(receipt, field) != getattr(
                expected, field
            ):
                raise ValueError()
        for field in ("model_id", "provider_sha256", "model_sha256", "tool_policy_sha256"):
            if getattr(receipt.projection, field) != getattr(expected, field):
                raise ValueError()
        fingerprint = phase_fingerprint(
            **{
                field: getattr(expected, field)
                for field in (
                    "order_revision",
                    "sequence",
                    "model_id",
                    "provider_sha256",
                    "model_sha256",
                    "tool_policy_sha256",
                )
            }
        )
        if receipt.projection.fingerprint != fingerprint:
            raise ValueError()
        observed, expires = _time(receipt.observed_at), _time(receipt.valid_until)
        if not observed <= now < expires or not 0 < (expires - observed).total_seconds() <= 30:
            raise ValueError()
        return CorePhaseReport(receipt, context)
    except Exception:
        raise PhaseEvidenceRejected() from None
