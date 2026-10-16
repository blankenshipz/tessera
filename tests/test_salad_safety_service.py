import datetime
import hashlib
import http.client
import json
import sqlite3
import ssl
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_salad_http import selection
from test_salad_workload_jwt import GROUP, MACHINE, ORG, claims, key_document, signed

from tessera_control.models import ExecutionSpec
from tessera_control.remote_execution_models import RemoteProposal
from tessera_control.salad_safety_service import (
    SafetyHTTPSClient,
    SafetyHTTPServer,
    SaladSafetyService,
    alarm_budget,
    encoded,
    serve,
)
from tessera_control.salad_secrets import FakeSecretResolver
from tessera_control.salad_workload_jwt import SaladWorkloadJWT
from tessera_control.store import canonical_digest


def reservation(seconds=30, now=100, argv_binding="exact"):
    value = selection().model_dump(mode="json")
    operation = "a" * 32
    value["resources"]["max_seconds"] = seconds
    value["salad"]["execution"]["argv_binding"] = argv_binding
    value["salad"]["execution"]["argv"] = [
        "v1",
        "public-integers-v1",
        operation,
        "1",
        "localhost",
        "a" * 64,
        str(seconds),
        "1024",
        "1",
        "4",
    ]
    plan = dict(
        budget_cents=10,
        max_duration_seconds=seconds,
        execution_spec=dict(
            argv=["/tessera/bin/run", *value["salad"]["execution"]["argv"]],
            max_output_bytes=1024,
            input_snapshot_digest=value["manifests"]["data"],
            pinned_skill_digests=[],
            output_paths=["output.json"],
            profile="kubernetes-opencode",
            network_enabled=False,
        ),
    )
    if argv_binding == "smoke_operation_v1":
        plan["execution_spec"]["argv"][3:5] = ["operation-id", "operation-epoch"]
    validated_spec = ExecutionSpec.model_validate(plan["execution_spec"])
    plan["execution_spec"] = validated_spec.model_dump(mode="json")
    runtime = dict(
        operation_id=operation,
        epoch=1,
        lease_id="lease",
        org_id="tenant",
        project_id="project",
        task_id="task",
        plan=plan,
        plan_digest=canonical_digest(plan),
        pinned_image=value["manifests"]["image"],
        snapshot_digest=value["manifests"]["data"],
        hard_expires_at=datetime.datetime.fromtimestamp(now + 3600, datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    )
    p = RemoteProposal.model_validate(
        dict(
            operation_id=operation,
            package_digest=canonical_digest(runtime),
            attempt_id="attempt",
            plan_digest=canonical_digest(plan),
            lease_id="lease",
            epoch=1,
            selection=value,
            study_id="study",
            program_review_id="review",
            source_review_id="source",
        )
    )
    proposal = p.model_dump(mode="json")
    package = dict(
        version=1,
        org_id="tenant",
        project_id="project",
        task_id="task",
        proposal=proposal,
        mode="offline",
        capability_expansion=False,
    )
    digest = canonical_digest(package)
    return dict(
        operation="reserve",
        reservation=canonical_digest(["route", digest]),
        route_id="route",
        org_id="tenant",
        project_id="project",
        proposal=proposal,
        routing_package=package,
        runtime_package=runtime,
        deadline=now + seconds,
        route_digest=digest,
    )


class Provider:
    def __init__(self):
        self.state = "stopped"
        self.machine = MACHINE
        self.calls = []
        self.fail = False
        self.version = 1
        self.rate = "0.1"
        self.rate_age = 0

    def factory(self, selection):
        owner = self

        class Transport:
            def gpu_class(self, selection, secret):
                owner.calls.append("gpu_class")
                if owner.fail:
                    raise ValueError("isolated_rate_outage")
                at = datetime.datetime.fromtimestamp(owner.clock() - owner.rate_age, datetime.UTC)
                observation = dict(
                    gpu_class_id=selection.resources.gpu_class,
                    hourly_price_usd=owner.rate,
                    price_basis="published_gpu_hourly_rate_not_actual_cost",
                    selection_digest=canonical_digest(selection.model_dump(mode="json")),
                    observed_at=at.isoformat().replace("+00:00", "Z"),
                )
                return type("Result", (), dict(status="observed", observation=observation))()

            def send(self, action, selection, provider_id, secret):
                owner.calls.append(action)
                if owner.fail:
                    raise RuntimeError("do-not-echo-provider-error")
                instances = (
                    []
                    if owner.state == "stopped"
                    else [
                        dict(
                            instance_id="d" * 32,
                            machine_id=owner.machine,
                            state="running",
                            version=owner.version,
                        )
                    ]
                )
                if action == "stop":
                    return type("Result", (), dict(status="pending", observation={}))()
                observation = dict(
                    provider_id=GROUP,
                    state=owner.state,
                    instance_status_counts={
                        k: int(owner.state == "running" and k == "running_count")
                        for k in (
                            "allocating_count",
                            "creating_count",
                            "running_count",
                            "stopping_count",
                        )
                    },
                    pending_change=False,
                    instances=instances,
                )
                return type("Result", (), dict(status="observed", observation=observation))()

        return Transport()


def make_service(root, clock, key, provider):
    provider.clock = clock
    jwt = SaladWorkloadJWT(clock=clock)
    jwt.load(key_document(key), observed_at=clock())
    resolver = FakeSecretResolver("credential-ref:guardian", now=clock)
    resolver.expires_at = clock() + 3600
    return SaladSafetyService(
        root,
        organization_ids={"acme": ORG},
        resolver=resolver,
        reference="credential-ref:guardian",
        version=1,
        destination="localhost",
        verifier=jwt,
        transport_factory=provider.factory,
        clock=clock,
    )


@pytest.fixture
def service_case(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    service = make_service(tmp_path, lambda: clock[0], key, provider)
    body = reservation()
    service.loop()
    service.admin(body)
    service.admin(
        dict(
            operation="bind", reservation=body["reservation"], provider_id=GROUP, allocation_id=None
        )
    )
    provider.state = "running"
    service.loop()
    request = service._request(body["proposal"]["selection"]["salad"]["execution"]["argv"])
    payload = claims()
    payload["salad_workload_name"] = body["proposal"]["selection"]["salad"]["group"]
    token = signed(key, payload)
    yield service, body, request, token, clock, provider, key, tmp_path
    service.close()


def test_rev002_reservation_requires_stop_credential_recovery_window(tmp_path):
    tmp_path.chmod(0o700)
    now = 100.0
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    service = make_service(tmp_path, lambda: now, key, provider)
    body = reservation()
    service.resolver.expires_at = body["deadline"] + 60 - 1
    service.loop()
    try:
        with pytest.raises(ValueError, match="stop_credential_window_insufficient"):
            service.admin(body)
        assert not provider.calls
        assert service.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    finally:
        service.close()


@pytest.mark.parametrize("delta", [-1, 0, 0.001])
def test_rev002_exact_deadline_margin_boundary(tmp_path, delta):
    tmp_path.chmod(0o700)
    provider = Provider()
    service = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    body = reservation()
    service.resolver.expires_at = body["deadline"] + service.STOP_RECOVERY_MARGIN_SECONDS + delta
    service.loop()
    try:
        if delta <= 0:
            with pytest.raises(ValueError, match="stop_credential_window_insufficient"):
                service.admin(body)
            assert not provider.calls
        else:
            assert service.admin(body)["state"] == "ready"
    finally:
        service.close()


def test_rev002_ready_rechecks_coverage_but_stop_preserves_remaining_authority(service_case):
    service, body, _, _, clock, provider, _, _ = service_case
    service.resolver.expires_at = body["deadline"] + 1
    provider.calls.clear()
    with pytest.raises(ValueError, match="stop_credential_window_insufficient"):
        service.admin({"operation": "ready", "reservation": body["reservation"]})
    assert not provider.calls
    # Shortening coverage blocks new work but must not block still-valid stop-only use.
    assignment = service._assignment(service._row(body["reservation"]))
    assert service._stop(assignment) == "pending"
    service.resolver.revoked = True
    with pytest.raises(ValueError, match="credential_unavailable"):
        service._stop(assignment)


def test_rev001_guardian_stop_uses_same_uuid_despite_replica_drift(service_case):
    from uuid import UUID

    from test_salad_http import Factory, group

    from tessera_control.salad_http import SaladHTTPTransport

    service, body, _, _, _, _, _, _ = service_case
    data = group()
    data.update(id=str(UUID(GROUP)), replicas=2, restart_policy="always", priority="low")
    factory = Factory((200, data), (202, b""))
    service.transport_factory = lambda selection: SaladHTTPTransport(
        selection, connection_factory=factory
    )
    assignment = service._assignment(service._row(body["reservation"]))
    assert service._stop(assignment) == "pending"
    assert [call[0] for call in factory.calls] == ["GET", "POST"]
    data["id"] = str(UUID("d" * 32))
    factory.responses = [(200, data)]
    with pytest.raises(ValueError, match="provider_unknown"):
        service._stop(assignment)
    assert [call[0] for call in factory.calls] == ["GET", "POST", "GET"]


def test_real_signature_begin_output_stop_and_reconnect(service_case):
    s, b, request, token, clock, provider, _, _ = service_case
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    with pytest.raises(ValueError, match="consumed"):
        s.workload("/v1/workload/begin-once", dict(binding=request), token)
    output = {"schema": "isolated-output", "result": 123}
    digest = hashlib.sha256(encoded(output)).hexdigest()
    assert s.workload(
        "/v1/workload/output", dict(binding=request, output=output, sha256=digest), token
    )["retained"]
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "stopped" and status["quarantine"]
    imported = s.admin(dict(operation="import", reservation=b["reservation"]))
    assert imported["output"] == output
    assert imported["manifest"]["complete"]
    assert all(
        action in {"gpu_class", "read_group", "read_instance", "stop"} for action in provider.calls
    )


def test_running_status_is_read_only_retention_then_cancel_and_import(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    before = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert before["retained_output"]["retained"] is False and before["manifest"] is None
    assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 0
    output = {"quarantined_result": 123}
    digest = hashlib.sha256(encoded(output)).hexdigest()
    s.workload("/v1/workload/output", dict(binding=request, output=output, sha256=digest), token)
    after = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert after["status"] == "armed" and after["manifest"] is None
    assert after["retained_output"]["retained"] is True
    assert after["retained_output"]["sha256"] == digest
    assert after["retained_output"]["bytes"] == len(encoded(output))
    assert "quarantined_result" not in json.dumps(after)
    assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 0
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    assert s.admin(dict(operation="status", reservation=b["reservation"]))["status"] == "stopped"
    assert s.admin(dict(operation="import", reservation=b["reservation"]))["output"] == output


def test_import_requires_confirmed_stop_not_cancel_or_unknown(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    output = {"quarantined_result": 123}
    s.workload(
        "/v1/workload/output",
        dict(binding=request, output=output, sha256=hashlib.sha256(encoded(output)).hexdigest()),
        token,
    )
    with pytest.raises(ValueError, match="writers_not_stopped"):
        s.admin(dict(operation="import", reservation=b["reservation"]))
    assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 0
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    provider.fail = True
    clock[0] += 5
    s.loop()
    assert s.guardian.status(request["operation"])["status"] == "unknown"
    with pytest.raises(ValueError, match="writers_not_stopped"):
        s.admin(dict(operation="import", reservation=b["reservation"]))
    assert s.custody.db.execute("SELECT manifest FROM inventories").fetchone()[0] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", "2"),
        ("operation", "b" * 32),
        ("dimension", 5),
        ("max_output_bytes", 999),
        ("max_output_files", True),
    ],
)
def test_exact_server_retained_argv(service_case, field, value):
    s, _, request, token, *_ = service_case
    bad = dict(request, **{field: value})
    with pytest.raises(ValueError):
        s.workload("/v1/workload/begin-once", dict(binding=bad), token)


def test_reallocation_and_unknown_provider_fence_workload(service_case):
    s, _, request, token, clock, provider, *_ = service_case
    provider.machine = "e" * 32
    clock[0] += 5
    s.loop()
    with pytest.raises(ValueError):
        s.workload("/v1/workload/begin-once", dict(binding=request), token)
    assert s.guardian.status(request["operation"])["allocation_uncertain"]
    assert "stop" in provider.calls


def test_consumption_survives_restart_and_never_resets_epoch(service_case):
    s, b, request, token, clock, provider, key, root = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    s.close()
    recovered = make_service(root, lambda: clock[0], key, provider)
    try:
        recovered.loop()
        assert recovered._row(b["reservation"])["begun"] == 1
        with pytest.raises(ValueError):
            recovered.workload("/v1/workload/begin-once", dict(binding=request), token)
        with pytest.raises(ValueError):
            recovered.admin(b)
        assert "stop" in provider.calls
    finally:
        recovered.close()
    # Fixture cleanup tolerates the already closed original owner.
    s.close = lambda: None


def test_readiness_requires_loop_and_custody_not_signature_config(service_case):
    s, b, _, _, clock, _, *_ = service_case
    clock[0] += 3
    with pytest.raises(ValueError, match="loop_unready"):
        s.admin(dict(operation="ready", reservation=b["reservation"]))
    s.loop()
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    with pytest.raises(ValueError):
        s.admin(dict(operation="ready", reservation=b["reservation"]))


def test_caps_and_checksums_and_output_replay(service_case):
    s, _, request, token, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    with pytest.raises(ValueError, match="output_rejected"):
        s.workload("/v1/workload/output", dict(binding=request, output={}, sha256="a" * 64), token)
    output = {"value": "a" * 2000}
    with pytest.raises(ValueError):
        s.workload(
            "/v1/workload/output",
            dict(
                binding=request, output=output, sha256=hashlib.sha256(encoded(output)).hexdigest()
            ),
            token,
        )
    output = {"value": 1}
    envelope = dict(
        binding=request, output=output, sha256=hashlib.sha256(encoded(output)).hexdigest()
    )
    s.workload("/v1/workload/output", envelope, token)
    with pytest.raises((ValueError, sqlite3.IntegrityError)) as error:
        s.workload("/v1/workload/output", envelope, token)
    assert "token" not in str(error.value)


def test_supplied_readiness_assertion_rejected(service_case):
    s, b, *_ = service_case
    with pytest.raises(ValueError, match="admin_request"):
        s.admin(dict(operation="ready", reservation=b["reservation"], conformance=True))


def test_reservation_package_and_deadline_never_extend(service_case):
    s, body, _, _, clock, *_ = service_case
    first = s._row(body["reservation"])["deadline"]
    clock[0] += 1
    s.loop()
    repeated = dict(body, deadline=first + 1)
    assert s.admin(repeated)["state"] == "ready"
    assert s._row(body["reservation"])["deadline"] == first
    with pytest.raises(ValueError):
        s.admin(dict(body, deadline=clock[0]))
    with pytest.raises(ValueError):
        s.admin(dict(body, deadline=clock[0] + 31))
    bad = dict(body, routing_package=dict(body["routing_package"], task_id="foreign-task"))
    with pytest.raises(ValueError):
        s.admin(bad)
    with pytest.raises(ValueError):
        s.admin(dict(body, project_id="foreign-project"))


def test_transient_instance_version_not_new_allocation(service_case):
    s, _, request, _, clock, provider, *_ = service_case
    provider.version += 1
    clock[0] += 5
    s.loop()
    assert not s.guardian.status(request["operation"])["allocation_uncertain"]


def test_reallocation_between_native_bind_and_first_guardian_poll_is_sticky(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    try:
        b = reservation()
        s.loop()
        s.admin(b)
        s.admin(
            dict(
                operation="bind",
                reservation=b["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        provider.machine = "e" * 32
        s.loop()
        assert s.guardian.status("a" * 32)["allocation_uncertain"]
        assert s._row(b["reservation"])["machine"] == MACHINE
        provider.machine = MACHINE
        provider.state = "stopped"
        clock[0] += 5
        s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["allocation_uncertain"]
    finally:
        s.close()


def test_prestart_stopped_is_not_completion_and_missing_output_unknown(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    service = make_service(tmp_path, lambda: clock[0], key, provider)
    try:
        body = reservation()
        service.loop()
        service.admin(body)
        service.admin(
            dict(
                operation="bind",
                reservation=body["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        service.loop()
        assert service.guardian.status("a" * 32)["status"] == "armed"
        assert service.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 0
        clock[0] = 131
        service.loop()
        status = service.guardian.status("a" * 32)
        assert status["status"] == "stopped"
        assert not status["manifest"]["complete"] and status["quarantine"]
    finally:
        service.close()


def test_absolute_provider_budget_preserves_stop_unknown(service_case, monkeypatch):
    s, b, request, _, _, _, *_ = service_case

    def hung(reference):
        time.sleep(2)

    monkeypatch.setattr(s.resolver, "metadata", hung)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with alarm_budget(0.02):
            s._send(s._row(b["reservation"]), "read_group", GROUP)
    assert time.monotonic() - started < 1
    assert s.guardian.status(request["operation"])["status"] != "stopped"


@pytest.mark.parametrize("change", ["stale", "changed", "outage"])
def test_rate_uncertainty_stops_and_fences_new_work(service_case, change):
    s, b, request, token, clock, provider, *_ = service_case
    if change == "stale":
        provider.rate_age = 6
    elif change == "changed":
        provider.rate = "0.2"
    else:
        provider.fail = True
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["budget_fenced"] and status["rate_unknown"]
    assert status["exposure_pending"] and status["actual_billed_cost_cents"] is None
    assert status["status"] == "unknown"
    with pytest.raises(ValueError):
        s.workload("/v1/workload/begin-once", dict(binding=request), token)


def test_orphan_exposure_survives_deadline_outage_and_exceeds_estimate_cap(service_case):
    s, b, _, _, clock, provider, *_ = service_case
    provider.fail = True
    clock[0] += 4000
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert Decimal(status["estimated_exposure_cents"]) > 10
    assert status["escalation_pending"] and not status["financial_hard_cap"]
    assert status["orphan"] and status["actual_billed_cost_cents"] is None


def test_exposure_ledger_restart_retains_unknown_estimate(service_case):
    s, b, _, _, clock, provider, key, root = service_case
    clock[0] += 5
    before = s.admin(dict(operation="status", reservation=b["reservation"]))
    s.close()
    recovered = make_service(root, lambda: clock[0], key, provider)
    try:
        recovered.loop()
        after = recovered.admin(dict(operation="status", reservation=b["reservation"]))
        assert Decimal(after["estimated_exposure_cents"]) >= Decimal(
            before["estimated_exposure_cents"]
        )
        assert after["actual_billed_cost_cents"] is None and after["exposure_pending"]
        assert "stop" in provider.calls
    finally:
        recovered.close()


def test_insufficient_published_rate_budget_fails_before_arm(tmp_path):
    tmp_path.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.rate = "3600"
    s = make_service(tmp_path, lambda: 100, key, provider)
    try:
        s.loop()
        with pytest.raises(ValueError, match="budget_insufficient"):
            s.admin(reservation())
        assert not s.db.execute("SELECT 1 FROM reservations").fetchone()
        assert provider.calls == ["gpu_class"]
    finally:
        s.close()


def test_reviewed_smoke_binding_preserves_source_package_and_rejects_tamper(tmp_path):
    tmp_path.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, key, provider)
    try:
        body = reservation(argv_binding="smoke_operation_v1")
        before = encoded(body["runtime_package"])
        s.loop()
        assert s.admin(body)["state"] == "ready"
        assert encoded(body["runtime_package"]) == before
        retained = json.loads(s._row(body["reservation"])["runtime_package"])
        assert retained["plan"]["execution_spec"]["argv"][3:5] == [
            "operation-id",
            "operation-epoch",
        ]
        body["runtime_package"]["plan"]["execution_spec"]["argv"][3] = "other-placeholder"
        with pytest.raises(ValueError):
            s.admin(body)
    finally:
        s.close()


@pytest.mark.parametrize("paths", [None, [], ["different.json"], ["output.json", "extra.json"]])
def test_missing_or_changed_approved_output_paths_rejected_before_provider(tmp_path, paths):
    tmp_path.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, key, provider)
    try:
        body = reservation()
        runtime = body["runtime_package"]
        spec = runtime["plan"]["execution_spec"]
        if paths is None:
            del spec["output_paths"]
        else:
            spec["output_paths"] = paths
            ExecutionSpec.model_validate(spec)  # Valid schema, unsupported smoke inventory.
        runtime["plan_digest"] = canonical_digest(runtime["plan"])
        body["proposal"]["plan_digest"] = runtime["plan_digest"]
        body["proposal"]["package_digest"] = canonical_digest(runtime)
        body["routing_package"]["proposal"] = body["proposal"]
        body["route_digest"] = canonical_digest(body["routing_package"])
        body["reservation"] = canonical_digest([body["route_id"], body["route_digest"]])
        s.loop()
        with pytest.raises(ValueError):
            s.admin(body)
        assert provider.calls == []
        assert not s.db.execute("SELECT 1 FROM reservations").fetchone()
    finally:
        s.close()


def provision_tls(root):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(
            key, __import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256()
        )
    )
    cert = root / "cert.pem"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private = root / "key.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    token = root / "admin-token"
    token.write_bytes(b"isolated-admin-authentication-marker-123456789")
    token.chmod(0o600)
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


def child_service(root):
    provider = Provider()
    provider.state = "running"
    # Isolated fixture signing only: never used by the production CLI/JWKS path.
    key = serialization.load_pem_private_key((root / "key.pem").read_bytes(), password=None)
    service = make_service(root, time.time, key, provider)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    server = SafetyHTTPServer(
        ("127.0.0.1", 0), service, (root / "admin-token").read_bytes(), context=context
    )
    (root / "port").write_text(str(server.server_port))
    try:
        serve(service, server)
    finally:
        service.close()


def test_actual_separate_https_process_auth_and_readiness(tmp_path):
    tmp_path.chmod(0o700)
    pin = provision_tls(tmp_path)
    script = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[2]); "
        "from test_salad_safety_service import child_service; child_service(Path(sys.argv[1]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(Path(__file__).parent)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not (tmp_path / "port").exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert (tmp_path / "port").exists()
        port = int((tmp_path / "port").read_text())
        client = SafetyHTTPSClient(
            "localhost",
            port,
            token_file=tmp_path / "admin-token",
            ca_file=tmp_path / "cert.pem",
            certificate_sha256=pin,
        )
        b = reservation(seconds=5, now=time.time())
        assert client.exchange(b)["state"] == "ready"
        assert (
            client.exchange(dict(operation="ready", reservation=b["reservation"]))["state"]
            == "ready"
        )
        context = ssl.create_default_context(cafile=tmp_path / "cert.pem")
        conn = http.client.HTTPSConnection("localhost", port, context=context, timeout=3)
        conn.request(
            "POST",
            "/v1/admin",
            encoded(b),
            {"Content-Type": "application/json", "Authorization": "Bearer wrong"},
        )
        response = conn.getresponse()
        assert response.status == 401
        assert response.read() == b'{"error":"request_rejected"}'
        conn.close()
        bad = SafetyHTTPSClient(
            "localhost",
            port,
            token_file=tmp_path / "admin-token",
            ca_file=tmp_path / "cert.pem",
            certificate_sha256="0" * 64,
        )
        with pytest.raises(ValueError):
            bad.exchange(b)
        client.exchange(
            dict(
                operation="bind",
                reservation=b["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        key = serialization.load_pem_private_key((tmp_path / "key.pem").read_bytes(), password=None)
        payload = claims(time.time())
        payload["salad_workload_name"] = b["proposal"]["selection"]["salad"]["group"]
        token = signed(key, payload)
        request = SaladSafetyService._request(
            b["proposal"]["selection"]["salad"]["execution"]["argv"]
        )

        def workload(path, body):
            connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=3)
            try:
                connection.request(
                    "POST",
                    path,
                    encoded(body),
                    {"Content-Type": "application/json", "Authorization": "Bearer " + token},
                )
                reply = connection.getresponse()
                return reply.status, json.loads(reply.read(4096))
            finally:
                connection.close()

        status, reply = workload("/v1/workload/begin-once", dict(binding=request))
        assert status == 200 and reply["granted"]
        assert workload("/v1/workload/begin-once", dict(binding=request))[0] == 401
        output = {"isolated_cuda_observation": "not-real-gpu"}
        digest = hashlib.sha256(encoded(output)).hexdigest()
        assert (
            workload("/v1/workload/output", dict(binding=request, sha256=digest, output=output))[0]
            == 200
        )
        # No control request/heartbeat drives this interval: the separate service
        # must enforce the deadline on its own, and 202 remains pending.
        time.sleep(5.1)
        status = client.exchange(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["node_writers"] == "unknown"
        assert status["reason"] == "deadline"
        with pytest.raises(ValueError, match="service_unavailable"):
            client.exchange(dict(operation="import", reservation=b["reservation"]))
        assert status["retained_output"]["retained"]
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(ValueError):
            client.exchange(dict(operation="ready", reservation=b["reservation"]))
        process.communicate(timeout=5)
        (tmp_path / "port").unlink()  # Only the test listener-discovery marker.
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(Path(__file__).parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while (
            not (tmp_path / "port").exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert (tmp_path / "port").exists()
        port = int((tmp_path / "port").read_text())
        client = SafetyHTTPSClient(
            "localhost",
            port,
            token_file=tmp_path / "admin-token",
            ca_file=tmp_path / "cert.pem",
            certificate_sha256=pin,
        )
        with pytest.raises(ValueError, match="service_unavailable"):
            client.exchange(dict(operation="import", reservation=b["reservation"]))
        assert workload("/v1/workload/begin-once", dict(binding=request))[0] == 401
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
