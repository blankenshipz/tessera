"""Authenticated binding client and issuer-co-located, bounded native IPC gate.

HTTP readbacks are never synchronous invocation authority. The issuer must register
the lease validator on its own SQLite connection and native measurement boundary.
No token discovery, human-token proxy, JSON authorization or successful defaults.
"""

import hashlib
import json
import os
import selectors
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tessera_control.runtime_binding_models import (
    AllocateRuntimeOperation,
    ClaimRuntimeOperation,
    ResolveRuntimeBinding,
    RuntimeBindingSnapshot,
    RuntimeClaimReceipt,
    RuntimeEnrollmentRecord,
    RuntimeFinancialRecord,
    RuntimeOperation,
    RuntimePolicyRecord,
    SettleRuntimeOperation,
)

from .harness_binding import HarnessBinding
from .runtime_admission import (
    AdmissionDenied,
    AdmissionPolicy,
    CurrentAuthority,
    CurrentLocalState,
    Financial,
    NativeTerminalPort,
    OperatorStatus,
    Pins,
    RuntimeAdmissionVerifier,
    Scope,
    TerminalExpectation,
    closed_object,
    instant,
)


class BindingUnavailable(AdmissionDenied):
    """Fixed code only; response bodies, credentials and native errors stay private."""


class RuntimeBindingClient:
    def __init__(self, *, origin, audience, credential_reference, destination, http=None):
        url = httpx.URL(origin)
        if (
            url.scheme != "https"
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or audience not in {"control", "worker"}
            or not callable(credential_reference)
        ):
            raise BindingUnavailable("approved_publisher_transport_required")
        self.origin = str(url).rstrip("/")
        self.audience, self.credential_reference, self.destination = (
            audience,
            credential_reference,
            destination,
        )
        self.http = http if http is not None else httpx.Client(timeout=10, follow_redirects=False)
        self._owns_http = http is None

    @classmethod
    def from_runtime_reference(cls, *, credential_file, **kwargs):
        # Exact owner-supplied reference only, never discovery/copy or inherited token.
        from tessera_agent.remote_reasoning_store import private_file

        return cls(
            credential_reference=lambda: private_file(Path(credential_file)).decode().strip(),
            **kwargs,
        )

    def _request(self, method, route, model, *, payload=None, key=None):
        if not route.startswith("/runtime-") or any(c in route for c in "?#\r\n"):
            raise BindingUnavailable("fixed_runtime_route_required")
        prefix = "/worker" if self.audience == "worker" else ""
        try:
            token = self.credential_reference()
            if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
                raise ValueError()
            headers = {"Authorization": "Bearer " + token, "X-Agent-Destination": self.destination}
            if method == "POST":
                if not isinstance(key, str) or not 1 <= len(key) <= 160:
                    raise ValueError()
                headers["Idempotency-Key"] = key
            response = self.http.request(
                method,
                self.origin + prefix + route,
                headers=headers,
                json=payload.model_dump(mode="json") if payload is not None else None,
                follow_redirects=False,
            )
            if response.status_code not in {200, 201} or len(response.content) > 65536:
                raise ValueError()
            result = model.model_validate(
                json.loads(response.content, object_pairs_hook=closed_object)
            )
        except Exception:
            # No automatic retry, redirect, error-body logging or credential fallback.
            raise BindingUnavailable("runtime_binding_reply_denied_or_uncertain") from None
        return response.status_code, result

    def resolve(self, payload: ResolveRuntimeBinding, key):
        status, value = self._request(
            "POST", "/runtime-bindings/resolve", RuntimeBindingSnapshot, payload=payload, key=key
        )
        if status != 200:
            raise BindingUnavailable("binding_snapshot_status_mismatch")
        return value

    def financial(self, ref):
        self._ref(ref)
        status, value = self._request(
            "GET", "/runtime-financial-assertions/" + ref, RuntimeFinancialRecord
        )
        if status != 200:
            raise BindingUnavailable("financial_read_status_mismatch")
        return value

    def allocate(self, payload: AllocateRuntimeOperation, key):
        status, value = self._request(
            "POST", "/runtime-operations", RuntimeOperation, payload=payload, key=key
        )
        if status not in {200, 201} or value.expected != payload or value.state != "allocated":
            raise BindingUnavailable("allocated_operation_mismatch_or_consumed")
        return value

    def claim(self, operation, payload: ClaimRuntimeOperation, key):
        self._ref(operation.operation_ref)
        status, value = self._request(
            "POST",
            "/runtime-operations/" + operation.operation_ref + "/claim",
            RuntimeClaimReceipt,
            payload=payload,
            key=key,
        )
        if status != 201 or value.delivery != "first-claim" or value.state != "claimed":
            raise BindingUnavailable("claim_readback_is_not_continuation")
        if (
            value.operation_ref != operation.operation_ref
            or value.root_ref != operation.root_ref
            or value.actor_credential_id != operation.actor_credential_id
            or value.scope != operation.binding.scope
            or value.pins != payload.pins
            or value.nonce != payload.nonce
            or value.ticket != payload.ticket
            or value.request_ref != payload.request_ref
            or value.final_fingerprint != payload.final_fingerprint
            or value.financial_ref != payload.financial_ref
            or value.local_check_ref != payload.local_check_ref
            or value.quota_check_ref != payload.quota_check_ref
            or value.order_revision != payload.order_revision
            or value.sequence != payload.sequence
            or value.reserved_native_operations != 1
        ):
            raise BindingUnavailable("claim_scope_or_reservation_mismatch")
        return value

    def settle(self, claim, disposition, key):
        payload = SettleRuntimeOperation(
            expected_claim_revision=claim.claim_revision,
            claim_ref=claim.claim_ref,
            disposition=disposition,
        )
        status, result = self._request(
            "POST",
            "/runtime-operations/" + claim.operation_ref + "/settle",
            RuntimeOperation,
            payload=payload,
            key=key,
        )
        if (
            status != 200
            or result.operation_ref != claim.operation_ref
            or result.state != disposition
        ):
            raise BindingUnavailable("settlement_mismatch_or_uncertain")
        return result

    @staticmethod
    def _ref(ref):
        if (
            not isinstance(ref, str)
            or not ref
            or ref[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            or len(ref) > 160
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
                for c in ref
            )
        ):
            raise BindingUnavailable("opaque_runtime_reference_required")

    def close(self):
        if self._owns_http:
            self.http.close()


@dataclass(frozen=True)
class OwnerCheckExecution:
    """Approved owner producer output, not an arbitrary JSON guard verdict.

    check references must resolve through the SAME registered producer in Store.
    installed_pins is separately measured full runtime/SDK/config/skill/catalog.
    """

    local_check_ref: str
    quota_check_ref: str
    evidence: bytes
    current: CurrentLocalState
    installed_pins: Pins


@dataclass(frozen=True)
class IssuerInvocationState:
    """Fresh Store transaction result, never a deserialized HTTP lease/Boolean."""

    claim: RuntimeClaimReceipt
    snapshot: RuntimeBindingSnapshot
    financial: RuntimeFinancialRecord
    checks: OwnerCheckExecution
    policy: RuntimePolicyRecord


class EnrolledAdmissionAdapter:
    def __init__(
        self,
        *,
        client,
        enrollment,
        policy,
        financial_ref,
        owner_producer,
        installed_pins,
        ledger,
        terminal_port,
        publisher_credential_id,
        binding,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            type(client) is not RuntimeBindingClient
            or type(enrollment) is not RuntimeEnrollmentRecord
            or type(policy) is not RuntimePolicyRecord
            or not callable(owner_producer)
            or not callable(installed_pins)
            or type(terminal_port) is not NativeTerminalPort
            or enrollment.publisher_audience != client.audience
            or enrollment.publisher_credential_id != publisher_credential_id
            or enrollment.scope.policy_ref != policy.policy_ref
            or type(binding) is not HarnessBinding
        ):
            raise BindingUnavailable("approved_enrolled_admission_ports_required")
        self.client, self.enrollment, self.record = client, enrollment, policy
        self.financial_ref, self.owner_producer, self.installed_pins = (
            financial_ref,
            owner_producer,
            installed_pins,
        )
        self.scope = Scope.model_validate(enrollment.scope.model_dump())
        self.pins = Pins.model_validate(enrollment.pins.model_dump())
        projected = {
            name: getattr(policy.policy, name)
            for name in (
                "version",
                "assurance",
                "max_financial_age_seconds",
                "max_evidence_age_seconds",
                "provider_unknown",
            )
        }
        self.policy = AdmissionPolicy(
            **projected,
            admission_policy_ref=policy.policy_ref,
            revision=policy.revision,
            operator_issuer=policy.operator_issuer,
        )
        if (
            self.policy.digest != policy.admission_policy_digest
            or self.scope.policy_digest != self.policy.digest
        ):
            raise BindingUnavailable("server_policy_projection_mismatch")
        metadata = json.loads(binding.manifest).get("runtime_admission")
        if binding.digest != self.pins.binding or metadata != {
            "version": "tessera-runtime-admission-v2",
            "policy_ref": policy.policy_ref,
            "admission_policy_digest": policy.admission_policy_digest,
            "profile_digest": policy.profile_digest,
        }:
            raise BindingUnavailable("measured_binding_must_bind_full_runtime_profile")
        self.ledger, self.terminal_port = ledger, terminal_port
        self.clock = clock
        self._execution = None
        self._lease = None

    def _resolve(self):
        if self._lease is not None:
            self._lease.assert_current()
            snapshot = self._lease.state.snapshot
            if snapshot.scope != self.enrollment.scope or snapshot.pins != self.enrollment.pins:
                raise BindingUnavailable("issuer_lease_binding_changed")
            return snapshot
        payload = ResolveRuntimeBinding(
            enrollment_ref=self.enrollment.enrollment_ref,
            expected_enrollment_revision=self.enrollment.revision,
            financial_ref=self.financial_ref,
            expected_grant_revision=self.scope.grant_revision,
            expected_grant_epoch=self.scope.grant_epoch,
        )
        snapshot = self.client.resolve(
            payload, "current-" + hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        )
        if (
            snapshot.scope != self.enrollment.scope
            or snapshot.pins != self.enrollment.pins
            or snapshot.enrollment_revision != self.enrollment.revision
            or snapshot.policy_revision != self.policy.revision
            or snapshot.financial_ref != self.financial_ref
            or self.installed_pins() != self.pins
        ):
            raise BindingUnavailable("current_enrollment_or_installed_measurement_changed")
        return snapshot

    def authority(self, operation):
        snapshot = self._resolve()
        # Remaining=0 is allowed ONLY for this issuer-validated, actively held reservation.
        budget = (
            snapshot.remaining_native_operations > 0
            and snapshot.remaining_exposure_cents >= self.record.policy.exposure_per_operation_cents
        )
        if self._lease is not None:
            self._lease.assert_current()
            budget = (
                self._lease.claim.scope == snapshot.scope
                and self._lease.claim.pins == snapshot.pins
            )
        actions = tuple(self.record.policy.allowed_actions)
        return CurrentAuthority(
            self.scope,
            instant(snapshot.expires_at.isoformat()),
            operation,
            False,
            budget,
            True,
            actions,
        )

    def operator(self, ref):
        if ref != self.financial_ref:
            raise BindingUnavailable("financial_reference_changed")
        self._resolve()
        financial = (
            self._lease.state.financial if self._lease is not None else self.client.financial(ref)
        )
        return OperatorStatus(
            Financial.model_validate(financial.model_dump(mode="json")), "operator", False
        )

    def current_checks(self, operation, expected):
        self._resolve()
        value = (
            self._lease.state.checks
            if self._lease is not None
            else self.owner_producer(operation, expected)
        )
        if (
            type(value) is not OwnerCheckExecution
            or value.installed_pins != self.pins
            or value.current.expected != expected
        ):
            raise BindingUnavailable("registered_owner_check_execution_required")
        self._execution = value
        return value.current

    def verifier(self):
        def authenticate(raw, scope):
            if self._execution is None or raw != self._execution.evidence or scope != self.scope:
                raise BindingUnavailable("approved_owner_publisher_execution_required")
            return hashlib.sha256(raw).hexdigest()

        return RuntimeAdmissionVerifier(
            policy=self.policy,
            scope=self.scope,
            pins=self.pins,
            model=self.record.policy.model,
            ledger=self.ledger,
            authorize=self.authority,
            authenticate_operator=self.operator,
            authenticate_evidence=authenticate,
            current_local_checks=self.current_checks,
            terminal_port=self.terminal_port,
            authority_action_namespace="runtime",
            clock=self.clock,
        )

    def verify(self, operation, expected):
        # Independently execute/read registered producer BEFORE taking authenticated bytes.
        self.current_checks(operation, expected)
        return self.verifier().verify(
            self._execution.evidence, operation=operation, expected=expected
        )

    def session_observer(self, *, binding, prepare_request, request_key):
        """Factory-compatible actual enrolled observer, no automatic research start."""
        from .harness_observer import EvidenceBackedHarnessObserver

        if (
            type(binding) is not HarnessBinding
            or binding.digest != self.pins.binding
            or not callable(prepare_request)
            or not callable(request_key)
        ):
            raise BindingUnavailable("approved_preparation_owner_ports_required")

        def read_preparation():
            payload = prepare_request()
            if (
                type(payload) is not AllocateRuntimeOperation
                or payload.action != "runtime:prepare"
                or payload.phase != "session-preparation"
                or payload.pins != self.enrollment.pins
            ):
                raise BindingUnavailable("preparation_is_not_native_authority")
            operation = self.client.allocate(payload, request_key(payload))
            if (
                operation.actor_credential_id != self.enrollment.publisher_credential_id
                or operation.binding.scope != self.enrollment.scope
            ):
                raise BindingUnavailable("preparation_enrolled_actor_changed")
            expected = TerminalExpectation(
                payload.phase,
                payload.request_ref,
                payload.final_fingerprint,
                payload.order_revision,
                payload.sequence,
                operation.nonce,
                operation.ticket,
            )
            self.current_checks("session", expected)
            return self._execution.evidence, expected

        return EvidenceBackedHarnessObserver(
            binding=binding, verifier=self.verifier(), read_preparation=read_preparation
        )


class _HeldInvocationLease:
    def __init__(self, issuer, claim):
        self.issuer, self.claim, self.active = issuer, claim, True

    def assert_current(self):
        if not self.active or not self.issuer.db.in_transaction:
            raise BindingUnavailable("issuer_invocation_lease_not_held")
        current = self.issuer.validate_owned_claim(self.claim)
        if (
            type(current) is not IssuerInvocationState
            or current.claim.model_dump(exclude={"delivery"})
            != self.claim.model_dump(exclude={"delivery"})
            or current.claim.state != "claimed"
            or current.snapshot.scope != self.claim.scope
            or current.snapshot.pins != self.claim.pins
            or current.snapshot.financial_ref != self.claim.financial_ref
            or current.financial.attestation_ref != self.claim.financial_ref
            or type(current.checks) is not OwnerCheckExecution
            or current.checks.local_check_ref != self.claim.local_check_ref
            or current.checks.quota_check_ref != self.claim.quota_check_ref
            or current.checks.installed_pins.model_dump() != self.claim.pins.model_dump()
            or self.claim.expires_at <= self.issuer.clock()
        ):
            raise BindingUnavailable("owned_reservation_or_current_authority_changed")
        policy = current.policy
        finance = current.financial
        checks = current.checks.current
        now = self.issuer.clock()
        expected = checks.expected
        if (
            policy.policy_ref != self.claim.scope.policy_ref
            or policy.revision != finance.policy_revision
            or policy.admission_policy_digest != self.claim.scope.policy_digest
            or policy.operator_issuer != finance.issuer
            or finance.opaque_owner_ref != self.claim.scope.opaque_owner_ref
            or finance.project != self.claim.scope.project
            or finance.destination != self.claim.scope.destination
            or not finance.observed_at <= finance.asserted_at <= now < finance.valid_until
            or now >= min(current.snapshot.expires_at, policy.policy.expires_at)
            or (now - finance.observed_at).total_seconds() > policy.policy.max_financial_age_seconds
            or expected.phase not in {"terminal-http", "terminal-ws"}
            or expected.nonce != self.claim.nonce
            or expected.ticket != self.claim.ticket
            or expected.request_ref != self.claim.request_ref
            or expected.fingerprint != self.claim.final_fingerprint
            or expected.order_revision != self.claim.order_revision
            or expected.sequence != self.claim.sequence
            or not checks.executed_at <= now
            or checks.local.model != policy.policy.model
        ):
            raise BindingUnavailable("live_operator_policy_native_scope_changed")
        quota = checks.quota
        snapshot, checked, until = map(
            instant, (quota.snapshot_checked_at, quota.checked_at, quota.valid_until)
        )
        if (
            not snapshot <= checked <= checks.executed_at <= now < until
            or (until - snapshot).total_seconds() > 30
        ):
            raise BindingUnavailable("live_native_quota_changed_or_stale")
        self.state = current


class SQLiteInvocationGate:
    """Concrete co-located SQLite writer lease, NOT a remote HTTP snapshot.

    Issuer registers its own connection+current Store validator. Validator must
    reauthenticate publisher/subject/operator and read OWN reservation, checks,
    revisions/expiry/pins; never require remaining>0 or accept caller flags.
    All revocation writers must use this SAME database locking boundary. Claim
    commits first; this second bounded transaction fences the immediate effect.
    No DB/network atomicity or provider exactly-once claim is made.
    """

    def __init__(self, *, connection, validate_owned_claim, clock=lambda: datetime.now(UTC)):
        if type(connection) is not sqlite3.Connection or not callable(validate_owned_claim):
            raise BindingUnavailable("co_located_issuer_gate_required")
        self.db, self.validate_owned_claim, self.clock = connection, validate_owned_claim, clock

    @contextmanager
    def hold(self, claim):
        if (
            type(claim) is not RuntimeClaimReceipt
            or claim.delivery != "first-claim"
            or claim.state != "claimed"
            or self.db.in_transaction
        ):
            raise BindingUnavailable("fresh_owned_claim_and_exclusive_issuer_lease_required")
        lease = _HeldInvocationLease(self, claim)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            lease.assert_current()
            yield lease
            lease.assert_current()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise BindingUnavailable("native_invocation_denied_or_uncertain") from None
        finally:
            lease.active = False

    def run(self, claim, continuation):
        with self.hold(claim) as lease:
            return continuation(lease)


class StoreInvocationGate:
    """Production gate using the canonical issuer's durable one-shot entry.

    Construct only at the independently registered owner boundary. The publisher
    actor comes from authenticated Store resolution, never caller JSON. The owner
    live callback rechecks private resource/wire state inside the held writer fence.
    No separate SQLite connection, HTTP permit or cached authority is substituted.
    """

    def __init__(self, *, store, publisher_actor, claim_request, audience, owner_live_check):
        from tessera_control.runtime_bindings import RuntimeBindingStore

        if (
            type(store) is not RuntimeBindingStore
            or type(claim_request) is not ClaimRuntimeOperation
            or audience not in {"control", "worker"}
            or not callable(owner_live_check)
        ):
            raise BindingUnavailable("registered_canonical_store_and_owner_boundary_required")
        self.store, self.actor, self.payload = store, publisher_actor, claim_request
        self.audience, self.owner_live_check = audience, owner_live_check
        self.clock = lambda: datetime.now(UTC)

    def run(self, claim, continuation):
        if (
            type(claim) is not RuntimeClaimReceipt
            or claim.delivery != "first-claim"
            or claim.state != "claimed"
            or claim.nonce != self.payload.nonce
            or claim.ticket != self.payload.ticket
            or claim.pins != self.payload.pins
            or claim.request_ref != self.payload.request_ref
            or claim.final_fingerprint != self.payload.final_fingerprint
        ):
            raise BindingUnavailable("canonical_claim_and_native_request_required")

        gate = self

        class HeldOwnerBoundary:
            active = False

            def assert_current(self):
                if not self.active or claim.expires_at <= gate.clock():
                    raise BindingUnavailable("canonical_native_lease_expired")
                # This must raise on denial. Boolean/JSON returns cannot admit.
                if gate.owner_live_check(claim) is not None:
                    raise BindingUnavailable("registered_owner_live_check_must_enforce")

        lease = HeldOwnerBoundary()

        def invoke_under_canonical_writer_fence():
            lease.active = True
            try:
                lease.assert_current()
                result = continuation(lease)
                lease.assert_current()
                return result
            finally:
                lease.active = False

        try:
            return self.store.guarded_native_continuation(
                self.actor,
                claim.operation_ref,
                claim.claim_ref,
                self.payload.model_dump(mode="json"),
                invoke_under_canonical_writer_fence,
                audience=self.audience,
            )
        except Exception:
            raise BindingUnavailable("canonical_native_entry_denied_or_uncertain") from None


class NativeInvocationPipe:
    """Actual bounded process IPC; only metadata, never raw wire/auth/prompt.

    Approved native issuer executable owns branded primitive+wire references.
    A caller's state JSON cannot acquire the inherited private pipe. No server,
    endpoint, shell interpolation, token argument, retry or replaced process.
    """

    def __init__(self, *, approved_command, gate, approved_references=None):
        if (
            type(gate) not in {SQLiteInvocationGate, StoreInvocationGate}
            or type(approved_command) is not tuple
            or not approved_command
            or not all(type(part) is str for part in approved_command)
        ):
            raise BindingUnavailable("approved_native_process_and_issuer_gate_required")
        self.command, self.gate = approved_command, gate
        self.environment = {"PATH": "/usr/bin:/bin"}
        if approved_references is not None and type(approved_references) is not dict:
            raise BindingUnavailable("native_environment_requires_approved_path_references")
        # References only: never inherit the coding process environment/credentials.
        for key, value in (approved_references or {}).items():
            if (
                key
                not in {
                    "HOME",
                    "PYTHONPATH",
                    "TESSERA_RUNTIME_RESOURCE_FILE",
                    "TESSERA_PUBLISHER_CREDENTIAL_FILE",
                }
                or type(value) is not str
                or not value.startswith("/")
                or any(c in value for c in "\r\n\x00")
            ):
                raise BindingUnavailable("native_environment_requires_approved_path_references")
            self.environment[key] = value

    def invoke(self, claim):
        deadline = min(30.0, (claim.expires_at - self.gate.clock()).total_seconds())
        if deadline <= 0:
            raise BindingUnavailable("native_invocation_deadline_elapsed")
        process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.environment,
        )
        start = time.monotonic()

        def exchange(lease):
            frame = self._read(process, start + deadline)
            if frame != {
                "event": "ready",
                "operation_ref": claim.operation_ref,
                "request_ref": claim.request_ref,
                "final_fingerprint": claim.final_fingerprint,
            }:
                raise BindingUnavailable("native_immutable_wire_or_operation_changed")
            lease.assert_current()  # same issuer lock immediately before native continuation
            process.stdin.write(
                (
                    json.dumps({"event": "invoke", "operation_ref": claim.operation_ref}) + "\n"
                ).encode()
            )
            process.stdin.flush()
            result = self._read(process, start + deadline)
            if result != {"event": "native-returned", "operation_ref": claim.operation_ref}:
                raise BindingUnavailable("native_reply_uncertain")
            lease.assert_current()

        def continuation(lease):
            try:
                exchange(lease)
                process.wait(timeout=max(0.01, start + deadline - time.monotonic()))
                if process.returncode != 0:
                    raise BindingUnavailable("native_process_uncertain")
                lease.assert_current()
            finally:
                # No admitted child may outlive the canonical writer fence, even
                # after malformed/lost replies or a native-returned/nonzero exit.
                if process.poll() is None:
                    process.kill()
                process.wait()

        try:
            self.gate.run(claim, continuation)
            return "native-returned"
        except Exception:
            raise BindingUnavailable("native_reply_or_lease_uncertain") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()

    @staticmethod
    def _read(process, deadline):
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while len(data) < 8192:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise BindingUnavailable("native_ipc_deadline_or_lost_reply")
                byte = os.read(process.stdout.fileno(), 1)
                if not byte:
                    raise BindingUnavailable("native_ipc_closed_or_uncertain")
                if byte == b"\n":
                    try:
                        return json.loads(data, object_pairs_hook=closed_object)
                    except (ValueError, TypeError):
                        raise BindingUnavailable("closed_native_ipc_frame_required") from None
                data.extend(byte)
        raise BindingUnavailable("native_ipc_bounds")


class EnrolledNativeBridge:
    """Real ordered client/verifier/claim/IPC/settlement orchestration; no retries."""

    def __init__(self, *, adapter, pipe_factory):
        if type(adapter) is not EnrolledAdmissionAdapter or not callable(pipe_factory):
            raise BindingUnavailable("approved_enrolled_bridge_required")
        self.adapter, self.pipe_factory = adapter, pipe_factory

    def invoke(self, operation, *, claim_key, settle_key):
        if (
            type(operation) is not RuntimeOperation
            or operation.state != "allocated"
            or operation.expected.action != "runtime:invoke"
            or operation.binding.scope != self.adapter.enrollment.scope
            or operation.binding.pins != self.adapter.enrollment.pins
            or operation.actor_credential_id != self.adapter.enrollment.publisher_credential_id
        ):
            raise BindingUnavailable("exact_allocated_native_operation_required")
        expected = TerminalExpectation(
            operation.expected.phase,
            operation.expected.request_ref,
            operation.expected.final_fingerprint,
            operation.expected.order_revision,
            operation.expected.sequence,
            operation.nonce,
            operation.ticket,
        )
        self.adapter.verify("dispatch", expected)  # NEVER claim/execute before actual verifier.
        execution = self.adapter._execution
        payload = ClaimRuntimeOperation(
            expected_operation_revision=operation.revision,
            nonce=operation.nonce,
            ticket=operation.ticket,
            expected_grant_revision=self.adapter.scope.grant_revision,
            expected_grant_epoch=self.adapter.scope.grant_epoch,
            expected_enrollment_revision=self.adapter.enrollment.revision,
            financial_ref=self.adapter.financial_ref,
            local_check_ref=execution.local_check_ref,
            quota_check_ref=execution.quota_check_ref,
            pins=operation.binding.pins,
            request_ref=expected.request_ref,
            final_fingerprint=expected.fingerprint,
            order_revision=expected.order_revision,
            sequence=expected.sequence,
        )
        # Any lost/readback claim reply stops here; no child/native effect is started.
        claim = self.adapter.client.claim(operation, payload, claim_key)
        try:
            pipe = self.pipe_factory(claim)
            if type(pipe) is not NativeInvocationPipe:
                raise BindingUnavailable("installed_co_located_native_pipe_required")
            disposition = pipe.invoke(claim)
        except Exception:
            # Conservative disposition; lost settlement cannot authorize a retry/refund.
            try:
                self.adapter.client.settle(claim, "uncertain", settle_key)
            except BindingUnavailable:
                pass
            raise BindingUnavailable("claimed_native_operation_uncertain") from None
        return self.adapter.client.settle(claim, disposition, settle_key)
