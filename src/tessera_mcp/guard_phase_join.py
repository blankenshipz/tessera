"""Non-admitting supervised-channel/owner-publisher join. No default authority.

Callbacks must be independently supplied by the authorized embedding. JSON never
establishes principal authority, actual guard execution or authentication. This
module performs no I/O, credential access, inference or admission-policy changes.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from tessera_mcp.guard_phase_evidence import (
    CorePhaseContext,
    CorePhaseReport,
    Digest,
    FinancialSettings,
    GuardPhaseExpectation,
    LocalChecks,
    PhaseEvidenceRejected,
    Positive,
    ProviderFacts,
    QuotaCheck,
    Timestamp,
    _Closed,
    _origin,
    _time,
    parse_core_model_hook_phase,
)


class OwnerGuardReceipt(_Closed):
    protocol: Literal["tessera-owner-guard-checks-v1"]
    publisher_ref: Digest
    issuer_origin: str
    nonce: Digest
    session_ref: Digest
    proof_ref: Digest
    phase: Literal["post-model-request-hooks"]
    executed_phase: Literal["model.request", "http.request", "experimental.ws.handshake"]
    order_revision: Positive
    sequence: Positive
    projection_fingerprint: Digest
    model_id: str
    executed_at: Timestamp
    valid_until: Timestamp
    local_checks: LocalChecks
    quota: QuotaCheck
    financial_settings: FinancialSettings
    provider_facts: ProviderFacts


@dataclass(frozen=True)
class OwnerPublisherTrust:
    """Approved channel reference, not a receipt-provided authorization boolean."""

    issuer_origin: str
    publisher_ref: str


@dataclass(frozen=True)
class JoinedGuardEvidence:
    phase: CorePhaseReport
    owner: OwnerGuardReceipt
    production_verified: Literal[False] = False
    admission_allowed: Literal[False] = False
    publisher_provenance: str = "authorized_callback_claim_not_independent_guard_execution_proof"

    def admission_fields(self):
        raise PhaseEvidenceRejected()

    def notebook_projection(self):
        # Typed closed data only. No raw callback bodies, auth/identifiers or source options.
        return {
            "protocol": "tessera-joined-guard-evidence-v1",
            "admission_allowed": False,
            "production_verified": False,
            "phase": self.phase.receipt.model_dump(),
            "owner_checks": self.owner.model_dump(),
            "financial_freshness": "unknown",
            "proof_limit": "issuer_claims_not_independent_truth_or_final_transport",
        }


def _json(raw):
    if type(raw) is not bytes or not 1 <= len(raw) <= 16384:
        raise ValueError()

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError()
            value[key] = item
        return value

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
    )


def join_owner_guard_checks(
    phase: CorePhaseReport, raw: bytes, *, trust: OwnerPublisherTrust, now: datetime
) -> JoinedGuardEvidence:
    """Join separate sanitized publisher output, not source/config/default facts."""
    try:
        if type(phase) is not CorePhaseReport or type(trust) is not OwnerPublisherTrust:
            raise ValueError()
        owner = OwnerGuardReceipt.model_validate(_json(raw))
        receipt = phase.receipt
        if (
            _origin(owner.issuer_origin) != _origin(trust.issuer_origin)
            or owner.publisher_ref != trust.publisher_ref
        ):
            raise ValueError()
        for name in ("nonce", "session_ref", "proof_ref", "phase", "order_revision", "sequence"):
            if getattr(owner, name) != getattr(receipt, name):
                raise ValueError()
        if (
            owner.projection_fingerprint != receipt.projection.fingerprint
            or owner.model_id != receipt.projection.model_id
            or not _time(receipt.observed_at) <= now < _time(receipt.valid_until)
        ):
            raise ValueError()
        executed, expires = _time(owner.executed_at), _time(owner.valid_until)
        if not executed <= now < expires or not 0 < (expires - executed).total_seconds() <= 30:
            raise ValueError()
        if any(
            value != "passed"
            for name, value in owner.local_checks.model_dump().items()
            if name != "evidence_class"
        ):
            raise ValueError()
        quota = owner.quota
        checked, valid = _time(quota.checked_at), _time(quota.valid_until)
        if quota.outcome != "passed" or quota.snapshot_checked_at is None:
            raise ValueError()
        snapshot = _time(quota.snapshot_checked_at)
        if (
            not snapshot <= checked <= now < valid
            or not 0 <= (now - snapshot).total_seconds() <= 30
            or not 0 < (valid - checked).total_seconds() <= 30
            or valid > snapshot + timedelta(seconds=30)
        ):
            raise ValueError()
        financial = owner.financial_settings
        if (
            not financial.auto_top_up_off
            or not financial.automatic_reload_off
            or not financial.purchased_credit_balance_zero
            or financial.verified_at is None
        ):
            raise ValueError()
        # Presence is not independent UI verification or proof of current financial settings.
        datetime.fromisoformat(financial.verified_at.replace("Z", "+00:00"))
        return JoinedGuardEvidence(phase, owner)
    except Exception:
        raise PhaseEvidenceRejected() from None


class SupervisedPhaseObserver:
    """Exact SDK read/consume callback port; no JSON boolean installs authority.

    Trusted embedding wraps ModelPhaseChannel.read/consume in its existing live
    Effect/authority scope. Callbacks return bounded safe receipt bytes only. SDK
    itself reauthorizes/live-checks; parser does not infer that from serialized data.
    No default callbacks, principal, owner publisher or fallback is provided.
    """

    def __init__(
        self,
        *,
        read_phase,
        consume_phase,
        expected: GuardPhaseExpectation,
        context: CorePhaseContext,
        clock,
    ):
        if not callable(read_phase) or not callable(consume_phase):
            raise PhaseEvidenceRejected()
        self._read, self._consume = read_phase, consume_phase
        self.expected, self.context, self.clock = expected, context, clock
        self._consumed = False

    def read(self):
        if self._consumed:
            raise PhaseEvidenceRejected()
        try:
            return parse_core_model_hook_phase(
                self._read(), expected=self.expected, context=self.context, now=self.clock()
            )
        except Exception:
            raise PhaseEvidenceRejected() from None

    def consume(self):
        if self._consumed:
            raise PhaseEvidenceRejected()
        # A failed consume is uncertain and cannot be replayed/fallback-authorized here.
        self._consumed = True
        try:
            return parse_core_model_hook_phase(
                self._consume(), expected=self.expected, context=self.context, now=self.clock()
            )
        except Exception:
            raise PhaseEvidenceRejected() from None

    def consume_and_join(self, *, owner_publish, owner_trust):
        if not callable(owner_publish):
            raise PhaseEvidenceRejected()
        phase = self.consume()
        try:
            raw = owner_publish(phase.receipt)
            return join_owner_guard_checks(phase, raw, trust=owner_trust, now=self.clock())
        except Exception:
            raise PhaseEvidenceRejected() from None
