"""Offline validation of sandbox activation *candidates*, not execution authority.

Authenticated probe transcripts are necessary but not sufficient: the deployment
must also supply an enforcing runner and a live independent inspector. Nothing in
this module enables GatedRealCapabilityEffects or issues isolated packages.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from tessera_control.capability_models import (
    Digest,
    Identifier,
    Strict,
    artifact_url,
    public_address,
)
from tessera_control.capability_package import encode
from tessera_control.store import canonical_digest

from .capability_effects import BackendBlocked
from .capability_journal import CapabilityJournal

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
Nonce = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Principal = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$")]


class Frozen(Strict):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PinnedDestination(Frozen):
    url: str = Field(max_length=2048)
    addresses: tuple[str, ...] = Field(min_length=1, max_length=8)
    certificate_sha256: Digest
    issuer_sha256: Digest
    dns_observed_at: str = Field(max_length=40)

    @model_validator(mode="after")
    def safe(self):
        artifact_url(self.url)
        if len(set(self.addresses)) != len(self.addresses):
            raise ValueError("duplicate destination address")
        for value in self.addresses:
            address = ipaddress.ip_address(value)
            if str(address) != value or not public_address(address):
                raise ValueError("noncanonical or nonpublic destination address")
        stamp(self.dns_observed_at)
        return self


class SandboxQuota(Frozen):
    # ResourceQuota admission limits alone are never accepted as a hard byte cap.
    mechanism: Literal["filesystem-project-quota"]
    output_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    output_inodes: int = Field(ge=1, le=4096)
    ephemeral_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    memory_bytes: int = Field(ge=16 * 1024 * 1024, le=512 * 1024 * 1024)
    cpu_millicores: int = Field(ge=1, le=1000)


class SandboxWatchdog(Frozen):
    endpoint: str = Field(max_length=2048)
    identity: Principal
    certificate_sha256: Digest
    heartbeat_seconds: int = Field(ge=1, le=2)
    lease_seconds: int = Field(ge=2, le=5)
    kill_grace_seconds: int = Field(ge=0, le=2)

    @model_validator(mode="after")
    def safe(self):
        # An endpoint is a target identity, not permission to resolve/connect to it.
        from tessera_control.capability_models import observed_url

        observed_url(self.endpoint)
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat must precede the lease deadline")
        return self


class ActivationManifest(Frozen):
    version: Literal[1] = 1
    enabled: Literal[True]
    target: Literal["docker-desktop-kubernetes"]
    org_id: Identifier
    project_id: Identifier
    worker_id: Identifier
    operation_id: Identifier
    package_digest: Digest
    environment_digest: Digest
    cluster_digest: Digest
    namespace: Name
    namespace_uid: Identifier
    control_namespace: Name
    service_account: Name
    service_account_uid: Identifier
    dispatcher_identity: Principal
    image: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$")]
    # One reviewed protocol entrypoint; never an arbitrary command or shell.
    command: tuple[Literal["/usr/local/bin/tessera-capability-sandbox-v1"]]
    pvc: Name
    pvc_uid: Identifier
    namespace_spec_digest: Digest
    rbac_spec_digest: Digest
    admission_spec_digest: Digest
    network_policy_spec_digest: Digest
    resource_quota_spec_digest: Digest
    pvc_spec_digest: Digest
    destination: PinnedDestination
    quota: SandboxQuota
    watchdog: SandboxWatchdog
    timeout_seconds: int = Field(ge=1, le=120)
    issued_at: str = Field(max_length=40)
    expires_at: str = Field(max_length=40)
    nonce: Nonce

    @field_validator("enabled", mode="before")
    @classmethod
    def explicit_boolean(cls, value):
        if value is not True:
            raise ValueError("explicit boolean opt-in required")
        return value

    @property
    def labels(self):
        return {
            "tessera.io/capability-scope": canonical_digest([self.org_id, self.project_id])[:32],
            "tessera.io/isolation": "capability-sandbox-v1",
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "v1.30",
        }

    @model_validator(mode="after")
    def safe(self):
        if self.namespace == self.control_namespace or self.namespace in {
            "default",
            "kube-system",
            "kube-public",
            "kube-node-lease",
        }:
            raise ValueError("dedicated execution namespace required")
        workload = f"system:serviceaccount:{self.namespace}:{self.service_account}"
        if workload in {self.dispatcher_identity, self.watchdog.identity}:
            raise ValueError("workload cannot carry supervisor or watchdog identity")
        if self.dispatcher_identity == self.watchdog.identity:
            raise ValueError("independent watchdog identity required")
        if not 0 < (stamp(self.expires_at) - stamp(self.issued_at)).total_seconds() <= 120:
            raise ValueError("activation validity must be bounded")
        return self


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    return result


def canonical(value):
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


@dataclass(frozen=True, repr=False)
class SignedDocument:
    """Domain-separated signatures. Keys are explicit runtime references, never files/env.

    HMAC authenticates a trusted observer, NOT independent execution of a probe.
    Deployment must keep observer keys away from workloads and agent inputs.
    """

    payload: bytes
    digest: str
    signature: str


def verify_document(document, model, *, key, domain):
    try:
        if type(key) is not bytes or len(key) < 32:
            raise ValueError
        if type(document.payload) is not bytes or len(document.payload) > 128 * 1024:
            raise ValueError
        expected = hmac.new(key, domain + b"\0" + document.payload, "sha256").hexdigest()
        if not hmac.compare_digest(expected, document.signature):
            raise ValueError
        if hashlib.sha256(document.payload).hexdigest() != document.digest:
            raise ValueError
        result = model.model_validate_json(document.payload)
        if canonical(result) != document.payload:
            raise ValueError
        return result
    except Exception:
        # Do not propagate validation inputs, tokens, upstream text or signatures.
        raise BackendBlocked("invalid signed activation document") from None


# Every case is required exactly once. A timeout or a generic 403 is NOT a pass.
# Probe responses are signed by the deployment's independent observer; the raw
# bounded request/response artifacts must be retained separately by that observer.
REQUIRED_PROBES = {
    "namespace": "identity-matched",
    "image-command": "identity-matched",
    "service-account": "identity-matched",
    "rbac-secrets": "rbac-denied",
    "rbac-token-issuance": "rbac-denied",
    "rbac-exec": "rbac-denied",
    "rbac-control-pvc": "rbac-denied",
    "rbac-escalation": "rbac-denied",
    "rbac-cross-namespace": "rbac-denied",
    "admission-positive": "admission-allowed",
    "admission-hostpath": "policy-denied",
    "admission-privileged": "policy-denied",
    "admission-hostnetwork": "policy-denied",
    "admission-hostpid-ipc": "policy-denied",
    "admission-extra-containers": "policy-denied",
    "admission-image": "policy-denied",
    "admission-command": "policy-denied",
    "admission-control-pvc": "policy-denied",
    "admission-secret": "policy-denied",
    "admission-ambient-token": "policy-denied",
    "admission-policy-mode": "fail-deny-readback",
    "output-pvc": "identity-matched",
    "resource-quota": "quota-admission-denied",
    "output-byte-exhaustion": "edquot",
    "output-inode-exhaustion": "edquot",
    "ephemeral-exhaustion": "edquot",
    "network-policy": "identity-matched",
    "network-allowed": "challenge-received",
    "network-denied-public": "policy-drop-observed",
    "network-denied-private": "policy-drop-observed",
    "network-denied-metadata": "policy-drop-observed",
    "network-denied-control": "policy-drop-observed",
    "network-denied-dns-bypass": "policy-drop-observed",
    "network-denied-proxy": "policy-drop-observed",
    "dns-pins": "identity-matched",
    "tls-san-issuer": "verified-chain-and-pins",
    "tls-wrong-ip": "client-denied",
    "tls-wrong-certificate": "client-denied",
    "redirect": "client-denied",
    "ambient-credentials": "absent",
    "readonly-nonroot": "enforced",
    "archive-escape": "installer-denied",
    "lockfile-closure": "installer-denied",
    "watchdog-health": "challenge-received",
    "watchdog-broker-loss": "termination-observed",
    "watchdog-revoke": "termination-observed",
}


class Probe(Frozen):
    control: Annotated[str, Field(max_length=64)]
    outcome: Annotated[str, Field(max_length=64)]
    observed_at: str = Field(max_length=40)
    subject_digest: Digest
    request_digest: Digest
    response_digest: Digest
    # Independent witness retained evidence, not a workload's "success" boolean.
    transcript_digest: Digest
    challenge: Nonce


class ProbeEvidence(Frozen):
    version: Literal[1] = 1
    manifest_digest: Digest
    observer_id: Identifier
    nonce: Nonce
    probes: tuple[Probe, ...] = Field(min_length=1, max_length=64)


class ActivationJournal(CapabilityJournal):
    """Private candidate-attempt extension; never removes failed/reused evidence.

    Uses the broker's existing owner lock/fsync/SQLite custody. Base journal schema
    remains 1; these additive tables are used only by explicitly composed tooling.
    """

    def __init__(self, root):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activation_candidates (
                    nonce TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                    manifest_digest TEXT UNIQUE NOT NULL, evidence_digest TEXT UNIQUE NOT NULL);
                CREATE TRIGGER IF NOT EXISTS activation_candidates_no_update
                    BEFORE UPDATE ON activation_candidates
                    BEGIN SELECT RAISE(ABORT,'immutable activation attempt'); END;
                CREATE TRIGGER IF NOT EXISTS activation_candidates_no_delete
                    BEFORE DELETE ON activation_candidates
                    BEGIN SELECT RAISE(ABORT,'retained activation attempt'); END;
            """)

    def consume(self, manifest, manifest_digest, evidence_digest):
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO activation_candidates VALUES (?,?,?,?)",
                    (manifest.nonce, manifest.operation_id, manifest_digest, evidence_digest),
                )
        except Exception:
            raise BackendBlocked(
                "activation attempt already consumed or custody unavailable"
            ) from None


class CandidateVerifier:
    """Authenticate and bind a full probe suite, but do not grant effect authority.

    Deliberately returns identifiers rather than a runnable capability. Attestation
    authenticity cannot establish observer independence, freshness of the current
    cluster or continuous enforcement. Those are outstanding deployment gates.
    """

    def __init__(self, *, manifest_key, observer_key, observer_id, journal, now=None):
        self._manifest_key, self._observer_key = manifest_key, observer_key
        self.observer_id, self.journal = observer_id, journal
        if (
            type(manifest_key) is not bytes
            or type(observer_key) is not bytes
            or manifest_key == observer_key
            or min(len(manifest_key), len(observer_key)) < 32
        ):
            raise ValueError("independent explicit observer and approval keys required")
        self.now = now or (lambda: datetime.now(UTC))

    def verify(self, document, evidence, package):
        manifest = verify_document(
            document, ActivationManifest, key=self._manifest_key, domain=b"tessera-activation-v1"
        )
        # Burn valid manifest's nonce BEFORE any probe validation, including failed
        # signatures/stale evidence. A failed attestation cannot be repaired in place.
        if type(evidence.payload) is not bytes or len(evidence.payload) > 128 * 1024:
            self.journal.consume(manifest, document.digest, "invalid-evidence")
            raise BackendBlocked("invalid signed activation document")
        evidence_digest = hashlib.sha256(evidence.payload).hexdigest()
        self.journal.consume(manifest, document.digest, evidence_digest)
        try:
            body = package.decode()
            now = self.now()
            if body.mode != "isolated":
                raise ValueError
            if (
                manifest.org_id,
                manifest.project_id,
                manifest.worker_id,
                manifest.operation_id,
                manifest.package_digest,
                manifest.environment_digest,
            ) != (
                body.org_id,
                body.project_id,
                body.worker_id,
                body.operation_id,
                hashlib.sha256(encode(body)).hexdigest(),
                body.declaration.broker_basis.environment_digest,
            ):
                raise ValueError
            action = body.binding.action
            if (
                manifest.image.split("@", 1)[1] != body.binding.image_digest
                or manifest.destination.url != action.url
                or manifest.quota.output_bytes < action.max_bytes
                or manifest.timeout_seconds > action.max_seconds
                or not stamp(manifest.issued_at) <= now < stamp(manifest.expires_at)
                or stamp(manifest.expires_at) > stamp(body.expires_at)
                or not 0 <= (now - stamp(manifest.destination.dns_observed_at)).total_seconds() <= 5
            ):
                raise ValueError
            probes = verify_document(
                evidence, ProbeEvidence, key=self._observer_key, domain=b"tessera-probes-v1"
            )
            if (probes.manifest_digest, probes.nonce, probes.observer_id) != (
                document.digest,
                manifest.nonce,
                self.observer_id,
            ):
                raise ValueError
            if len(probes.probes) != len(REQUIRED_PROBES) or {
                p.control for p in probes.probes
            } != set(REQUIRED_PROBES):
                raise ValueError
            for probe in probes.probes:
                if (
                    probe.outcome != REQUIRED_PROBES[probe.control]
                    or probe.challenge != manifest.nonce
                    or probe.subject_digest != document.digest
                    or not stamp(manifest.issued_at) <= stamp(probe.observed_at) <= now
                    or (now - stamp(probe.observed_at)).total_seconds() > 5
                    or "0" * 64
                    in (probe.request_digest, probe.response_digest, probe.transcript_digest)
                ):
                    raise ValueError
            # No activation token. The package signature/live authority must still
            # be checked by control, not by decode() or this offline verifier.
            return {"manifest_digest": document.digest, "evidence_digest": evidence.digest}
        except Exception:
            raise BackendBlocked("activation candidate failed verification") from None


def destination_host(destination):
    parsed = urlsplit(destination.url)
    return parsed.hostname, parsed.port or 443, parsed.path or "/"


class CollectorActivationGate:
    """Current package authority plus fresh, one-time independent collection.

    Returns identifiers for the *next exact* manifest, never a transferable grant
    or a real-effects enable flag. CandidateVerifier remains offline diagnostics.
    The runner still owns global claim, watchdog arming and use-time revocation.
    """

    def __init__(self, *, collector, verifier, journal):
        from .capability_collector import KubernetesEvidenceCollector

        if type(collector) is not KubernetesEvidenceCollector:
            raise BackendBlocked("authenticated collector required")
        self.collector, self.verifier, self.journal = collector, verifier, journal

    def authorize(self, plan, verified):
        from .capability_inspection import RawEvidenceInspector

        manifest_digest = hashlib.sha256(canonical(plan.activation)).hexdigest()
        # Burn before authority/evidence checks, including revoked authority.
        self.journal.consume(plan.activation, manifest_digest, plan.digest)
        plan.validate_package(verified, self.verifier)
        digest = RawEvidenceInspector(client=self.collector, now=self.verifier.now).inspect(plan)
        plan.validate_package(verified, self.verifier)
        if not self.collector.allow_fake_witnesses:
            pins = [channel.pin for channel in self.collector.witness.channels.values()]
            if (
                len(pins) != 4
                or None in {pin.target_digest for pin in pins}
                or len({pin.target_digest for pin in pins}) != 1
            ):
                # Legacy authenticated protocol reports remain diagnostics, not a
                # fallback around target-specific native evidence/defaulting gates.
                raise BackendBlocked("exact independently witnessed target profile required")
        return {"manifest_digest": manifest_digest, "evidence_digest": digest}
