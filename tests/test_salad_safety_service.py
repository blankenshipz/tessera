import copy
import datetime
import hashlib
import http.client
import json
import os
import re
import socket
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


def reservation(
    seconds=30, now=100, argv_binding="exact", workload_seconds=None, operation="a" * 32
):
    value = selection().model_dump(mode="json")
    value["resources"]["max_seconds"] = seconds
    value["salad"]["execution"]["argv_binding"] = argv_binding
    value["salad"]["execution"]["argv"] = [
        "v1",
        "public-integers-v1",
        operation,
        "1",
        "localhost",
        "a" * 64,
        str(workload_seconds or seconds),
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
        budget_binding=dict(
            experiment_id="experiment",
            admitted_attempt_id="attempt",
            grant_id="grant",
            authorization_revision=1,
            reservation_digest=canonical_digest(["experiment", "attempt", "grant", 1, 10]),
            operation_id=operation,
            lease_id="lease",
            epoch=1,
            package_digest=canonical_digest(runtime),
            max_exposure_cents=10,
            expires_at=runtime["hard_expires_at"],
        ),
        deadline=now + seconds,
        route_digest=digest,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "b" * 32),
        ("epoch", 2),
        ("grant_id", "other"),
        ("max_exposure_cents", 0),
        ("expires_at", "1970-01-01T00:00:01Z"),
    ],
)
def test_parent_budget_binding_mismatch_cannot_reserve(tmp_path, field, value):
    tmp_path.chmod(0o700)
    service = make_service(
        tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), Provider()
    )
    body = reservation()
    body["budget_binding"][field] = value
    try:
        with pytest.raises(ValueError):
            service.admin(body)
        assert service.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    finally:
        service.close()


def test_budget_binding_replay_cannot_replace_parent_and_cancel_retains_constraint(service_case):
    service, body, *_ = service_case
    before = service._row(body["reservation"])["budget_binding"]
    changed = copy.deepcopy(body)
    changed["budget_binding"]["authorization_revision"] = 2
    changed["budget_binding"]["reservation_digest"] = canonical_digest(
        ["experiment", "attempt", "grant", 2, 10]
    )
    with pytest.raises(ValueError, match="reservation_mismatch"):
        service.admin(changed)
    service.admin(dict(operation="cancel", reservation=body["reservation"]))
    assert service._row(body["reservation"])["budget_binding"] == before


class Provider:
    def __init__(self):
        self.state = "stopped"
        self.machine = MACHINE
        self.instance_state = "running"
        self.group_state = None  # Salad may report "deploying" while the instance runs
        self.calls = []
        self.fail = False
        self.version = 1
        self.rate = "0.1"
        self.rate_age = 0
        # Lost create: the reserved name was never created (Salad answers 404).
        self.group_missing = False
        self.rate_failures = 0  # fail the next N gpu_class reads only
        self.rate_error = None  # raise this (once) from the next gpu_class read
        self.read_failures = 0  # fail the next N group/instance reads only
        self.instance_failures = 0  # fail the next N read_instance calls only
        self.read_error = None  # raise this (once) from the next group/instance read
        self.group_without_instances = False  # read_group omits the instances list
        self.pending_change = False
        self.counts = None  # override instance_status_counts
        # read_absence reply: "not_found", "present", "unknown" (timeout/5xx), "raise"
        self.absence = "not_found"
        # list read after a definite 404: absent, absent_partial, present, unavailable
        self.list_read = "absent"

    def factory(self, selection):
        owner = self

        class Transport:
            def gpu_class(self, selection, secret):
                owner.calls.append("gpu_class")
                if owner.fail:
                    raise ValueError("isolated_rate_outage")
                if owner.rate_error is not None:
                    error, owner.rate_error = owner.rate_error, None
                    raise error
                if owner.rate_failures:
                    owner.rate_failures -= 1
                    return type("Result", (), dict(status="unknown", observation=None))()
                at = datetime.datetime.fromtimestamp(owner.clock() - owner.rate_age, datetime.UTC)
                observation = dict(
                    gpu_class_id=selection.resources.gpu_class,
                    hourly_price_usd=owner.rate,
                    price_basis="published_gpu_hourly_rate_not_actual_cost",
                    selection_digest=canonical_digest(selection.model_dump(mode="json")),
                    observed_at=at.isoformat().replace("+00:00", "Z"),
                )
                return type("Result", (), dict(status="observed", observation=observation))()

            def read_absence(self, selection, secret):
                owner.calls.append("read_absence")
                if owner.absence == "raise":
                    raise RuntimeError("do-not-echo-provider-error")
                if owner.absence == "unknown":
                    return type("Result", (), dict(status="unknown", observation=None))()
                observation = dict(
                    group_not_found=owner.absence == "not_found",
                    list_read=owner.list_read if owner.absence == "not_found" else "present",
                )
                return type("Result", (), dict(status="observed", observation=observation))()

            def send(self, action, selection, provider_id, secret):
                owner.calls.append(action)
                if owner.fail:
                    raise RuntimeError("do-not-echo-provider-error")
                if owner.read_error is not None and action in {"read_group", "read_instance"}:
                    error, owner.read_error = owner.read_error, None
                    raise error
                if owner.instance_failures and action == "read_instance":
                    owner.instance_failures -= 1
                    return type("Result", (), dict(status="unknown", observation=None))()
                if owner.read_failures and action in {"read_group", "read_instance"}:
                    owner.read_failures -= 1
                    return type("Result", (), dict(status="unknown", observation=None))()
                if owner.group_missing and action in {"read_group", "read_instance"}:
                    return type("Result", (), dict(status="unknown", observation=None))()
                instances = (
                    []
                    if owner.state == "stopped"
                    else [
                        dict(
                            instance_id="d" * 32,
                            machine_id=owner.machine,
                            state=owner.instance_state,
                            version=owner.version,
                        )
                    ]
                )
                if action == "stop":
                    return type("Result", (), dict(status="pending", observation={}))()
                observation = dict(
                    provider_id=GROUP,
                    state=owner.group_state or owner.state,
                    instance_status_counts=owner.counts
                    or {
                        k: int(owner.state == "running" and k == "running_count")
                        for k in (
                            "allocating_count",
                            "creating_count",
                            "running_count",
                            "stopping_count",
                        )
                    },
                    pending_change=owner.pending_change,
                    instances=instances,
                )
                if owner.group_without_instances and action == "read_group":
                    observation.pop("instances")
                return type("Result", (), dict(status="observed", observation=observation))()

        return Transport()


def make_service(root, clock, key, provider, **kwargs):
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
        **kwargs,
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
    salad = body["proposal"]["selection"]["salad"]
    payload["salad_workload_name"] = f"{salad['organization']}-{salad['project']}-{salad['group']}"
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
        clock[0] += 20  # past the close-check throttle
        s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        # Still sticky: never "stopped"/sealed; the exact provider read only
        # closes the spend exposure (provider-confirmed reconcile).
        assert status["status"] == "reconciled" and status["allocation_uncertain"]
        assert status["reason"] == "provider_confirmed_exact_group_stopped"
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


def test_one_transient_rate_read_failure_is_retried_not_fenced(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    provider.rate_failures = 1
    before = provider.calls.count("gpu_class")
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert not status["budget_fenced"] and not status["rate_unknown"]
    assert status["status"] != "unknown" and status["rate_read_retries"] == 1
    assert provider.calls.count("gpu_class") - before == 2 and slept == [0.2]


@pytest.mark.parametrize("failures", [3, 10])
def test_rate_reads_failing_every_attempt_still_fence_and_stop(service_case, failures):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    provider.rate_failures = failures
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["budget_fenced"] and status["rate_unknown"]
    assert status["status"] == "unknown"
    assert slept == [0.2, 0.4]  # three attempts in all, then fail-closed as before
    assert status["rate_read_retries"] == 2  # recorded even though every attempt failed
    with pytest.raises(ValueError):
        s.workload("/v1/workload/begin-once", dict(binding=request), token)


def test_a_slow_failed_rate_read_is_not_retried_past_the_budget(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    elapsed = [0.0]

    def monotonic():
        elapsed[0] += 2.5  # each read takes as long as a transport timeout
        return elapsed[0]

    s.retry_monotonic = monotonic
    slept = []
    s.retry_sleep = slept.append
    provider.rate_failures = 1
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["budget_fenced"] and status["rate_unknown"] and slept == []
    assert status["rate_read_retries"] == 0


def test_a_stale_rate_observation_is_not_retried(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    provider.rate_age = 6
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["budget_fenced"] and status["rate_unknown"] and slept == []


def test_one_transient_group_read_failure_is_retried_not_stopped(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    provider.read_failures = 1
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "armed" and status["stop_cause"] is None
    assert status["provider_read_retries"] == 1 and slept == [0.2]
    assert "stop" not in provider.calls


def test_group_reads_failing_every_attempt_stop_and_record_the_cause(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    provider.read_failures = 3
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "unknown" and status["stop_cause"] == "provider_unavailable"
    assert status["provider_read_retries"] == 2 and slept == [0.2, 0.4]
    assert "stop" in provider.calls


def test_rate_and_group_reads_each_have_their_own_retry_window(service_case):
    """A slow rate read cannot starve the group read's retry in the same pass. Time
    advances only with provider reads and backoff, never with how often it is read."""
    s, b, request, token, clock, provider, *_ = service_case
    elapsed = [0.0]
    s.retry_monotonic = lambda: elapsed[0]
    s.retry_sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    send = s._send

    def timed_send(*args, **kwargs):
        elapsed[0] += 1.2  # every provider read costs 1.2 s
        return send(*args, **kwargs)

    s._send = timed_send
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    provider.rate_failures, provider.read_failures = 1, 1
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["rate_read_retries"] == 1 and status["provider_read_retries"] == 1
    assert status["status"] == "armed" and status["stop_cause"] is None


def test_a_rate_read_that_timed_out_gets_one_retry(service_case):
    """A read failing by timeout uses a full transport deadline; the window still fits
    one retry after it (2 s + 0.2 s + 2 s <= 4.5 s)."""
    s, b, request, token, clock, provider, *_ = service_case
    elapsed = [0.0]
    s.retry_monotonic = lambda: elapsed[0]
    s.retry_sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    send = s._send

    def timed_send(*args, **kwargs):
        elapsed[0] += 2.0  # every read takes the full transport deadline
        return send(*args, **kwargs)

    s._send = timed_send
    provider.rate_error = TimeoutError("service_deadline")
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert not status["budget_fenced"] and status["rate_read_retries"] == 1
    assert status["last_rate_read_failure"] is None


@pytest.mark.parametrize(
    "setup,code",
    [("rate_failures", "provider_unknown"), ("stale", "rate_stale")],
)
def test_a_rate_read_that_gives_up_records_its_closed_reason(service_case, setup, code):
    s, b, request, token, clock, provider, *_ = service_case
    s.retry_sleep = lambda _: None
    if setup == "stale":
        provider.rate_age = 6
    else:
        provider.rate_failures = 3
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["rate_unknown"] and status["last_rate_read_failure"] == code
    rows = s.db.execute(
        "SELECT count(*) FROM events WHERE kind='rate_read_failed' AND reservation=?",
        (b["reservation"],),
    ).fetchone()[0]
    assert rows == 1


def test_a_group_read_that_gives_up_records_timeout(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    elapsed = [0.0]
    s.retry_monotonic = lambda: elapsed[0]
    s.retry_sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    send = s._send

    def timed_send(row, action, *args, **kwargs):
        elapsed[0] += 2.0
        if action in {"read_group", "read_instance"}:
            raise TimeoutError("service_deadline")  # every group read times out
        return send(row, action, *args, **kwargs)

    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    s._send = timed_send
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["provider_read_retries"] == 1  # its own window allowed one retry
    assert status["stop_cause"] == "provider_unavailable"
    assert status["last_provider_read_failure"] == "timeout"


def test_worst_case_loop_pass_stays_inside_the_allowance(service_case):
    """Every read takes a full transport deadline and times out: each read kind uses its
    own window (one retry each, then fail-closed). The whole pass, not just its retries,
    must fit LOOP_PASS_ALLOWANCE_SECONDS."""
    from tessera_control.salad_safety_service import (
        LOOP_PASS_ALLOWANCE_SECONDS,
        PROVIDER_READ_SECONDS,
    )

    s, b, request, token, clock, provider, *_ = service_case
    elapsed = [0.0]
    s.retry_monotonic = lambda: elapsed[0]
    s.retry_sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    send = s._send

    def timed_send(row, action, *args, **kwargs):
        elapsed[0] += PROVIDER_READ_SECONDS
        if action in {"gpu_class", "read_group", "read_instance"}:
            raise TimeoutError("service_deadline")  # both read kinds time out every time
        return send(row, action, *args, **kwargs)

    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    s._send = timed_send
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["rate_read_retries"] == 1 and status["rate_unknown"]
    assert status["provider_read_retries"] == 1
    assert status["last_rate_read_failure"] == "timeout"
    assert status["last_rate_read_failure_at"] is not None
    # The failed rate read already put the reservation into stop-only recovery, so the
    # guardian's own poll records no first stop decision (stop_cause stays None); the
    # provider failure is what the guardian reports as the reason.
    assert status["status"] == "unknown" and status["reason"] == "provider_unavailable"
    assert status["stop_cause"] is None
    assert elapsed[0] <= LOOP_PASS_ALLOWANCE_SECONDS - 1.0


def test_worst_case_loop_pass_with_the_instance_read_stays_inside_the_allowance(service_case):
    """The group read succeeds without its instances, so the follow-up instance read runs
    (and shares the group window); every read takes a full transport deadline and the
    instance read times out. The whole pass must still fit the allowance."""
    from tessera_control.salad_safety_service import (
        LOOP_PASS_ALLOWANCE_SECONDS,
        PROVIDER_READ_SECONDS,
    )

    s, b, request, token, clock, provider, *_ = service_case
    elapsed = [0.0]
    actions = []
    s.retry_monotonic = lambda: elapsed[0]
    s.retry_sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    send = s._send

    def timed_send(row, action, *args, **kwargs):
        elapsed[0] += PROVIDER_READ_SECONDS
        actions.append(action)
        if action == "read_instance":
            raise TimeoutError("service_deadline")
        result = send(row, action, *args, **kwargs)
        if action == "read_group" and type(result.observation) is dict:
            result.observation = {k: v for k, v in result.observation.items() if k != "instances"}
        return result

    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    s._send = timed_send
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert "read_instance" in actions
    assert status["provider_read_retries"] == 1
    assert status["stop_cause"] == "provider_unavailable"
    assert status["last_provider_read_failure"] == "timeout"
    assert elapsed[0] <= LOOP_PASS_ALLOWANCE_SECONDS - 1.0


def test_group_and_instance_reads_share_the_pinned_window_kinds():
    import ast
    import inspect

    from tessera_control import salad_safety_service as svc

    kinds = set()
    for node in ast.walk(ast.parse(inspect.getsource(svc))):
        if isinstance(node, ast.keyword) and node.arg == "failure_kind":
            kinds.add(ast.unparse(node.value))
    assert kinds == {"RATE_READ_KIND", "PROVIDER_READ_KIND"}
    assert {svc.RATE_READ_KIND, svc.PROVIDER_READ_KIND} == svc.PROVIDER_READ_WINDOW_KIND_NAMES
    assert svc.PROVIDER_READ_WINDOW_KINDS == 2
    assert (
        svc.UNIX_ADMIN_FRAME_SECONDS
        + svc.UNIX_ADMIN_BUDGET_SECONDS
        + svc.CHUNK_REQUEST_SECONDS
        + svc.LOOP_PASS_ALLOWANCE_SECONDS
        <= svc.UNSEEN_RUN_GAP_SECONDS
    )


def test_bind_retries_the_instance_read_after_the_group_read(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    try:
        slept = []
        s.retry_sleep = slept.append
        b = reservation()
        s.loop()
        s.admin(b)
        provider.group_without_instances = True
        provider.instance_failures = 1
        s.admin(
            dict(
                operation="bind",
                reservation=b["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["provider_read_retries"] == 1 and slept == [0.2]
        assert provider.calls.count("read_group") >= 1
    finally:
        s.close()


def test_a_timed_out_group_read_is_retried(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    provider.read_error = TimeoutError("service_deadline")
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "armed" and status["provider_read_retries"] == 1
    assert slept == [0.2]


def test_a_non_transient_group_read_error_is_not_retried(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    slept = []
    s.retry_sleep = slept.append
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    provider.read_error = RuntimeError("do-not-echo-provider-error")
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "unknown" and status["stop_cause"] == "provider_unavailable"
    assert status["provider_read_retries"] == 0 and slept == []


def test_refused_workload_calls_record_a_deduplicated_closed_code(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    for _ in range(3):
        with pytest.raises(ValueError):
            s.workload("/v1/workload/begin-once", dict(binding=request), bad)
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    code = status["last_workload_refusal"]
    assert isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
    assert bad not in json.dumps(status)
    rows = s.db.execute(
        "SELECT count(*) FROM events WHERE reservation=? AND kind='workload_refused'",
        (b["reservation"],),
    ).fetchone()[0]
    assert rows == 1  # deduplicated within the window
    clock[0] += 61
    s.loop()
    with pytest.raises(ValueError):
        s.workload("/v1/workload/begin-once", dict(binding=request), bad)
    rows = s.db.execute(
        "SELECT count(*) FROM events WHERE reservation=? AND kind='workload_refused'",
        (b["reservation"],),
    ).fetchone()[0]
    assert rows == 2


def test_reserve_tolerates_one_transient_rate_read_failure(tmp_path):
    """The reserve path reads the rate before any reservation row exists."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    try:
        slept = []
        s.retry_sleep = slept.append
        body = reservation()
        s.loop()
        provider.rate_failures = 1
        s.admin(body)
        assert slept == [0.2]
        status = s.admin(dict(operation="status", reservation=body["reservation"]))
        assert status["status"] in {"reserved", "armed"}
    finally:
        s.close()


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
        salad = b["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = (
            f"{salad['organization']}-{salad['project']}-{salad['group']}"
        )
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
        # No control request/heartbeat drives this: the separate service stops the
        # group by itself as soon as the approved output is retained, before the
        # deadline, and the stop's 202 remains pending.
        stop_by = time.monotonic() + 3
        while True:
            status = client.exchange(dict(operation="status", reservation=b["reservation"]))
            if status["reason"] != "armed" or time.monotonic() >= stop_by:
                break
            time.sleep(0.05)
        assert status["status"] == "unknown" and status["node_writers"] == "unknown"
        assert status["reason"] == "output_retained"
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


def test_guardian_survives_unix_client_that_disconnects_before_reply(tmp_path):
    import tempfile
    import threading

    from tessera_control.salad_safety_service import serve

    short = Path(tempfile.mkdtemp(prefix="tg", dir="/tmp"))  # AF_UNIX path length limit
    short.chmod(0o700)
    path = short / "g.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    listener.settimeout(0.01)
    served = []

    class Service:
        def loop(self):
            served.append(1)

        def admin(self, body):  # never reached without peer credentials here
            raise AssertionError("unexpected admin")

    class NoHTTP:
        def handle_request(self):
            pass

    stop = threading.Event()
    thread = threading.Thread(
        target=serve,
        args=(Service(), NoHTTP()),
        kwargs=dict(shutdown=stop.is_set, unix_listener=listener, proxy_uid=os.getuid()),
        daemon=True,
    )
    thread.start()
    try:
        # A client that hangs up before the reply previously crashed serve().
        for _ in range(3):
            gone = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            gone.connect(str(path))
            gone.close()
        time.sleep(0.3)
        assert thread.is_alive()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(path))
            client.sendall(b"{}\n")
            # A bounded code only; the assertion text never reaches the client.
            assert json.loads(client.recv(4096)) == {
                "error": "request_rejected",
                "code": "internal",
            }
        assert thread.is_alive() and served
    finally:
        stop.set()
        thread.join(5)
        listener.close()
        path.unlink(missing_ok=True)
        short.rmdir()


@pytest.mark.parametrize("beyond", [0, 1])
def test_guardian_deadline_covers_route_window_not_only_workload_runtime(tmp_path, beyond):
    tmp_path.chmod(0o700)
    service = make_service(
        tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), Provider()
    )
    # Route window 90s (allocation + image pull + workload), workload runtime 30s.
    body = reservation(seconds=90, workload_seconds=30)
    body["deadline"] = 100 + 90 + beyond
    try:
        service.loop()
        if beyond:
            with pytest.raises(ValueError, match="reservation_mismatch"):
                service.admin(body)
        else:
            assert service.admin(body)["state"] == "ready"
    finally:
        service.close()


def test_retained_output_stops_group_before_replacement_without_cancel(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    output = {"quarantined_result": 7}
    digest = hashlib.sha256(encoded(output)).hexdigest()
    before = len([c for c in provider.calls if c == "stop"])
    s.workload("/v1/workload/output", dict(binding=request, output=output, sha256=digest), token)
    # No cancel, no deadline, fresh heartbeat: retention alone must start the stop.
    s.loop()
    assert len([c for c in provider.calls if c == "stop"]) == before + 1
    assert s.guardian.status(b["proposal"]["operation_id"])["reason"] == "output_retained"
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["status"] == "stopped" and status["retained_output"]["retained"]
    assert s.admin(dict(operation="import", reservation=b["reservation"]))["output"] == output


def test_operator_reconcile_closes_uncertain_orphan_without_claiming_stop(tmp_path):
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
        reconcile = dict(
            operation="reconcile",
            reservation=b["reservation"],
            attestation="operator: portal shows group stopped, 0 instances",
        )
        # No stop decision yet: an armed operation is never reconciled, and a
        # refused reconcile has no side effect (writers stay open).
        with pytest.raises(ValueError, match="reconcile_not_applicable"):
            s.admin(reconcile)
        assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 0
        provider.machine = "e" * 32  # Salad moved the instance: allocation uncertain.
        s.loop()
        assert s.guardian.status("a" * 32)["allocation_uncertain"]
        # Still running somewhere: the service's own fresh read refuses.
        with pytest.raises(ValueError, match="reconcile_requires_exact_stopped_group"):
            s.admin(reconcile)
        with pytest.raises(ValueError):
            s.admin(dict(reconcile, attestation=""))
        provider.machine = MACHINE
        provider.state = "stopped"
        clock[0] += 5
        # The operator may close it before the loop's own provider-confirmed close.
        status = s.admin(reconcile)
        assert status["status"] == "reconciled" and status["allocation_uncertain"]
        assert status["orphan"] is False
        assert status["reason"] == "operator_reconciled_exact_group_stopped"
        assert s._row(b["reservation"])["reconciled_attestation"]
        # Accounting is frozen: a reconciled operation accrues nothing further.
        frozen = s._row(b["reservation"])["accrued_cents"]
        clock[0] += 3600
        s.loop()
        assert s._row(b["reservation"])["accrued_cents"] == frozen
        assert not s._row(b["reservation"])["escalation"]
        # Reconciled is not a stop/acceptance: custody import stays refused.
        with pytest.raises(ValueError, match="writers_not_stopped"):
            s.admin(dict(operation="import", reservation=b["reservation"]))
        with pytest.raises(ValueError, match="reconcile_not_applicable"):
            s.admin(reconcile)
        # The loop leaves it alone and a new operation is no longer fenced by it.
        before = list(provider.calls)
        clock[0] += 10
        s.loop()
        assert "stop" not in provider.calls[len(before) :]
        # The single-operation orphan fence admits new work only when every other
        # reservation is stopped or reconciled.
        statuses = {
            s._status(s._row(r))["status"]
            for (r,) in s.db.execute("SELECT reservation FROM reservations")
        }
        assert statuses == {"reconciled"}
    finally:
        s.close()


def test_provider_confirmed_stop_closes_uncertain_orphan_without_operator(tmp_path):
    """A failed workload, a replacement instance on another node: the allocation is
    uncertain and custody cannot seal, but once the exact group
    reads stopped with zero instances the loop itself ends the spend: no more stop
    retries, accrual frozen, fence released; custody stays unsealed and unimportable."""
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
        s.loop()
        provider.machine = "e" * 32  # replacement instance on another node
        clock[0] += 5
        s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["allocation_uncertain"]
        # Group-level "stopped" while an instance still runs is not exact: keep stopping.
        provider.group_state = "stopped"
        for _ in range(2):
            clock[0] += 40
            s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["orphan"] is True
        assert "stop" in provider.calls
        # The fence holds while the orphan is open.
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        # Exact stopped group, zero instances: closed by the provider's own read.
        provider.group_state = None
        provider.state = "stopped"
        clock[0] += 40
        s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reconciled"
        assert status["reason"] == "provider_confirmed_exact_group_stopped"
        assert status["orphan"] is False and status["allocation_uncertain"]
        assert s._row(b["reservation"])["reconciled_attestation"] is None
        # The fence releases: the next operation is admitted.
        nxt = _next_reservation(s, clock)
        s.admin(nxt)
        assert s._status(s._row(nxt["reservation"]))["status"] == "reserved"
        frozen = s._row(b["reservation"])["accrued_cents"]
        before = len(provider.calls)
        clock[0] += 3600
        s.loop()
        assert s._row(b["reservation"])["accrued_cents"] == frozen
        assert "stop" not in provider.calls[before:]
        # Never a seal or acceptance: import refused, operator reconcile not applicable.
        with pytest.raises(ValueError, match="writers_not_stopped"):
            s.admin(dict(operation="import", reservation=b["reservation"]))
        with pytest.raises(ValueError, match="reconcile_not_applicable"):
            s.admin(
                dict(
                    operation="reconcile",
                    reservation=b["reservation"],
                    attestation="operator: stopped",
                )
            )
        kinds = [k for (k,) in s.guardian.db.execute("SELECT kind FROM events")]
        assert "provider_confirmed_stop" in kinds and "exact_terminal" not in kinds
    finally:
        s.close()


def test_custody_failure_never_leaves_a_reconciled_operation_with_writers_open(
    tmp_path, monkeypatch
):
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
        s.loop()
        provider.machine = "e" * 32
        clock[0] += 5
        s.loop()
        provider.state = "stopped"
        real = s.custody.close_writers
        failures = []

        def failing(binding):
            failures.append(binding)
            raise RuntimeError("custody store unavailable")

        monkeypatch.setattr(s.custody, "close_writers", failing)
        for _ in range(2):
            clock[0] += 20
            s.loop()
        assert failures  # the close was attempted, and refused as a whole
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["orphan"] is True
        monkeypatch.setattr(s.custody, "close_writers", real)
        clock[0] += 20
        s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reconciled"
        assert status["reason"] == "provider_confirmed_exact_group_stopped"
        closed = s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0]
        assert closed == 1
    finally:
        s.close()


@pytest.mark.parametrize(
    "variant", ["pending_change", "stopping_count", "creating_count", "group_running"]
)
def test_non_exact_stopped_read_never_closes_uncertain_orphan(tmp_path, variant):
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
        s.loop()
        provider.machine = "e" * 32
        clock[0] += 5
        s.loop()
        provider.state = "stopped"  # no instances listed
        zero = dict.fromkeys(
            ("allocating_count", "creating_count", "running_count", "stopping_count"), 0
        )
        if variant == "pending_change":
            provider.pending_change = True
        elif variant == "group_running":
            provider.group_state = "running"
        else:
            provider.counts = dict(zero, **{variant: 1})
        for _ in range(4):
            clock[0] += 40
            s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["orphan"] is True
        with pytest.raises(ValueError, match="reconcile_requires_exact_stopped_group"):
            s.admin(
                dict(
                    operation="reconcile",
                    reservation=b["reservation"],
                    attestation="operator: looks stopped",
                )
            )
    finally:
        s.close()


def test_provider_read_failure_never_closes_uncertain_orphan(tmp_path):
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
        s.loop()
        provider.machine = "e" * 32
        clock[0] += 5
        s.loop()
        provider.state = "stopped"
        provider.fail = True  # unreadable provider: no evidence of a stop
        for _ in range(3):
            clock[0] += 40
            s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "unknown" and status["orphan"] is True
        provider.fail = False
        provider.group_missing = True  # unknown read is not a stop either
        clock[0] += 40
        s.loop()
        assert s.admin(dict(operation="status", reservation=b["reservation"]))["status"] == (
            "unknown"
        )
    finally:
        s.close()


def test_pre_start_node_attempts_are_not_reallocation_but_later_moves_are(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.instance_state = "allocating"
    provider.machine = "e" * 32  # Salad tries one node while allocating...
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
        s.loop()
        assert s._row(b["reservation"])["allocation"] is None
        provider.machine = MACHINE  # ...then the container starts on another node.
        provider.instance_state = "running"
        clock[0] += 5
        s.loop()
        assert not s.guardian.status("a" * 32)["allocation_uncertain"]
        assert s._row(b["reservation"])["machine"] == MACHINE
        # Once running is bound, a different node in any state is a real move.
        provider.machine = "f" * 32
        provider.instance_state = "allocating"
        clock[0] += 5
        s.loop()
        assert s.guardian.status("a" * 32)["allocation_uncertain"]
    finally:
        s.close()


def test_reconcile_refused_for_ordinary_stop_keeps_output_importable(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    s.workload("/v1/workload/begin-once", dict(binding=request), token)
    output = {"result": 1}
    digest = hashlib.sha256(encoded(output)).hexdigest()
    s.workload("/v1/workload/output", dict(binding=request, output=output, sha256=digest), token)
    provider.state = "stopped"
    s.loop()  # output_retained stop decision, but no allocation uncertainty
    with pytest.raises(ValueError, match="reconcile_not_applicable"):
        s.admin(dict(operation="reconcile", reservation=b["reservation"], attestation="x"))
    clock[0] += 5
    s.loop()
    assert s.admin(dict(operation="import", reservation=b["reservation"]))["output"] == output


def test_begin_once_right_after_start_binds_from_fresh_read(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.instance_state = "creating"
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
        s.loop()
        assert s._row(b["reservation"])["machine"] is None  # still creating: unbound
        # The container starts and calls begin-once before the next loop tick.
        provider.instance_state = "running"
        clock[0] += 1
        s.loop_at = clock[0]
        request = s._request(b["proposal"]["selection"]["salad"]["execution"]["argv"])
        payload = claims(clock[0])
        salad = b["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = "-".join(
            salad[k] for k in ("organization", "project", "group")
        )
        token = signed(key, payload)
        assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
        assert s._row(b["reservation"])["machine"] == MACHINE
    finally:
        s.close()


def test_control_status_accepts_reconciled():
    from tessera_control.salad_server import SaladSafetyClient

    client = SaladSafetyClient.__new__(SaladSafetyClient)
    client._call = lambda body: {"status": "reconciled", "allocation_uncertain": True}
    state = {"id": "route", "proposal_digest": "a" * 64}
    assert client.status(state)["status"] == "reconciled"


def test_begin_once_while_group_still_deploying_but_instance_running(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    # Observed live (2026-11-19): group "deploying", its single instance "running".
    provider.group_state = "deploying"
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    # An instance that is not running is still refused.
    other = provider.instance_state
    provider.instance_state = "creating"
    try:
        with pytest.raises(ValueError, match="workload_rejected"):
            s.workload("/v1/workload/begin-once", dict(binding=request), token)
    finally:
        provider.instance_state = other


@pytest.mark.parametrize("same_machine", [True, False])
def test_begin_once_before_api_reports_running_binds_only_on_token_machine(tmp_path, same_machine):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.group_state = "deploying"
    provider.instance_state = "creating"  # API still lags the running container
    if not same_machine:
        provider.machine = "e" * 32  # token below is signed for MACHINE
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
        s.loop()
        assert s._row(b["reservation"])["machine"] is None
        s.loop_at = clock[0]
        request = s._request(b["proposal"]["selection"]["salad"]["execution"]["argv"])
        payload = claims(clock[0])
        salad = b["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = "-".join(
            salad[k] for k in ("organization", "project", "group")
        )
        token = signed(key, payload)
        if same_machine:
            assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
            assert s._row(b["reservation"])["machine"] == MACHINE
        else:
            with pytest.raises(ValueError, match="workload_identity_rejected"):
                s.workload("/v1/workload/begin-once", dict(binding=request), token)
            assert s._row(b["reservation"])["machine"] is None
    finally:
        s.close()


@pytest.mark.parametrize(
    "group_state,instance_state",
    [("deploying", "stopping"), ("failed", "creating"), ("stopped", "creating")],
)
def test_token_bind_refused_for_stopping_instance_or_inactive_group(
    tmp_path, group_state, instance_state
):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.group_state = "deploying"
    provider.instance_state = "creating"
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
        s.loop()
        provider.group_state, provider.instance_state = group_state, instance_state
        s.loop_at = clock[0]
        request = s._request(b["proposal"]["selection"]["salad"]["execution"]["argv"])
        payload = claims(clock[0])
        salad = b["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = "-".join(
            salad[k] for k in ("organization", "project", "group")
        )
        with pytest.raises(ValueError, match="workload_rejected"):
            s.workload("/v1/workload/begin-once", dict(binding=request), signed(key, payload))
        row = s._row(b["reservation"])
        assert not row["begun"]
        if instance_state != "stopping":
            # A pre-start instance is never bound from a token in an inactive group.
            # (A stopping instance was started, so the guardian tracks its allocation.)
            assert row["machine"] is None and row["allocation"] is None
    finally:
        s.close()


def test_token_bind_checks_deadline_before_persisting(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.group_state = "deploying"
    provider.instance_state = "creating"
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
        s.loop()
        with s.db:  # heartbeat already stale when the late token arrives
            s.db.execute(
                "UPDATE reservations SET heartbeat=? WHERE reservation=?",
                (clock[0] - 31, b["reservation"]),
            )
        s.loop_at = clock[0]
        request = s._request(b["proposal"]["selection"]["salad"]["execution"]["argv"])
        payload = claims(clock[0])
        salad = b["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = "-".join(
            salad[k] for k in ("organization", "project", "group")
        )
        with pytest.raises(ValueError, match="workload_rejected"):
            s.workload("/v1/workload/begin-once", dict(binding=request), signed(key, payload))
        assert s._row(b["reservation"])["machine"] is None
    finally:
        s.close()


def _bind_and_run(service, provider, body):
    service.admin(body)
    service.admin(
        dict(
            operation="bind", reservation=body["reservation"], provider_id=GROUP, allocation_id=None
        )
    )


def _status(service, body):
    return service.admin(dict(operation="status", reservation=body["reservation"]))


def _beat(s, body, clock, seconds=5):
    clock[0] += seconds
    s.loop()
    s.admin(dict(operation="heartbeat", reservation=body["reservation"]))


def test_accrual_is_zero_before_first_running_and_accrues_after(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    body = reservation(seconds=300, workload_seconds=60)
    try:
        s.loop()
        _bind_and_run(s, provider, body)
        provider.state, provider.instance_state = "allocating", "allocating"
        for _ in range(4):
            _beat(s, body, clock)
        before = _status(s, body)
        assert Decimal(before["estimated_exposure_cents"]) == 0
        assert before["first_running_at"] is None
        unseen = s._row(body["reservation"])["last_unseen_at"]
        provider.state, provider.instance_state = "running", "running"
        _beat(s, body, clock)
        # Billing is backdated to the last poll that did not see it running.
        assert s._row(body["reservation"])["first_running_at"] == unseen < clock[0]
        _beat(s, body, clock)
        after = _status(s, body)
        # <=10 s at 0.1 USD/h = ~0.028 cents; the earlier provisioning cost nothing.
        assert 0 < Decimal(after["estimated_exposure_cents"]) < Decimal("0.04")
    finally:
        s.close()


def test_pre_create_admission_checks_billed_window_not_route_window(tmp_path):
    tmp_path.chmod(0o700)
    provider = Provider()
    from tessera_control.salad_safety_service import UNSEEN_RUN_GAP_SECONDS

    # 5 USD/h (10 cents buys 72 s), billed = running + UNSEEN_RUN_GAP_SECONDS (45 s):
    # 40 s running bills 85 s = 11.8 cents > 10; 2 s running bills 47 s = 6.5 cents < 10.
    # The 90 s route window would bill 135 s = 18.8 cents: only the billed window counts.
    assert UNSEEN_RUN_GAP_SECONDS == 45
    provider.rate = "5"
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        s.loop()
        with pytest.raises(ValueError, match="budget_insufficient"):
            s.admin(reservation(seconds=90, workload_seconds=40))
        assert s.admin(reservation(seconds=90, workload_seconds=2))["state"] == "ready"
        assert (
            s._row(reservation(seconds=90, workload_seconds=2)["reservation"])["running_seconds"]
            == 2
        )
    finally:
        s.close()


def test_service_stops_at_first_running_plus_running_seconds_with_longer_route(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    body = reservation(seconds=900, workload_seconds=30)
    try:
        s.loop()
        _bind_and_run(s, provider, body)
        provider.state, provider.instance_state = "allocating", "allocating"
        for _ in range(8):  # 40s of provisioning: far beyond the 30s running window
            _beat(s, body, clock)
        assert "stop" not in provider.calls
        provider.state, provider.instance_state = "running", "running"
        _beat(s, body, clock)
        started = s._row(body["reservation"])["first_running_at"]
        assert started is not None
        while clock[0] < started + 24:
            _beat(s, body, clock)
        assert "stop" not in provider.calls
        for _ in range(3):
            clock[0] += 5
            s.loop()
        assert "stop" in provider.calls
        assert s.guardian.status(body["proposal"]["operation_id"])["reason"] == "deadline"
        assert clock[0] < body["deadline"] - 800
    finally:
        s.close()


def test_never_running_instance_stops_at_route_deadline(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    body = reservation(seconds=60, workload_seconds=10)
    try:
        s.loop()
        _bind_and_run(s, provider, body)
        provider.state, provider.instance_state = "allocating", "allocating"
        while clock[0] < 100 + 55:
            _beat(s, body, clock)
        assert "stop" not in provider.calls
        clock[0] += 10
        s.loop()
        assert "stop" in provider.calls
        assert s.guardian.status(body["proposal"]["operation_id"])["reason"] == "deadline"
        assert Decimal(_status(s, body)["estimated_exposure_cents"]) == 0
    finally:
        s.close()


def _settled_first_run(tmp_path, cap, clock, provider):
    s = make_service(
        tmp_path,
        lambda: clock[0],
        ec.generate_private_key(ec.SECP256R1()),
        provider,
        total_cap_cents=cap,
    )
    first = reservation(seconds=300, workload_seconds=60)
    s.loop()
    _bind_and_run(s, provider, first)
    provider.state = "running"
    _beat(s, first, clock)
    for _ in range(5):
        _beat(s, first, clock)  # 25 s of billed time, nonzero frozen estimate
    s.admin(dict(operation="cancel", reservation=first["reservation"]))
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    status = _status(s, first)
    assert status["status"] == "stopped"
    return s, first, status


def test_total_cap_counts_frozen_estimate_and_refuses_overrun(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s, first, status = _settled_first_run(tmp_path, 11, clock, provider)
    try:
        frozen = Decimal(status["estimated_exposure_cents"])
        assert 0 < frozen < 1
        assert status["total_cap_cents"] == 11
        assert Decimal(status["total_committed_cents"]) == frozen
        assert Decimal(status["total_remaining_cents"]) == 11 - frozen
        assert status["financial_hard_cap"] is False
        # Stopped run contributes its frozen estimate only: 10 + frozen fits in 11.
        second = reservation(now=int(clock[0]), operation="b" * 32)
        assert s.admin(second)["state"] == "ready"
        # Third run: 10 more would exceed the cap while the second holds its cap.
        third = reservation(now=int(clock[0]), operation="c" * 32)
        with pytest.raises(ValueError, match="total_budget_exhausted"):
            s.admin(third)
        assert not s.db.execute(
            "SELECT 1 FROM reservations WHERE operation=?", ("c" * 32,)
        ).fetchone()
        live = _status(s, second)
        assert Decimal(live["total_committed_cents"]) == frozen + 10
    finally:
        s.close()


def test_total_cap_refuses_reservation_that_does_not_fit(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s, first, status = _settled_first_run(tmp_path, 10, clock, provider)
    try:
        # frozen > 0, so 10 + frozen > 10: refused before any provider effect.
        with pytest.raises(ValueError, match="total_budget_exhausted"):
            s.admin(reservation(now=int(clock[0]), operation="b" * 32))
    finally:
        s.close()


def test_total_cap_committed_uses_per_run_cap_for_unsettled(tmp_path):
    tmp_path.chmod(0o700)
    provider = Provider()
    s = make_service(
        tmp_path,
        lambda: 100,
        ec.generate_private_key(ec.SECP256R1()),
        provider,
        total_cap_cents=15,
    )
    body = reservation()
    try:
        s.loop()
        s.admin(body)
        status = _status(s, body)
        assert Decimal(status["total_committed_cents"]) == 10
        assert Decimal(status["total_remaining_cents"]) == 5
        with pytest.raises(ValueError, match="total_budget_exhausted"):
            s.admin(reservation(operation="b" * 32))
    finally:
        s.close()


def test_status_without_total_cap_is_unchanged(service_case):
    s, b, *_ = service_case
    status = _status(s, b)
    assert s.total_cap_cents is None
    assert status["total_cap_cents"] is None
    assert status["total_committed_cents"] is None
    assert status["total_remaining_cents"] is None
    assert status["financial_hard_cap"] is False


@pytest.mark.parametrize("cap", [0, -1, 1_000_001, 5.5, True, "10"])
def test_invalid_total_cap_rejected(tmp_path, cap):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="invalid_total_cap"):
        make_service(
            tmp_path,
            lambda: 100,
            ec.generate_private_key(ec.SECP256R1()),
            Provider(),
            total_cap_cents=cap,
        )


def test_unset_cost_cap_counts_the_approved_per_run_cap(service_case):
    s, b, *_ = service_case
    with s.db:
        s.db.execute("UPDATE reservations SET cost_cap=NULL, accrued_cents='0'")
    per_run = b["proposal"]["selection"]["salad"]["execution"]["max_cost_cents"]
    assert s._committed(account=False) >= per_run


def test_accrual_starts_at_last_unseen_poll_not_first_seen_running(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.instance_state = "creating"
    provider.group_state = "deploying"
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
        assignment = s._assignment(s._row(b["reservation"]))
        s._observe(assignment)  # not running yet
        unseen = s._row(b["reservation"])["last_unseen_at"]
        assert unseen == clock[0]
        clock[0] += 12
        provider.instance_state = "running"
        s._observe(assignment)
        assert s._row(b["reservation"])["first_running_at"] == unseen
    finally:
        s.close()


def test_stopped_without_being_seen_running_is_charged_a_poll_gap(tmp_path):
    from tessera_control.salad_safety_service import UNSEEN_RUN_GAP_SECONDS

    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.state = "running"
    provider.instance_state = "creating"
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
        assignment = s._assignment(s._row(b["reservation"]))
        clock[0] += 20
        provider.state = "stopped"  # ran and stopped between two polls
        s._observe(assignment)
        first = s._row(b["reservation"])["first_running_at"]
        assert first is not None and first <= clock[0] - UNSEEN_RUN_GAP_SECONDS
        s._account(s._row(b["reservation"]), force=True)
        assert Decimal(s._row(b["reservation"])["accrued_cents"]) > 0
    finally:
        s.close()


def test_begin_once_stamps_billing_start(service_case):
    s, b, request, token, *_ = service_case
    with s.db:
        s.db.execute("UPDATE reservations SET first_running_at=NULL, last_unseen_at=NULL")
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    assert s._row(b["reservation"])["first_running_at"] is not None


def _bound_service(tmp_path, clock, provider):
    key = ec.generate_private_key(ec.SECP256R1())
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    b = reservation()
    s.loop()
    s.admin(b)
    s.admin(
        dict(operation="bind", reservation=b["reservation"], provider_id=GROUP, allocation_id=None)
    )
    return s, b


def test_first_poll_already_running_backdates_one_poll_gap(tmp_path):
    from tessera_control.salad_safety_service import UNSEEN_RUN_GAP_SECONDS

    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    provider.state = "running"  # first observation already sees it running
    s, b = _bound_service(tmp_path, clock, provider)
    try:
        assert s._row(b["reservation"])["last_unseen_at"] is None
        s._observe(s._assignment(s._row(b["reservation"])))
        first = s._row(b["reservation"])["first_running_at"]
        assert first == clock[0] - UNSEEN_RUN_GAP_SECONDS
    finally:
        s.close()


def test_empty_read_while_deploying_never_shrinks_the_backdated_window(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    provider.state = "running"
    provider.group_state = "deploying"
    provider.instance_state = "creating"
    s, b = _bound_service(tmp_path, clock, provider)
    try:
        assignment = s._assignment(s._row(b["reservation"]))
        s._observe(assignment)
        unseen = s._row(b["reservation"])["last_unseen_at"]
        assert unseen == clock[0]
        # A deploying group whose read shows no instance is ambiguous, not proof
        # that nothing runs: it must not advance last_unseen_at.
        clock[0] += 10
        provider.group_state = "deploying"
        empty = provider.factory

        def factory(selection):
            transport = empty(selection)
            original = transport.send

            def send(action, selection, provider_id, secret):
                result = original(action, selection, provider_id, secret)
                if result.observation:
                    result.observation["instances"] = []
                return result

            transport.send = send
            return transport

        s.transport_factory = factory
        s._observe(assignment)
        assert s._row(b["reservation"])["last_unseen_at"] == unseen
    finally:
        s.close()


def test_total_cap_check_and_insert_hold_the_write_lock_together(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    s = make_service(tmp_path, lambda: clock[0], key, Provider(), total_cap_cents=10_000)
    original = SaladSafetyService._committed
    observed = []

    def committed(self, *, exclude=None, account=True):
        if not account:
            # Inside reserve's transaction: a competing writer must be blocked.
            other = sqlite3.connect(tmp_path / "service.db", timeout=0.05)
            try:
                other.execute("UPDATE reservations SET heartbeat=heartbeat")
                other.commit()
                observed.append("write_allowed")
            except sqlite3.OperationalError as error:
                observed.append(str(error))
            finally:
                other.close()
        return original(self, exclude=exclude, account=account)

    monkeypatch.setattr(SaladSafetyService, "_committed", committed)
    try:
        s.loop()
        assert s.admin(reservation())["state"] == "ready"
        assert observed == ["database is locked"]
    finally:
        s.close()


def test_unix_admin_frame_accepts_a_real_sized_reserve_and_bounds_the_rest():
    import socket as _socket
    import threading as _threading

    from tessera_control.salad_safety_service import UNIX_ADMIN_FRAME_BYTES, read_admin_frame

    def roundtrip(payload):
        a, b = _socket.socketpair()
        try:
            sender = _threading.Thread(target=lambda: (a.sendall(payload), a.close()))
            sender.start()
            try:
                return read_admin_frame(b)
            finally:
                sender.join()
        finally:
            b.close()

    # A workload_v1 reserve with a real pinned manifest is ~40 KiB: over the old 16 KiB cap.
    frame = b'{"operation":"reserve","pad":"' + b"x" * 40_000 + b'"}\n'
    assert bytes(roundtrip(frame)) == frame
    with pytest.raises(ValueError, match="invalid_frame"):
        roundtrip(b"x" * (UNIX_ADMIN_FRAME_BYTES + 1) + b"\n")
    with pytest.raises(ValueError, match="invalid_frame"):
        roundtrip(b'{"no":"newline"}')


# ---- lost create (reservation never bound to a provider group) ---------------------------


def _lost_create(tmp_path):
    """Reserve, then the create fails and the driver cancels: recovery=1, no provider_id."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.group_missing = True
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    s.resolver.expires_at = 100 + 86400
    b = reservation()
    s.loop()
    s.admin(b)
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    s.loop()
    row = s._row(b["reservation"])
    assert row["recovery"] == 1 and row["provider_id"] is None
    return s, b, clock, provider


def _next_reservation(s, clock):
    s.loop()
    return reservation(now=clock[0], operation="b" * 32)


def _absence_reads(provider):
    return provider.calls.count("read_absence")


def test_lost_create_blocks_until_deadline_plus_margin(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _lost_create(tmp_path)
    try:
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reserved"
        assert status["lost_create_eligible_at"] == b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS - 1
        s.loop()
        assert _absence_reads(provider) == 0
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        # The operator path has the same margin, refused before any provider read.
        with pytest.raises(ValueError, match="lost_create_margin_pending"):
            s.admin(
                dict(
                    operation="reconcile",
                    reservation=b["reservation"],
                    attestation="operator: portal shows no group",
                )
            )
        assert _absence_reads(provider) == 0
        assert s._row(b["reservation"])["lost_create_resolution"] is None
    finally:
        s.close()


def test_lost_create_resolves_after_margin_on_definite_absence(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _lost_create(tmp_path)
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert _absence_reads(provider) == 1
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "lost_create_absent"
        assert status["orphan"] is False and status["allocation_uncertain"] is False
        record = status["lost_create"]
        assert record["kind"] == "automatic" and record["attestation"] == "lost_create_absent"
        assert record["name_read"] == "not_found" and record["list_read"] == "absent"
        assert record["margin_seconds"] == LOST_CREATE_MARGIN_SECONDS
        # Accrual stays zero: nothing was ever created or running.
        assert Decimal(status["estimated_exposure_cents"]) == 0
        assert status["actual_billed_cost_cents"] is None and not status["financial_hard_cap"]
        # Custody writers are closed; the event log records the auto-resolution.
        assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
        events = [tuple(r) for r in s.db.execute("SELECT operation,kind FROM events")]
        assert events == [("a" * 32, "lost_create_absent_reconciled")]
        # Terminal for the reservation: it can never be revived or reconciled again.
        with pytest.raises(ValueError, match="reconcile_not_applicable"):
            s.admin(
                dict(operation="reconcile", reservation=b["reservation"], attestation="operator")
            )
        with pytest.raises(ValueError):
            s.admin(dict(operation="ready", reservation=b["reservation"]))
        # The orphan fence now admits a new operation, with zero committed for this one.
        clock[0] += 1
        s.loop()
        nxt = _next_reservation(s, clock)
        s.admin(nxt)
        assert s._committed(exclude=nxt["reservation"]) == 0
        assert s._row(b["reservation"])["accrued_cents"] == "0"
    finally:
        s.close()


@pytest.mark.parametrize("mode", ["unknown", "raise", "present"])
def test_lost_create_never_resolves_on_timeout_5xx_or_presence(tmp_path, mode):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _lost_create(tmp_path)
    provider.absence = mode
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS + 1
        s.loop()
        assert _absence_reads(provider) == 1
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        with pytest.raises(ValueError):
            s.admin(
                dict(
                    operation="reconcile",
                    reservation=b["reservation"],
                    attestation="operator: portal shows no group",
                )
            )
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        assert s.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        s.close()


def test_lost_create_operator_path_needs_a_list_read_that_reached_the_project(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _lost_create(tmp_path)
    # Name read 404s, but the list read failed (e.g. the project itself is missing,
    # which answers the name read with the same body): nothing proves the project.
    provider.list_read = "unavailable"
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS + 1
        s.loop()
        assert _absence_reads(provider) == 1
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        reconcile = dict(
            operation="reconcile",
            reservation=b["reservation"],
            attestation="operator: portal shows no attempt group",
        )
        with pytest.raises(ValueError, match="attestation_required"):
            s.admin(dict(reconcile, attestation=""))
        # A failed list read no longer lets the operator clear it.
        with pytest.raises(ValueError, match="lost_create_absence_unconfirmed"):
            s.admin(reconcile)
        # A listed group contradicts the 404: refused too.
        provider.list_read = "present"
        with pytest.raises(ValueError, match="lost_create_group_listed"):
            s.admin(reconcile)
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        # A reached 100+ item page without the name: not enough for the automatic
        # path, enough for an attested operator reconcile.
        provider.list_read = "absent_partial"
        clock[0] += LOST_CREATE_READ_SECONDS_FOR_TEST
        s.loop()
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        status = s.admin(reconcile)
        assert status["status"] == "reconciled"
        assert status["reason"] == "operator_lost_create_absent"
        record = status["lost_create"]
        digest = hashlib.sha256(reconcile["attestation"].encode()).hexdigest()
        assert record["kind"] == "operator" and record["attestation"] == "sha256:" + digest
        assert record["list_read"] == "absent_partial"
        assert s._row(b["reservation"])["reconciled_attestation"] == digest
        assert Decimal(status["estimated_exposure_cents"]) == 0
        assert [tuple(r) for r in s.db.execute("SELECT kind FROM events")] == [
            ("operator_lost_create_absent_reconciled",)
        ]
        s.admin(_next_reservation(s, clock))
    finally:
        s.close()


LOST_CREATE_READ_SECONDS_FOR_TEST = 16


def test_lost_create_path_never_touches_a_row_with_a_provider_id(service_case):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, request, token, clock, provider, *_ = service_case
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    provider.group_missing = True
    clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS + 10
    s.loop()
    row = s._row(b["reservation"])
    assert row["provider_id"] and row["lost_create_resolution"] is None
    assert _absence_reads(provider) == 0
    with pytest.raises(ValueError, match="reconcile_not_applicable"):
        s._resolve_lost_create(row)
    assert _absence_reads(provider) == 0
    assert s._status(row)["status"] not in {"reconciled"}


def _resolved(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _lost_create(tmp_path)
    clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
    s.loop()
    assert s._status(s._row(b["reservation"]))["status"] == "reconciled"
    return s, b, clock, provider


def _kinds(s):
    return [r[0] for r in s.db.execute("SELECT kind FROM events ORDER BY sequence")]


def test_lost_create_late_group_is_bound_and_stopped_and_fences_again(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        # A group appears under the reserved name after resolution (should not happen).
        provider.absence = "present"
        provider.group_missing = False
        provider.state = "running"
        clock[0] += 61
        s.loop()
        row = s._row(b["reservation"])
        assert row["lost_create_resolution"] is None and row["provider_id"] == GROUP
        assert s._status(row)["status"] != "reconciled"
        assert _kinds(s)[-1] == "lost_create_late_group_observed"
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
    finally:
        s.close()


def test_lost_create_watch_event_is_written_even_when_the_bind_fails(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        provider.absence = "present"  # 200/202 on the exact name, but the bind read fails
        clock[0] += 61
        s.loop()
        row = s._row(b["reservation"])
        assert row["lost_create_resolution"] is None and row["provider_id"] is None
        assert _kinds(s)[-1] == "lost_create_late_group_observed"
        assert s._status(row)["status"] == "reserved"
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
    finally:
        s.close()


@pytest.mark.parametrize("mode", ["unknown", "raise"])
def test_lost_create_watch_unknown_read_voids_until_absence_is_read_again(tmp_path, mode):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        provider.absence = mode
        clock[0] += 61
        s.loop()
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        assert _kinds(s)[-1] == "lost_create_resolution_voided"
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        # Still unknown: stays blocking.
        clock[0] += 16
        s.loop()
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        # A fresh definite absence resolves it again.
        provider.absence = "not_found"
        clock[0] += 16
        s.loop()
        assert s._status(s._row(b["reservation"]))["status"] == "reconciled"
        assert _kinds(s)[-1] == "lost_create_absent_reconciled"
        s.admin(_next_reservation(s, clock))
    finally:
        s.close()


def test_lost_create_watch_keeps_a_definite_absence_resolved(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        reads = _absence_reads(provider)
        clock[0] += 61
        s.loop()
        assert _absence_reads(provider) == reads + 1
        assert s._status(s._row(b["reservation"]))["status"] == "reconciled"
        assert _kinds(s) == ["lost_create_absent_reconciled"]
    finally:
        s.close()


def test_closed_lost_create_row_closes_writers_after_an_interrupted_resolution(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        # Simulate a crash between the durable resolution and closing custody writers.
        with s.custody.db:
            s.custody.db.execute("UPDATE inventories SET closed=0")
        assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 0
        clock[0] += 1
        s.loop()
        assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
    finally:
        s.close()


@pytest.mark.parametrize("column", ["begun", "output", "seen_running", "allocation_uncertain"])
def test_lost_create_release_rechecks_the_row_at_write_time(tmp_path, column):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _lost_create(tmp_path)
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        stale = s._row(b["reservation"])  # passes the in-memory preconditions
        with s.db:
            s.db.execute(
                f"UPDATE reservations SET {column}=1 WHERE reservation=?", (b["reservation"],)
            )
        with pytest.raises(ValueError, match="reconcile_not_applicable"):
            s._resolve_lost_create(stale)
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        assert s.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        # The loop's own path refuses it too.
        s.loop()
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
    finally:
        s.close()


# ---- create rejected (clean provider 400/422; audited early release) -----------------------

REJECTION = dict(
    status=400,
    title="One or more validation errors occurred.",
    detail="storage_amount must be a whole number of GiB",
    error_fields=["container.resources.storage_amount"],
)


def _report(s, b):
    return s.admin(
        dict(operation="create_rejected", reservation=b["reservation"], rejection=REJECTION)
    )


def _ready_reservation(tmp_path):
    """Reserve and admit ``ready`` (the create may now be in flight); nothing reported."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    provider.group_missing = True
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    s.resolver.expires_at = 100 + 86400
    b = reservation()
    s.loop()
    s.admin(b)
    s.admin(dict(operation="ready", reservation=b["reservation"]))
    assert s._row(b["reservation"])["last_ready_at"] == 100.0
    return s, b, clock, provider


def _rejected(tmp_path):
    """Ready at 100, then the trusted server reports a clean 400 for the create."""
    s, b, clock, provider = _ready_reservation(tmp_path)
    status = _report(s, b)
    row = s._row(b["reservation"])
    assert row["recovery"] == 1 and row["provider_id"] is None
    assert status["status"] == "reserved"
    assert status["create_rejection"] == dict(REJECTION, reported_at=100.0)
    return s, b, clock, provider


def test_create_rejected_releases_early_on_the_guardians_own_absence_reads(tmp_path):
    from tessera_control.salad_safety_service import (
        CREATE_REJECTED_SETTLE_SECONDS,
        LOST_CREATE_MARGIN_SECONDS,
    )

    s, b, clock, provider = _rejected(tmp_path)
    try:
        # The report fences at once: no readiness, writers closed, orphan fence holds.
        with pytest.raises(ValueError):
            s.admin(dict(operation="ready", reservation=b["reservation"]))
        assert s.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
        # Within the dispatch settle window nothing is read and it stays blocking.
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS - 1
        s.loop()
        assert _absence_reads(provider) == 0
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        # After it, long before deadline + margin, the guardian's own reads release it.
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS
        assert clock[0] < b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert _absence_reads(provider) == 1
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "create_rejected_absent"
        record = status["lost_create"]
        assert record["kind"] == "create_rejected"
        assert record["attestation"] == "create_rejected_absent"
        assert record["name_read"] == "not_found" and record["list_read"] == "absent"
        assert record["margin_seconds"] == CREATE_REJECTED_SETTLE_SECONDS
        assert record["rejection"] == dict(REJECTION, reported_at=100.0)
        assert Decimal(status["estimated_exposure_cents"]) == 0
        events = [
            (r[0], json.loads(r[1]))
            for r in s.db.execute("SELECT kind,detail FROM events ORDER BY sequence")
        ]
        assert [k for k, _ in events] == [
            "create_rejection_reported",
            "create_rejected_absent_reconciled",
        ]
        assert events[1][1]["rejection"]["detail"] == REJECTION["detail"]
        # The orphan fence reopens; the late-group watch keeps running.
        clock[0] += 1
        s.loop()
        s.admin(_next_reservation(s, clock))
        clock[0] += 61
        s.loop()
        assert _absence_reads(provider) == 2
    finally:
        s.close()


def test_create_rejected_report_during_the_ready_to_post_window_waits_for_the_bound(tmp_path):
    from tessera_control.salad_safety_service import CREATE_REJECTED_SETTLE_SECONDS

    s, b, clock, provider = _ready_reservation(tmp_path)
    try:
        # Reported 5 s after ready: a create admitted by that ready may still be in flight.
        clock[0] = 105.0
        status = _report(s, b)
        assert status["create_rejected_eligible_at"] == 105 + CREATE_REJECTED_SETTLE_SECONDS
        clock[0] = 105 + CREATE_REJECTED_SETTLE_SECONDS - 1
        s.loop()
        assert _absence_reads(provider) == 0
        # The bound runs from max(report, last ready), never from the report alone.
        with s.db:
            s.db.execute("UPDATE reservations SET last_ready_at=?", (120.0,))
        clock[0] = 120 + CREATE_REJECTED_SETTLE_SECONDS - 1
        s.loop()
        assert _absence_reads(provider) == 0
        with pytest.raises(ValueError, match="create_rejection_settle_pending"):
            s._resolve_lost_create(s._row(b["reservation"]), rejected=True)
        clock[0] = 120 + CREATE_REJECTED_SETTLE_SECONDS
        s.loop()
        assert _absence_reads(provider) == 1
        assert s._status(s._row(b["reservation"]))["reason"] == "create_rejected_absent"
    finally:
        s.close()


def test_create_rejected_report_before_any_ready_is_refused_and_fences(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        b = reservation()
        s.loop()
        s.admin(b)
        assert s._row(b["reservation"])["recovery"] == 0
        # No ready admission, so no create could have been sent: not a rejection.
        with pytest.raises(ValueError, match="create_rejection_not_applicable"):
            _report(s, b)
        row = s._row(b["reservation"])
        assert row["create_rejection"] is None and row["recovery"] == 1
    finally:
        s.close()


@pytest.mark.parametrize(
    "absence,list_read",
    [
        ("unknown", "absent"),  # name read timed out / 5xx
        ("raise", "absent"),
        ("present", "absent"),  # the exact name exists
        ("not_found", "unavailable"),  # project list failed: project unproven
        ("not_found", "absent_partial"),  # incomplete page
        ("not_found", "present"),
    ],
)
def test_create_rejected_never_releases_without_both_reads(tmp_path, absence, list_read):
    from tessera_control.salad_safety_service import CREATE_REJECTED_SETTLE_SECONDS

    s, b, clock, provider = _rejected(tmp_path)
    provider.absence, provider.list_read = absence, list_read
    try:
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS + 1
        s.loop()
        assert _absence_reads(provider) == 1
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        assert _kinds(s) == ["create_rejection_reported"]
    finally:
        s.close()


def test_create_rejected_report_for_a_bound_reservation_fences_then_refuses(service_case):
    s, b, request, token, clock, provider, *_ = service_case
    row = s._row(b["reservation"])
    assert row["provider_id"] and row["recovery"] == 0
    with pytest.raises(ValueError, match="create_rejection_not_applicable"):
        _report(s, b)
    row = s._row(b["reservation"])
    assert row["create_rejection"] is None and row["recovery"] == 1
    assert s.guardian.status(b["proposal"]["operation_id"])["reason"] == "cancelled"
    # Even with a forged record, the fast path never touches a bound row.
    with s.db:
        s.db.execute(
            "UPDATE reservations SET create_rejection=?,last_ready_at=0",
            (json.dumps(dict(REJECTION, reported_at=0)),),
        )
    with pytest.raises(ValueError, match="reconcile_not_applicable"):
        s._resolve_lost_create(s._row(b["reservation"]), rejected=True)
    assert _absence_reads(provider) == 0


@pytest.mark.parametrize(
    "rejection",
    [
        None,
        dict(REJECTION, status=409),
        dict(REJECTION, status="400"),
        dict(REJECTION, detail="x" * 201),
        dict(REJECTION, title="line\nbreak"),
        dict(REJECTION, error_fields=["a"] * 17),
        dict(REJECTION, extra="claim"),
        {k: v for k, v in REJECTION.items() if k != "detail"},
    ],
)
def test_create_rejected_report_must_be_a_bounded_summary(tmp_path, rejection):
    s, b, clock, provider = _ready_reservation(tmp_path)
    try:
        with pytest.raises(ValueError, match="invalid_admin_request"):
            s.admin(
                dict(operation="create_rejected", reservation=b["reservation"], rejection=rejection)
            )
        assert s._row(b["reservation"])["create_rejection"] is None
    finally:
        s.close()


# --- Admin freshness at acceptance and closed refusal codes ---------------------------------


def _slow_rate(provider, clock, seconds):
    """Make every rate read take ``seconds`` of service clock (a slow Salad read)."""
    factory = provider.factory

    def slow_factory(selection):
        transport = factory(selection)
        read = transport.gpu_class

        def gpu_class(selection, secret):
            clock[0] += provider.slow
            return read(selection, secret)

        transport.gpu_class = gpu_class
        return transport

    provider.slow = seconds
    provider.factory = slow_factory


def _running_reservation(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    body = reservation(seconds=300, workload_seconds=60)
    s.loop()
    _bind_and_run(s, provider, body)
    provider.state = "running"
    s.loop()
    _slow_rate(provider, clock, 0)
    s.transport_factory = lambda selection: provider.factory(selection)
    return s, clock, provider, body


def test_heartbeat_with_slow_budget_refresh_is_judged_at_acceptance(tmp_path):
    s, clock, provider, body = _running_reservation(tmp_path)
    try:
        heartbeat = dict(operation="heartbeat", reservation=body["reservation"])
        provider.slow = 3
        clock[0] += 1
        s.loop()
        accepted = clock[0]
        # The rate refresh takes 3 s; the loop was fresh when the request was accepted.
        reply = s.admin(dict(heartbeat), accepted_at=accepted)
        assert clock[0] - accepted >= 3
        assert reply == dict(reservation=body["reservation"], state="ready", observed_at=s.loop_at)
        # Direct callers default to acceptance at the start of admin().
        clock[0] += 1
        s.loop()
        assert s.admin(dict(heartbeat))["state"] == "ready"
        row = s._row(body["reservation"])
        assert not row["recovery"] and not row["budget_fenced"] and not row["rate_unknown"]
    finally:
        s.close()


def _fast_released(tmp_path):
    from tessera_control.salad_safety_service import CREATE_REJECTED_SETTLE_SECONDS

    s, b, clock, provider = _rejected(tmp_path)
    clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS
    s.loop()
    assert s._status(s._row(b["reservation"]))["reason"] == "create_rejected_absent"
    return s, b, clock, provider


@pytest.mark.parametrize("mode", ["unknown", "raise"])
def test_unknown_watch_read_reblocks_but_keeps_the_fast_path(tmp_path, mode):
    s, b, clock, provider = _fast_released(tmp_path)
    try:
        provider.absence = mode
        clock[0] += 61
        s.loop()
        row = s._row(b["reservation"])
        assert row["lost_create_resolution"] is None
        assert row["create_rejection"] is not None and row["create_rejection_voided"] == 0
        assert _kinds(s)[-1] == "lost_create_resolution_voided"
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        # A fresh definite absence resolves it again through the fast path, long
        # before deadline + margin.
        provider.absence = "not_found"
        clock[0] += 16
        s.loop()
        status = s._status(s._row(b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "create_rejected_absent"
        assert _kinds(s)[-1] == "create_rejected_absent_reconciled"
    finally:
        s.close()


def test_heartbeat_with_stale_loop_at_acceptance_is_refused(tmp_path):
    s, clock, provider, body = _running_reservation(tmp_path)
    try:
        heartbeat = dict(operation="heartbeat", reservation=body["reservation"])
        clock[0] += 3  # no loop pass for 3 s before this request is accepted
        with pytest.raises(ValueError, match="^guardian_loop_unready$"):
            s.admin(dict(heartbeat), accepted_at=clock[0])
        s.loop()
        for bad in (clock[0] + 1, float("nan"), "now"):
            with pytest.raises(ValueError, match="^guardian_loop_unready$"):
                s.admin(dict(heartbeat), accepted_at=bad)
        # The HTTPS listener passes NaN without a stamp: liveness actions are refused,
        # but status/cancel, which need no fresh loop, still work.
        status = dict(operation="status", reservation=body["reservation"])
        assert s.admin(status, accepted_at=float("nan"))["status"]
        # Accepted before the last loop pass: not evidence of a live loop at acceptance.
        with pytest.raises(ValueError, match="^guardian_loop_unready$"):
            s.admin(dict(heartbeat), accepted_at=s.loop_at - 1)
        # Work beyond the admin window is refused even if no alarm fired.
        provider.slow = 13
        s.loop()
        with pytest.raises(ValueError, match="^admin_deadline_exceeded$"):
            s.admin(dict(heartbeat), accepted_at=clock[0])
    finally:
        s.close()


def test_present_watch_read_voids_the_fast_path_for_good(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, provider = _fast_released(tmp_path)
    try:
        # A group is seen under the name (its bind read still fails here).
        provider.absence = "present"
        clock[0] += 61
        s.loop()
        row = s._row(b["reservation"])
        assert row["lost_create_resolution"] is None and row["create_rejection"] is None
        assert row["create_rejection_voided"] == 1
        assert _kinds(s)[-1] == "lost_create_late_group_observed"
        # A second report is refused (and only fences); absence alone does not resolve.
        with pytest.raises(ValueError, match="create_rejection_not_applicable"):
            _report(s, b)
        assert s._row(b["reservation"])["create_rejection"] is None
        provider.absence = "not_found"
        clock[0] += 16
        s.loop()
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        with pytest.raises(ValueError, match="reconcile_not_applicable"):
            s._resolve_lost_create(s._row(b["reservation"]), rejected=True)
        # Only the ordinary deadline + margin path can resolve it again.
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        status = s._status(s._row(b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "lost_create_absent"
    finally:
        s.close()


def test_refusal_code_is_closed():
    from tessera_control.salad_safety_service import refusal_code

    assert refusal_code(ValueError("guardian_loop_unready")) == "guardian_loop_unready"
    assert refusal_code(ValueError("budget_binding_expired")) == "budget_binding_expired"
    for error in (
        ValueError("/home/user/secret path"),
        ValueError("Upper"),
        ValueError("a" * 65),
        ValueError("code", "extra"),
        ValueError(42),
        ValueError(""),
        RuntimeError("guardian_loop_unready"),
        KeyError("operation"),
        json.JSONDecodeError("Expecting value", "doc", 0),
    ):
        assert refusal_code(error) == "internal"
    assert refusal_code(ValueError("a" * 64)) == "a" * 64
    # A request alarm or provider deadline is a fixed closed code, never its message.
    for error in (TimeoutError("service_deadline"), TimeoutError("/home/user/secret path")):
        assert refusal_code(error) == "service_deadline"


def test_unix_admin_socket_replies_closed_codes_and_passes_acceptance(tmp_path):
    import tempfile
    import threading

    from tessera_control.salad_safety_service import serve

    short = Path(tempfile.mkdtemp(prefix="tg", dir="/tmp"))  # AF_UNIX path length limit
    short.chmod(0o700)
    path = short / "g.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    listener.settimeout(0.01)
    seen = []
    errors = {
        "loop": ValueError("guardian_loop_unready"),
        "path": ValueError("unsafe file /home/user/.config/credential 1234"),
        "runtime": RuntimeError("guardian_loop_unready"),
    }

    class Service:
        def __init__(self):
            self.ticks = 0.0

        def clock(self):
            self.ticks += 1
            return self.ticks

        def loop(self):
            pass

        def admin(self, body, accepted_at=None):
            seen.append((accepted_at, self.ticks))
            if body["case"] == "ok":
                return dict(reservation="r", state="ready", observed_at=1)
            raise errors[body["case"]]

    class NoHTTP:
        def handle_request(self):
            pass

    stop = threading.Event()
    replies = {}

    def ask(case):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(path))
            client.sendall(json.dumps({"case": case}).encode() + b"\n")
            return json.loads(client.recv(4096))

    def clients():
        try:
            for case in ("ok", "loop", "path", "runtime"):
                replies[case] = ask(case)
        finally:
            stop.set()

    # serve() owns SIGALRM, so it runs on the main thread and the client on a helper.
    thread = threading.Thread(target=clients, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        serve(
            Service(),
            NoHTTP(),
            shutdown=lambda: stop.is_set() or time.monotonic() > deadline,
            unix_listener=listener,
            proxy_uid=os.getuid(),
        )
        thread.join(5)
        assert replies["ok"] == dict(reservation="r", state="ready", observed_at=1)
        assert replies["loop"] == {"error": "request_rejected", "code": "guardian_loop_unready"}
        assert replies["path"] == {"error": "request_rejected", "code": "internal"}
        assert replies["runtime"] == {"error": "request_rejected", "code": "internal"}
        # Acceptance is stamped by serve() from the service clock before admin() runs.
        assert len(seen) == 4
        assert all(type(accepted) is float and accepted <= now for accepted, now in seen)
    finally:
        listener.close()
        path.unlink(missing_ok=True)
        short.rmdir()


def test_a_create_that_was_never_sent_is_released_on_the_same_absence_reads(tmp_path):
    """Status 0 (no HTTP exchange): reported like a clean rejection, fenced at once and
    released only after the settle bound and the guardian's own absence reads, long
    before the route deadline plus the lost-create margin."""
    from tessera_control.salad_safety_service import (
        CREATE_REJECTED_SETTLE_SECONDS,
        LOST_CREATE_MARGIN_SECONDS,
    )

    s, b, clock, provider = _ready_reservation(tmp_path)
    try:
        not_sent = dict(status=0, title="The create request was not sent", detail=None,
                        error_fields=[])  # fmt: skip
        s.admin(dict(operation="create_rejected", reservation=b["reservation"],
                     rejection=not_sent))  # fmt: skip
        with pytest.raises(ValueError):
            s.admin(dict(operation="ready", reservation=b["reservation"]))
        clock[0] += CREATE_REJECTED_SETTLE_SECONDS
        s.loop()
        assert clock[0] < b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        status = s._status(s._row(b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "create_rejected_absent"
        events = dict(s.db.execute(
            "SELECT kind, detail FROM events WHERE kind='create_rejection_reported'"
        ).fetchall())  # fmt: skip
        assert json.loads(events["create_rejection_reported"])["status"] == 0
    finally:
        s.close()


@pytest.mark.parametrize("status", [1, 200, 404, -1])
def test_only_status_0_400_and_422_are_accepted_as_create_reports(tmp_path, status):
    s, b, clock, provider = _ready_reservation(tmp_path)
    try:
        bad = dict(status=status, title="x", detail=None, error_fields=[])
        with pytest.raises(ValueError, match="invalid_admin_request"):
            s.admin(dict(operation="create_rejected", reservation=b["reservation"],
                         rejection=bad))  # fmt: skip
    finally:
        s.close()


def test_unclosed_refusals_journal_only_the_exception_class(capsys):
    from tessera_control.salad_safety_service import log_unclosed_refusal

    log_unclosed_refusal(KeyError("/home/user/secret path"), "admin")
    log_unclosed_refusal(TimeoutError("service_deadline at /srv/x"), "https")
    log_unclosed_refusal(ValueError("guardian_loop_unready"), "admin")  # closed: no line
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "guardian admin refusal: KeyError",
        "guardian https refusal: TimeoutError",
    ]
    assert "secret" not in err and "/srv" not in err
