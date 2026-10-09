"""Synthetic candidate attestations only; no independent/live enforcement claims."""

import hashlib
import hmac
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_capabilities import declaration

from tessera_control.capability_package import BrokerPackage, SignedCapabilityPackage, encode
from tessera_control.store import canonical_digest
from tessera_worker.capability_activation import (
    REQUIRED_PROBES,
    ActivationJournal,
    ActivationManifest,
    CandidateVerifier,
    ProbeEvidence,
    SignedDocument,
    canonical,
    verify_document,
)
from tessera_worker.capability_effects import (
    BackendBlocked,
    GatedRealCapabilityEffects,
    RealBackendConfig,
)

APPROVAL_KEY = b"synthetic-approval-key-not-secret-0000"
OBSERVER_KEY = b"synthetic-observer-key-not-secret-0000"
HEARTBEAT_KEY = b"synthetic-heartbeat-key-not-secret-000"
NOW = datetime(2026, 9, 22, tzinfo=UTC)


def signed(model, key=APPROVAL_KEY, domain=b"tessera-activation-v1"):
    payload = canonical(model)
    return signed_bytes(payload, key, domain)


def signed_bytes(payload, key, domain):
    return SignedDocument(
        payload,
        hashlib.sha256(payload).hexdigest(),
        hmac.new(key, domain + b"\0" + payload, "sha256").hexdigest(),
    )


@pytest.fixture
def candidate(tmp_path):
    declared = declaration("data")
    declared["broker_basis"] = dict(
        protocol_digest="1" * 64,
        data_digest="2" * 64,
        code_digest="3" * 64,
        environment_digest="4" * 64,
    )
    declared["actions"][0].update(expected_size=7, content_type="application/octet-stream")
    body = BrokerPackage(
        mode="isolated",
        org_id="org",
        project_id="project",
        worker_id="worker",
        task_id="task",
        operation_id="operation",
        policy_id="policy",
        grant_id="grant",
        plan_digest="5" * 64,
        declaration_digest=canonical_digest(declared),
        declaration=declared,
        binding=dict(
            execution_lease_id="lease",
            expected_epoch=1,
            environment_id="sandbox-1",
            image_digest=declared["image_digest"],
            action=declared["actions"][0],
        ),
        expires_at=(NOW + timedelta(seconds=10)).isoformat(),
    )
    payload = encode(body)
    # Not a control signature and never authority to run a real effect.
    package = SignedCapabilityPackage(payload, hashlib.sha256(payload).hexdigest(), "0" * 64)
    manifest = ActivationManifest(
        enabled=True,
        target="docker-desktop-kubernetes",
        org_id=body.org_id,
        project_id=body.project_id,
        worker_id=body.worker_id,
        operation_id=body.operation_id,
        package_digest=package.digest,
        environment_digest="4" * 64,
        cluster_digest="6" * 64,
        namespace="tessera-capability-test",
        namespace_uid="ns-uid",
        control_namespace="control",
        service_account="workload",
        service_account_uid="sa-uid",
        dispatcher_identity="system:serviceaccount:supervisor:dispatcher",
        image="fixture.invalid/runner@" + declared["image_digest"],
        command=("/usr/local/bin/tessera-capability-sandbox-v1",),
        pvc="output",
        pvc_uid="pvc-uid",
        namespace_spec_digest="a" * 64,
        rbac_spec_digest="b" * 64,
        admission_spec_digest="c" * 64,
        network_policy_spec_digest="d" * 64,
        resource_quota_spec_digest="e" * 64,
        pvc_spec_digest="f" * 64,
        destination=dict(
            url=declared["actions"][0]["url"],
            addresses=("8.8.8.8",),
            certificate_sha256=hashlib.sha256(b"fixture-der").hexdigest(),
            issuer_sha256=canonical_digest((("organizationName", "Fixture CA"),)),
            dns_observed_at=NOW.isoformat(),
        ),
        quota=dict(
            mechanism="filesystem-project-quota",
            output_bytes=1024,
            output_inodes=64,
            ephemeral_bytes=1024,
            memory_bytes=64 * 1024 * 1024,
            cpu_millicores=500,
        ),
        watchdog=dict(
            endpoint="https://watchdog.example.org/lease",
            identity="external-watchdog",
            certificate_sha256="7" * 64,
            heartbeat_seconds=1,
            lease_seconds=3,
            kill_grace_seconds=1,
        ),
        timeout_seconds=10,
        issued_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=10)).isoformat(),
        nonce="8" * 64,
    )
    document = signed(manifest)
    evidence = ProbeEvidence(
        manifest_digest=document.digest,
        observer_id="independent-observer",
        nonce=manifest.nonce,
        probes=tuple(
            dict(
                control=control,
                outcome=outcome,
                observed_at=NOW.isoformat(),
                subject_digest=document.digest,
                request_digest=canonical_digest([control, "request"]),
                response_digest=canonical_digest([control, "response"]),
                transcript_digest=canonical_digest([control, "retained"]),
                challenge=manifest.nonce,
            )
            for control, outcome in REQUIRED_PROBES.items()
        ),
    )
    journal = ActivationJournal(tmp_path / "activation")
    verifier = CandidateVerifier(
        manifest_key=APPROVAL_KEY,
        observer_key=OBSERVER_KEY,
        observer_id="independent-observer",
        journal=journal,
        now=lambda: NOW,
    )
    yield SimpleNamespace(**locals())
    journal.close()


def verify(h, evidence=None, document=None, package=None):
    return h.verifier.verify(
        document or h.document,
        signed(evidence or h.evidence, OBSERVER_KEY, b"tessera-probes-v1"),
        package or h.package,
    )


def test_candidate_is_not_execution_authority(candidate):
    h = candidate
    result = verify(h)
    assert set(result) == {"manifest_digest", "evidence_digest"}
    backend = GatedRealCapabilityEffects(
        RealBackendConfig(
            enabled=True,
            target="kubernetes",
            sandbox_runner_id="candidate",
            network_client_id="candidate",
            isolation_inspector_id="candidate",
        ),
        isolation_inspector=result,
    )
    with pytest.raises(BackendBlocked):
        backend.preflight(h.body)
    with pytest.raises(BackendBlocked, match="consumed"):
        verify(h)


@pytest.mark.parametrize("control", list(REQUIRED_PROBES))
@pytest.mark.parametrize("fault", ["missing", "failed"])
def test_every_required_negative_and_positive_control_fails_closed(candidate, control, fault):
    h = candidate
    probes = (
        tuple(
            p.model_copy(update={"outcome": "unverified"}) if p.control == control else p
            for p in h.evidence.probes
        )
        if fault == "failed"
        else tuple(p for p in h.evidence.probes if p.control != control)
    )
    with pytest.raises(BackendBlocked):
        verify(h, h.evidence.model_copy(update={"probes": probes}))
    # Failed attestation cannot be replaced by a passing one with the same nonce.
    with pytest.raises(BackendBlocked, match="consumed"):
        verify(h)


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_at", (NOW - timedelta(seconds=6)).isoformat()),
        ("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
        ("challenge", "0" * 64),
        ("subject_digest", "0" * 64),
        ("request_digest", "0" * 64),
        ("transcript_digest", "0" * 64),
        ("outcome", "timeout"),
        ("outcome", "generic-http-403"),
    ],
)
def test_unattributed_stale_future_or_wrong_probe(candidate, field, value):
    h = candidate
    first = h.evidence.probes[0].model_copy(update={field: value})
    with pytest.raises(BackendBlocked):
        verify(h, h.evidence.model_copy(update={"probes": (first,) + h.evidence.probes[1:]}))


@pytest.mark.parametrize(
    "field",
    [
        "org_id",
        "project_id",
        "worker_id",
        "operation_id",
        "package_digest",
        "environment_digest",
        "image",
    ],
)
def test_operation_and_tenant_binding(candidate, field):
    h = candidate
    value = "0" * 64 if field.endswith("digest") else "other"
    if field == "image":
        value = "fixture.invalid/runner@sha256:" + "0" * 64
    document = signed(h.manifest.model_copy(update={field: value}))
    with pytest.raises(BackendBlocked):
        verify(h, document=document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("enabled", 1),
        ("namespace", "control"),
        ("namespace", "default"),
        ("target", "local-shell"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 121),
        ("image", "python:latest"),
        ("command", ["sh", "-c", "true"]),
        ("unknown", "secret-canary-do-not-log"),
    ],
)
def test_strict_config(candidate, field, value):
    data = candidate.manifest.model_dump(mode="json") | {field: value}
    doc = signed_bytes(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
        APPROVAL_KEY,
        b"tessera-activation-v1",
    )
    with pytest.raises(BackendBlocked) as error:
        candidate.verifier.verify(doc, signed(candidate.evidence), candidate.package)
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("quota", "mechanism", "resource-quota-object-present"),
        ("quota", "output_bytes", 0),
        ("quota", "output_inodes", 0),
        ("quota", "ephemeral_bytes", 0),
        ("watchdog", "lease_seconds", 60),
        ("watchdog", "heartbeat_seconds", 3),
        ("watchdog", "endpoint", "http://watchdog.example.org"),
        ("watchdog", "identity", "system:serviceaccount:tessera-capability-test:workload"),
        ("destination", "addresses", ["127.0.0.1"]),
        ("destination", "addresses", ["169.254.169.254"]),
        ("destination", "addresses", ["8.8.8.8", "8.8.8.8"]),
        ("destination", "url", "https://user:password@example.org/x"),
    ],
)
def test_weak_nested_config(candidate, section, field, value):
    data = candidate.manifest.model_dump(mode="json")
    data[section][field] = value
    with pytest.raises(ValidationError):
        ActivationManifest.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("mutation", ["signature", "digest", "noncanonical", "wrong-domain"])
def test_signature_canonical_and_domain_separation(candidate, mutation):
    doc = candidate.document
    if mutation == "signature":
        doc = replace(doc, signature="0" * 64)
    elif mutation == "digest":
        doc = replace(doc, digest="0" * 64)
    elif mutation == "noncanonical":
        doc = signed_bytes(doc.payload + b"\n", APPROVAL_KEY, b"tessera-activation-v1")
    else:
        doc = signed(candidate.manifest, APPROVAL_KEY, b"wrong-domain")
    with pytest.raises(BackendBlocked):
        verify_document(doc, ActivationManifest, key=APPROVAL_KEY, domain=b"tessera-activation-v1")


def test_duplicate_probe_and_observer_substitution(candidate):
    h = candidate
    bad = h.evidence.model_copy(update={"probes": h.evidence.probes[1:] + h.evidence.probes[1:2]})
    with pytest.raises(BackendBlocked):
        verify(h, bad)


def test_failed_signature_burns_nonce_across_restart(candidate):
    h = candidate
    with pytest.raises(BackendBlocked):
        h.verifier.verify(h.document, signed(h.evidence, APPROVAL_KEY), h.package)
    with h.journal.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM activation_candidates")
    root = h.journal.root
    h.journal.close()
    with_journal = ActivationJournal(root)
    try:
        h.verifier.journal = with_journal
        with pytest.raises(BackendBlocked, match="consumed"):
            verify(h)
    finally:
        with_journal.close()


@pytest.mark.parametrize("age", [-1, 6, 11])
def test_stale_expired_or_clock_rollback(candidate, age):
    candidate.verifier.now = lambda: NOW + timedelta(seconds=age)
    with pytest.raises(BackendBlocked):
        verify(candidate)


def test_synthetic_package_never_candidate(candidate):
    body = candidate.body.model_copy(update={"mode": "synthetic"})
    payload = encode(body)
    package = SignedCapabilityPackage(payload, hashlib.sha256(payload).hexdigest(), "0" * 64)
    with pytest.raises(BackendBlocked):
        verify(candidate, package=package)
