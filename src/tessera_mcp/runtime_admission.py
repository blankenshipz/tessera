"""Opt-in v2 local-enforcement admission; never converts diagnostic/v1 evidence.

Trusted callbacks are embedding-owned authentication/measurement ports, not JSON
flags. This verifier does not install those ports or itself execute a transport.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class AdmissionDenied(ValueError):
    pass


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def bounded_strings(cls, value):
        if isinstance(value, str) and (len(value) > 512 or any(c in value for c in "\x00\r\n")):
            raise ValueError("Bounded public fields required")
        return value


Digest = str


class Pins(Closed):
    instance: str = Field(min_length=1, max_length=160)
    build: Digest
    sdk: Digest
    harness_schema: Digest
    effective_config: Digest
    loaded_skill: Digest
    typed_catalog: Digest
    tool_mapping: Digest
    binding: Digest
    terminal_implementation: Digest

    @field_validator("*")
    @classmethod
    def bounded_pins(cls, value, info):
        if info.field_name != "instance" and (
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("SHA256 pin required")
        return value


class AdmissionPolicy(Closed):
    version: Literal["tessera-runtime-admission-v2"]
    assurance: Literal["local-enforcement-not-provider-billing"]
    admission_policy_ref: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    max_financial_age_seconds: int = Field(gt=0, le=86400 * 31)
    max_evidence_age_seconds: int = Field(gt=0, le=30)
    provider_unknown: Literal["explicitly-approved"]
    operator_issuer: str = Field(min_length=1, max_length=160)

    @property
    def digest(self):
        return public_digest(self.model_dump())


class Scope(Closed):
    issuer: str
    publisher_ref: Digest
    implementation: Digest
    enrollment: str
    principal: str
    credential: str
    opaque_owner_ref: Digest
    project: str
    destination: str
    session_ref: Digest
    grant: str
    grant_revision: int = Field(ge=1)
    grant_epoch: int = Field(ge=1)
    policy_ref: str
    policy_digest: Digest
    actions_digest: Digest
    role: Literal["planner", "critic"]
    context_ref: str
    context_digest: Digest

    @field_validator(
        "implementation",
        "opaque_owner_ref",
        "session_ref",
        "policy_digest",
        "actions_digest",
        "publisher_ref",
        "context_digest",
    )
    @classmethod
    def digest_fields(cls, value):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("SHA256 required")
        return value


class LocalChecks(Closed):
    evidence_class: Literal["completed-native-local-enforcement"]
    oauth_method: Literal["passed"]
    owner_account_match: Literal["passed"]
    openai_only: Literal["passed"]
    approved_provider_model: Literal["passed"]
    chatgpt_endpoint: Literal["passed"]
    api_key_overrides_absent: Literal["passed"]
    custom_overrides_absent: Literal["passed"]
    no_paid_fallback: Literal["passed"]
    provider: Literal["openai"]
    model: str


class Quota(Closed):
    outcome: Literal["passed"]
    checked_at: str
    snapshot_checked_at: str
    valid_until: str


class Financial(Closed):
    evidence_class: Literal["authenticated-operator-attestation"]
    attestation_ref: str
    issuer: str
    opaque_owner_ref: Digest
    project: str
    destination: str
    policy_revision: int = Field(ge=1)
    observed_at: str
    asserted_at: str
    valid_until: str
    purchased_credit_balance: Literal[0]
    auto_top_up_off: Literal[True]
    automatic_reload_off: Literal[True]

    @field_validator(
        "purchased_credit_balance", "auto_top_up_off", "automatic_reload_off", mode="before"
    )
    @classmethod
    def exact_financial_types(cls, value, info):
        if type(value) is not (int if info.field_name == "purchased_credit_balance" else bool):
            raise ValueError("Exact financial type required")
        return value


class ProviderFacts(Closed):
    entitlement: Literal["unknown"]
    billing_guarantee: Literal["unknown"]


class Evidence(Closed):
    version: Literal["tessera-runtime-admission-v2"]
    assurance: Literal["local-enforcement-not-provider-billing"]
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    ticket: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase: Literal["session-preparation", "terminal-http", "terminal-ws"]
    observed_at: str
    valid_until: str
    scope: Scope
    pins: Pins
    local: LocalChecks
    quota: Quota
    financial: Financial
    provider_facts: ProviderFacts
    order_revision: int = Field(ge=1)
    sequence: int = Field(ge=1)
    request_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    final_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class CurrentAuthority:
    """Returned only by separately enrolled current supervisor authority port."""

    scope: Scope
    expires_at: datetime
    operation: Literal["session", "dispatch"]
    revoked: bool
    budget_available: bool
    context_approved: bool
    actions: tuple[str, ...]


@dataclass(frozen=True)
class OperatorStatus:
    """Independent operator authentication/current revocation, never agent issuer."""

    assertion: Financial
    principal_kind: Literal["operator"]
    revoked: bool


@dataclass(frozen=True)
class TerminalExpectation:
    """Current native pre-effect state, after HTTP/WS mutation and lowering."""

    phase: Literal["session-preparation", "terminal-http", "terminal-ws"]
    request_ref: str
    fingerprint: str
    order_revision: int
    sequence: int
    nonce: str
    ticket: str


@dataclass(frozen=True)
class CurrentLocalState:
    """Independent current native check execution and quota, never receipt echo."""

    local: LocalChecks
    quota: Quota
    executed_at: datetime
    expected: "TerminalExpectation"


@dataclass(frozen=True)
class NativeTerminalPort:
    """Approved installed embedding, not a config-selectable plugin or flag.

    installed_pins measures actual installed mandatory terminal capability.
    bind_verifier installs verify_terminal at the native last pre-effect boundary;
    returns a tick adapter, never a post-dispatch join or continuePrepared bypass.
    No default implementation exists. Approval of this port belongs to the owner.
    """

    installed_pins: Callable
    bind_verifier: Callable


def public_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def instant(value):
    try:
        # Date-only legacy attestations cannot be upgraded to invented timestamps.
        if not isinstance(value, str) or "T" not in value or len(value) > 40:
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise AdmissionDenied("timestamp_missing_or_invalid") from None


def closed_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate evidence fields")
        result[key] = value
    return result


class AdmissionLedger:
    """One owned private SQLite replay ledger; pending/uncertain never retry."""

    def __init__(self, directory):
        directory = Path(directory)
        if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
            raise AdmissionDenied("ledger_path_invalid")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / "admission-v2.sqlite3"
        if path.is_symlink():
            raise AdmissionDenied("ledger_path_invalid")
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tickets "
            "(nonce TEXT PRIMARY KEY, ticket TEXT UNIQUE, "
            "request_scope TEXT UNIQUE, state TEXT NOT NULL)"
        )
        self.db.commit()

    def claim(self, nonce, ticket, request_scope):
        try:
            self.db.execute(
                "INSERT INTO tickets VALUES(?,?,?,?)", (nonce, ticket, request_scope, "pending")
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise AdmissionDenied("nonce_or_ticket_replayed") from None

    def finish(self, nonce, state):
        self.db.execute("UPDATE tickets SET state=? WHERE nonce=?", (state, nonce))
        self.db.commit()

    def close(self):
        self.db.close()


class RuntimeAdmissionVerifier:
    """Expected context and all trust callbacks arrive independently of receipts."""

    def __init__(
        self,
        *,
        policy,
        scope,
        pins,
        model,
        ledger,
        authorize,
        authenticate_operator,
        authenticate_evidence,
        current_local_checks,
        terminal_port,
        authority_action_namespace="verifier",
        clock=lambda: datetime.now(UTC),
    ):
        if (
            type(policy) is not AdmissionPolicy
            or type(scope) is not Scope
            or type(pins) is not Pins
            or type(ledger) is not AdmissionLedger
            or type(terminal_port) is not NativeTerminalPort
            or not all(
                callable(port)
                for port in (
                    authorize,
                    authenticate_operator,
                    authenticate_evidence,
                    current_local_checks,
                    terminal_port.installed_pins,
                    terminal_port.bind_verifier,
                )
            )
        ):
            raise AdmissionDenied("approved_admission_ports_missing")
        if scope.policy_ref != policy.admission_policy_ref or scope.policy_digest != policy.digest:
            raise AdmissionDenied("independent_policy_pin_changed")
        self.policy, self.scope, self.pins, self.model = policy, scope, pins, model
        self.ledger, self.authorize = ledger, authorize
        self.authenticate_operator, self.authenticate_evidence = (
            authenticate_operator,
            authenticate_evidence,
        )
        self.terminal_port, self.clock = terminal_port, clock
        if authority_action_namespace not in {"verifier", "runtime"}:
            raise AdmissionDenied("explicit_authority_action_namespace_required")
        self.authority_action_namespace = authority_action_namespace
        self.current_local_checks = current_local_checks
        self.check_installed()

    def check_installed(self):
        # Separate installed measurement, not receipt-provided terminal_installed=True.
        if self.terminal_port.installed_pins() != self.pins:
            raise AdmissionDenied("mandatory_terminal_capability_or_runtime_pins_changed")

    def _current(self, operation, now):
        current = self.authorize(operation)
        if (
            type(current) is not CurrentAuthority
            or current.scope != self.scope
            or current.operation != operation
            or current.revoked is not False
            or current.budget_available is not True
            or current.context_approved is not True
            or type(current.actions) is not tuple
            or (
                operation
                if self.authority_action_namespace == "verifier"
                else {"session": "runtime:prepare", "dispatch": "runtime:invoke"}[operation]
            )
            not in current.actions
            or public_digest(
                sorted(current.actions)
                if self.authority_action_namespace == "verifier"
                else {"actions": list(current.actions)}
            )
            != self.scope.actions_digest
            or current.expires_at.tzinfo is None
            or current.expires_at <= now
        ):
            raise AdmissionDenied("current_enrollment_grant_or_context_denied")
        self.check_installed()

    def verify(self, raw, *, operation, expected):
        if operation not in {"session", "dispatch"} or type(expected) is not TerminalExpectation:
            raise AdmissionDenied("explicit_operation_and_native_context_required")
        if (
            expected.phase not in {"session-preparation", "terminal-http", "terminal-ws"}
            or any(
                type(value) is not str
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
                for value in (
                    expected.nonce,
                    expected.ticket,
                    expected.request_ref,
                    expected.fingerprint,
                )
            )
            or any(
                type(value) is not int or value < 1
                for value in (expected.order_revision, expected.sequence)
            )
        ):
            raise AdmissionDenied("independent_native_context_invalid")
        if type(raw) is not bytes or len(raw) > 32768:
            raise AdmissionDenied("evidence_bounds")
        try:
            evidence = Evidence.model_validate(json.loads(raw, object_pairs_hook=closed_object))
        except (ValidationError, ValueError, TypeError):
            raise AdmissionDenied("closed_v2_evidence_required") from None
        self.ledger.claim(
            expected.nonce,
            expected.ticket,
            public_digest(
                {
                    "instance": self.pins.instance,
                    "session_ref": self.scope.session_ref,
                    "phase": expected.phase,
                    "request_ref": expected.request_ref,
                }
            ),
        )
        try:
            now = self.clock()
            if now.tzinfo is None:
                raise AdmissionDenied("aware_clock_required")
            self._current(operation, now)
            # Authentication port must return the exact authenticated bytes, not a bool.
            if self.authenticate_evidence(raw, self.scope) != hashlib.sha256(raw).hexdigest():
                raise AdmissionDenied("independent_issuer_authentication_required")
            if (
                evidence.scope != self.scope
                or evidence.pins != self.pins
                or evidence.local.model != self.model
            ):
                raise AdmissionDenied("scope_model_or_full_runtime_pins_changed")
            if (
                evidence.phase != expected.phase
                or evidence.nonce != expected.nonce
                or evidence.ticket != expected.ticket
                or evidence.request_ref != expected.request_ref
                or evidence.final_fingerprint != expected.fingerprint
                or evidence.order_revision != expected.order_revision
                or evidence.sequence != expected.sequence
                or (operation == "session") != (evidence.phase == "session-preparation")
            ):
                raise AdmissionDenied("native_final_phase_order_or_request_changed")
            at, until = instant(evidence.observed_at), instant(evidence.valid_until)
            if not (
                at <= now < until
                and (now - at).total_seconds() <= self.policy.max_evidence_age_seconds
                and (until - at).total_seconds() <= self.policy.max_evidence_age_seconds
            ):
                raise AdmissionDenied("evidence_stale_or_future")
            quota = evidence.quota
            snapshot, checked, expires = map(
                instant, (quota.snapshot_checked_at, quota.checked_at, quota.valid_until)
            )
            if not (
                snapshot <= checked <= at <= now < expires
                and (expires - snapshot).total_seconds() <= 30
            ):
                raise AdmissionDenied("quota_stale_future_or_unknown")
            native = self.current_local_checks(operation, expected)
            if (
                type(native) is not CurrentLocalState
                or native.local != evidence.local
                or native.quota != evidence.quota
                or native.expected != expected
                or native.executed_at.tzinfo is None
                or not at <= native.executed_at <= now
            ):
                raise AdmissionDenied("fresh_native_checks_or_request_context_changed")
            financial = evidence.financial
            status = self.authenticate_operator(financial.attestation_ref)
            if (
                type(status) is not OperatorStatus
                or status.principal_kind != "operator"
                or status.revoked is not False
                or status.assertion != financial
                or financial.issuer != self.policy.operator_issuer
                or financial.issuer == self.scope.principal
                or financial.opaque_owner_ref != self.scope.opaque_owner_ref
                or financial.project != self.scope.project
                or financial.destination != self.scope.destination
                or financial.policy_revision != self.policy.revision
            ):
                raise AdmissionDenied("scoped_current_operator_attestation_required")
            observed, asserted, expires = map(
                instant, (financial.observed_at, financial.asserted_at, financial.valid_until)
            )
            if not (
                observed <= asserted <= now < expires
                and (now - observed).total_seconds() <= self.policy.max_financial_age_seconds
            ):
                raise AdmissionDenied("operator_attestation_stale_or_future")
            # Recheck before releasing a one-shot validation result, never after dispatch.
            final_now = self.clock()
            self._current(operation, final_now)
            if (
                self.authenticate_operator(financial.attestation_ref) != status
                or self.current_local_checks(operation, expected) != native
                or not final_now
                < min(
                    instant(evidence.valid_until),
                    instant(quota.valid_until),
                    instant(financial.valid_until),
                )
                or (final_now - observed).total_seconds() > self.policy.max_financial_age_seconds
            ):
                raise AdmissionDenied("late_attestation_native_checks_or_expiry_changed")
            self.ledger.finish(expected.nonce, "validated")
        except Exception:
            self.ledger.finish(expected.nonce, "uncertain")
            raise AdmissionDenied("v2_admission_denied_or_uncertain") from None
        return {
            "version": evidence.version,
            "assurance": evidence.assurance,
            "state": "session-ready-no-dispatch"
            if operation == "session"
            else "terminal-validated",
            "policy_digest": self.policy.digest,
            "binding_sha256": self.pins.binding,
            "config_sha256": self.pins.effective_config,
            "skill_sha256": self.pins.loaded_skill,
            "catalog_sha256": self.pins.typed_catalog,
            "request_ref": evidence.request_ref,
            "final_fingerprint": evidence.final_fingerprint,
            "order_revision": evidence.order_revision,
            "sequence": evidence.sequence,
            "ticket": evidence.ticket,
        }

    def verify_terminal(self, raw, expected):
        return self.verify(raw, operation="dispatch", expected=expected)

    def bind_native_tick(self):
        self.check_installed()
        tick = self.terminal_port.bind_verifier(self.verify_terminal)
        if not callable(tick):
            raise AdmissionDenied("approved_terminal_tick_adapter_missing")
        return tick
