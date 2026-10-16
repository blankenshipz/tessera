"""Isolated server composition tests; synthetic HTTP/IPC, never live acceptance."""

import copy
import hashlib
import http.client
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_kubernetes_execution import _admitted_package
from test_program_interactions import ProgramJourney
from test_remote_execution_routing import command, dispatch
from test_salad_http import GPU, ID, Factory, gpu_class, group
from test_salad_proxy import declaration, profile

from tessera_control.capabilities import CapabilityStore
from tessera_control.remote_execution import RemoteExecutionStore
from tessera_control.remote_execution_models import ExecutionSelection, TargetPolicy
from tessera_control.salad_proxy_contract import ManagementCredential, ProxyPolicy
from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer
from tessera_control.salad_server import (
    SaladSafetyClient,
    SaladSafetyHTTPSClient,
    TrustedSaladServer,
)
from tessera_control.store import Conflict, Forbidden, Store, canonical_digest


def resolver(tmp_path):
    directory = tmp_path / "private-credentials"
    directory.mkdir(mode=0o700)
    backend = PrivateFileSecretResolver(directory)
    backend.enroll(
        "credential-ref:fixture",
        1,
        SecretBuffer(bytearray(b"isolated-marker")),
        expires_at=time.time() + 300,
    )
    return backend


def fixture(tmp_path, monkeypatch, *, capability=True, safety=None, smoke=False):
    execution = profile()
    if smoke:
        execution["argv_binding"] = "smoke_operation_v1"
        execution["max_cost_cents"] = 10
        execution["argv"] = [
            "v1",
            "public-integers-v1",
            "operation-id",
            "operation-epoch",
            "localhost",
            execution["watchdog_profile"],
            "60",
            "1024",
            "1",
            "4",
        ]
    if smoke:
        revise = Store.revise_task_plan

        def submit_reviewed_output_plan(store, actor, task, key, digest, payload):
            payload = copy.deepcopy(payload)
            payload["execution_spec"]["output_paths"] = ["output.json"]
            return revise(store, actor, task, key, canonical_digest(payload), payload)

        with monkeypatch.context() as submission:
            submission.setattr(Store, "revise_task_plan", submit_reviewed_output_plan)
            response, worker, _ = _admitted_package(
                tmp_path, argv=[execution["command"], *execution["argv"]]
            )
    else:
        response, worker, _ = _admitted_package(
            tmp_path, argv=[execution["command"], *execution["argv"]]
        )
    package = response["package"]
    if smoke:
        execution["argv"][2:4] = [package["operation_id"], str(package["epoch"])]
    journey = ProgramJourney(
        tmp_path,
        execution=SimpleNamespace(
            state=tmp_path / "state",
            project={"id": package["project_id"]},
            experiment={"id": package["experiment"]["experiment_id"]},
        ),
    )
    journey.assess()
    plan = journey.scope()
    plan["brief"]["compute"]["target"] = "salad_container"
    program = journey.resolve(journey.propose(plan))
    source = journey.resolve(journey.propose(journey.scope("source_use")))
    selection = ExecutionSelection.model_validate(
        dict(
            target="salad_container",
            target_digest="f" * 64,
            resources=dict(
                cpu_millicores=1000,
                memory_mb=1024,
                gpu_class=GPU,
                gpu_count=1,
                disk_mb=1024,
                max_seconds=60,
            ),
            manifests=dict(
                image=package["pinned_image"],
                data=package["snapshot_digest"],
                code="c" * 64,
                model="d" * 64,
                environment="e" * 64,
            ),
            salad=dict(
                organization="fixture-org",
                project="fixture-project",
                group="attempt-" + package["operation_id"],
                country="US",
                provider_trust="broad_egress_ephemeral_reallocation_credit_not_hard_cap",
                execution=execution,
            ),
        )
    )
    policy = ProxyPolicy(
        org_id=worker["org_id"],
        project_id=package["project_id"],
        target=TargetPolicy(selection=selection),
        management=ManagementCredential(reference="credential-ref:fixture", version=1),
    )
    calls = []
    if safety is None:
        safety = SaladSafetyClient("/not-contacted/guardian.sock", service_uid=os.geteuid())
        monkeypatch.setattr(safety, "_exchange", lambda body: calls.append(body))
    server = TrustedSaladServer(
        state_dir=journey.state,
        worker=worker,
        policy=policy,
        resolver=resolver(tmp_path),
        safety=safety,
    )
    body = dict(
        operation_id=package["operation_id"],
        package_digest=response["package_digest"],
        attempt_id=response["admitted_attempt_id"],
        plan_digest=package["plan_digest"],
        lease_id=package["lease_id"],
        epoch=package["epoch"],
        selection=selection.model_dump(mode="json"),
        study_id=journey.study["id"],
        program_review_id=program["id"],
        source_review_id=source["id"],
    )
    route = server.store.propose(journey.actor, package["task_id"], "route", body)[1]
    h = SimpleNamespace(
        server=server,
        store=server.store,
        worker=worker,
        journey=journey,
        route=route,
        body=body,
        safety_calls=calls,
    )
    command(h, "approve")
    h.route = command(h, "start")
    h.dispatcher = server.dispatcher(route["id"])
    if capability:
        store = CapabilityStore(journey.state)
        task = store.get_task(journey.actor, route["task_id"])
        _, cap = store.request(
            journey.actor,
            task["id"],
            "cap",
            dict(
                expected_plan_revision=task["revision"] - 1,
                expected_plan_digest=body["plan_digest"],
                declaration=declaration().model_dump(mode="json"),
            ),
        )
        store.approve(
            journey.actor,
            cap["id"],
            "cap-approve",
            dict(
                expected_revision=1,
                declaration_digest=cap["declaration_digest"],
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            ),
        )
    provider_group = group()
    provider_group.update(
        name=selection.salad.group, organization_name="fixture-org", project_name="fixture-project"
    )
    provider_group["container"] = copy.deepcopy(server.transport._spec["container"])
    h.provider_group = provider_group
    return h


def wire(h, *responses):
    factory = Factory(*responses)
    h.server.transport._factory = factory
    return factory


def preflight(h, *, absent):
    return [
        (200, {"items": [] if absent else [h.provider_group]}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": 2,
                    "container_replicas_used": 0 if absent else 1,
                }
            },
        ),
        (200, {"items": [gpu_class()]}),
        (200, {"available_gpu_high": 1}),
    ]


def test_private_backend_rotation_revocation_expiry_and_zeroization(tmp_path):
    backend = resolver(tmp_path)
    metadata = backend.metadata("credential-ref:fixture")
    assert metadata.version == 1 and not metadata.revoked
    buffer = backend.resolve("credential-ref:fixture", 1)
    with buffer:
        assert buffer.contains_in(b"isolated-marker")
    assert not any(buffer._buffer)
    backend.enroll(
        "credential-ref:fixture",
        2,
        SecretBuffer(bytearray(b"replacement-marker")),
        expires_at=time.time() + 60,
    )
    with pytest.raises(ValueError):
        backend.resolve("credential-ref:fixture", 1)
    backend.revoke("credential-ref:fixture")
    assert backend.metadata("credential-ref:fixture").revoked
    with pytest.raises(ValueError):
        backend.resolve("credential-ref:fixture", 2)
    assert "marker" not in repr(backend)
    backend.close()


def test_private_backend_rejects_symlink_permissions_and_hardlink(tmp_path):
    backend = resolver(tmp_path)
    root = tmp_path / "private-credentials"
    os.chmod(root, 0o755)
    with pytest.raises(ValueError):
        backend.metadata("credential-ref:fixture")
    os.chmod(root, 0o700)
    os.link(root / "fixture.credential", root / "linked")
    with pytest.raises(ValueError):
        backend.resolve("credential-ref:fixture", 1)
    link = tmp_path / "symlink"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        PrivateFileSecretResolver(link)
    backend.close()


def test_real_wire_composition_one_shot_pending_stop_and_reconnect(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    factory = wire(h, *preflight(h, absent=True), (201, h.provider_group))
    created = dispatch(h, "create")
    assert created["provider_id"] == ID and created["termination_confirmed"]
    assert h.store.get(h.worker, created["id"])["authority_status"] == "current_trusted_server_only"
    assert created["receipts"][-1]["provenance"] == "provider"
    assert [b["operation"] for b in h.safety_calls] == ["reserve", "ready", "bind"]
    assert factory.calls[-1][0] == "POST"
    factory = wire(h, *preflight(h, absent=False), (200, h.provider_group), (202, b""))
    assert dispatch(h, "start")["receipts"][-1]["response_status"] == 202
    assert not h.route["termination_confirmed"]
    with pytest.raises(Conflict):
        dispatch(h, "start")
    command(h, "cancel")
    wire(h, (200, h.provider_group), (200, h.provider_group), (202, b""))
    assert dispatch(h, "stop")["cancellation"] == "pending"
    wire(h, (200, h.provider_group), (200, h.provider_group), (200, {"instances": []}))
    stopped = dispatch(h, "observe")
    assert stopped["termination_confirmed"] and stopped["cancellation"] == "confirmed"
    assert stopped["cost_cents"] is None
    # Restart the composition against retained state. No automatic effect occurs.
    restarted = TrustedSaladServer(
        state_dir=h.journey.state,
        worker=h.worker,
        policy=h.server.policy,
        resolver=h.server.resolver,
        safety=h.server.safety,
    )
    reconnect = restarted.store.get(h.worker, h.route["id"])
    assert reconnect["provider_id"] == ID
    assert len(reconnect["effects"]) == 4
    assert reconnect["receipts"] == stopped["receipts"]
    with pytest.raises(Conflict):
        restarted.dispatcher(h.route["id"]).dispatch(h.route["id"], "create")


@pytest.mark.parametrize(
    "field,value",
    [
        ("replicas", 2),
        ("restart_policy", "always"),
        ("country_codes", ["ca"]),
        ("priority", "low"),
    ],
)
def test_rev001_dispatcher_stop_after_same_uuid_mutable_drift(tmp_path, monkeypatch, field, value):
    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    command(h, "cancel")
    drifted = copy.deepcopy(h.provider_group)
    drifted[field] = value
    factory = wire(h, (200, drifted), (200, drifted), (202, b""))
    # Actual public dispatcher journey, not a direct transport/guardian call.
    state = h.dispatcher.dispatch(h.route["id"], "stop")
    persisted = h.store.get(h.worker, h.route["id"])
    effect = persisted["effects"][-1]
    assert effect["action"] == "stop"
    assert state["receipts"][-1]["effect_id"] == effect["id"]
    assert state["receipts"][-1]["response_status"] == 202
    assert state["cancellation"] == "pending" and not state["termination_confirmed"]
    assert [call[0] for call in factory.calls] == ["GET", "GET", "POST"]
    assert factory.calls[-1][1].endswith("/" + h.body["selection"]["salad"]["group"] + "/stop")
    with sqlite3.connect(h.store.database_path) as db:
        claims = db.execute(
            "SELECT payload_json FROM events WHERE event_type='salad_proxy.claim' "
            "AND json_extract(payload_json,'$.action')='stop'"
        ).fetchall()
    assert len(claims) == 1
    assert json.loads(claims[0][0])["nonce"] == effect["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", GPU),
        ("name", "foreign-group"),
        ("organization_name", "foreign-org"),
        ("project_name", "foreign-project"),
    ],
)
def test_rev001_dispatcher_stop_rejects_foreign_identity_before_effect(
    tmp_path, monkeypatch, field, value
):
    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    command(h, "cancel")
    foreign = copy.deepcopy(h.provider_group)
    foreign[field] = value
    factory = wire(h, (200, foreign))
    with pytest.raises(Conflict, match="Fresh provider preflight unavailable"):
        h.dispatcher.dispatch(h.route["id"], "stop")
    state = h.store.get(h.worker, h.route["id"])
    assert not any(effect["action"] == "stop" for effect in state["effects"])
    assert [call[0] for call in factory.calls] == ["GET"]


@pytest.mark.parametrize("action", ["start", "observe"])
def test_rev001_dispatcher_execution_reads_stay_strict_under_drift(tmp_path, monkeypatch, action):
    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    h.provider_group["replicas"] = 2
    factory = wire(
        h, *(preflight(h, absent=False) if action == "start" else [(200, h.provider_group)])
    )
    with pytest.raises(Conflict, match="Fresh provider preflight unavailable"):
        h.dispatcher.dispatch(h.route["id"], action)
    assert [call[0] for call in factory.calls] == ["GET"]
    assert [effect["action"] for effect in h.store.get(h.worker, h.route["id"])["effects"]] == [
        "create"
    ]


def test_rev001_dispatcher_rechecks_uuid_before_stop_send(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    command(h, "cancel")
    foreign = copy.deepcopy(h.provider_group)
    foreign["id"] = GPU
    factory = wire(h, (200, h.provider_group), (200, foreign))
    state = h.dispatcher.dispatch(h.route["id"], "stop")
    assert state["effects"][-1]["action"] == "stop"
    assert state["receipts"][-1]["proxy_reason"] == "identity_mismatch"
    assert state["cancellation"] != "confirmed"
    assert [call[0] for call in factory.calls] == ["GET", "GET"]


@pytest.mark.parametrize("fence", ["revoked", "version", "expired"])
def test_rev001_dispatcher_stop_keeps_credential_fences(tmp_path, monkeypatch, fence):
    import tessera_control.salad_server as module

    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    command(h, "cancel")
    if fence == "revoked":
        h.server.resolver.revoke("credential-ref:fixture")
    elif fence == "version":
        h.server.resolver.enroll(
            "credential-ref:fixture",
            2,
            SecretBuffer(bytearray(b"isolated-replacement")),
            expires_at=time.time() + 300,
        )
    else:
        future = time.time() + 400
        monkeypatch.setattr(module.time, "time", lambda: future)
    factory = wire(h)
    with pytest.raises(ValueError, match="credential unavailable"):
        h.dispatcher.dispatch(h.route["id"], "stop")
    assert not factory.calls
    assert not any(
        effect["action"] == "stop" for effect in h.store.get(h.worker, h.route["id"])["effects"]
    )


def test_lost_create_reconciles_reserved_name_never_relaunches(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), TimeoutError("not-retained"))
    assert dispatch(h, "create")["state"] == "unknown"
    wire(h, (200, h.provider_group), (200, h.provider_group))
    assert dispatch(h, "observe")["provider_id"] == ID
    wire(h, *preflight(h, absent=False))
    with pytest.raises(Conflict):
        dispatch(h, "create")
    with sqlite3.connect(h.store.database_path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='salad_proxy.claim' "
                "AND json_extract(payload_json,'$.action')='create'"
            ).fetchone()[0]
            == 1
        )


def test_admission_before_credential_resolution_and_safety_outage_before_mutation(
    tmp_path, monkeypatch
):
    h = fixture(tmp_path, monkeypatch, capability=False)
    monkeypatch.setattr(h.server.resolver, "resolve", lambda *a: pytest.fail("secret resolution"))
    with pytest.raises(Conflict):
        dispatch(h, "create")
    assert not h.safety_calls


def test_independent_service_unavailable_never_launches(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        h.server.safety, "_exchange", lambda body: (_ for _ in ()).throw(ValueError("unavailable"))
    )
    factory = wire(h)
    with pytest.raises(ValueError):
        dispatch(h, "create")
    assert not factory.calls


def test_default_store_cannot_accept_provider_provenance(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    state = dispatch(h, "create")
    default = RemoteExecutionStore(h.journey.state, policies=[h.server.policy.target])
    effect = default.begin(h.worker, state["id"], "observe")
    body = state["receipts"][-1] | {"observed_at": datetime.now(UTC).isoformat()}
    for key in ("effect_id", "target", "route_digest"):
        body.pop(key)
    with pytest.raises(Forbidden):
        default.record(h.worker, state["id"], effect["effects"][-1]["id"], body)


def test_request_cannot_substitute_resolver_or_safety(tmp_path):
    with pytest.raises(ValueError):
        TrustedSaladServer(
            state_dir=tmp_path, worker={}, policy=None, resolver=object(), safety=object()
        )


def test_ready_loss_after_claim_consumes_nonce_without_mutation(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    exchange = h.server.safety._exchange

    def service(body):
        if body["operation"] == "ready":
            raise ValueError("not retained")
        exchange(body)

    monkeypatch.setattr(h.server.safety, "_exchange", service)
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["state"] == "unknown"
    assert state["receipts"][-1]["proxy_reason"] == "provider_unavailable"
    assert all(
        path.endswith(("/containers", "/quotas", "/gpu-classes", "/sce-gpu-availability"))
        for _, path, _, _ in factory.calls
    )
    assert not any(
        method == "POST" and path.endswith("/containers") for method, path, _, _ in factory.calls
    )
    with pytest.raises(Conflict):
        dispatch(h, "create")


def test_revocation_during_effect_resolution_prevents_provider_mutation(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    resolve = h.server.resolver.resolve
    count = 0

    def resolve_then_revoke(reference, version):
        nonlocal count
        count += 1
        secret = resolve(reference, version)
        if count == 2:
            h.server.resolver.revoke(reference)
        return secret

    monkeypatch.setattr(h.server.resolver, "resolve", resolve_then_revoke)
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "credential_unavailable"
    assert len(factory.calls) == 4
    assert state["state"] == "unknown"


def test_authority_rechecked_after_effect_resolution(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    resolve = h.server.resolver.resolve
    count = 0

    def resolve_then_fence(reference, version):
        nonlocal count
        count += 1
        secret = resolve(reference, version)
        if count == 2:
            with sqlite3.connect(h.store.database_path) as db:
                db.execute("UPDATE capability_policies SET status='revoked'")
        return secret

    monkeypatch.setattr(h.server.resolver, "resolve", resolve_then_fence)
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "admission_denied"
    assert len(factory.calls) == 4


def test_expired_credentials_and_symlink_record_fail_closed(tmp_path):
    backend = resolver(tmp_path)
    backend._now = lambda: time.time() + 1000
    with pytest.raises(ValueError):
        backend.resolve("credential-ref:fixture", 1)
    backend._now = time.time
    root = tmp_path / "private-credentials"
    original = root / "fixture.credential"
    original.rename(root / "old")
    original.symlink_to(root / "old")
    with pytest.raises(OSError):
        backend.resolve("credential-ref:fixture", 1)
    backend.close()


def test_control_heartbeat_never_renews_fenced_authority(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    with pytest.raises(Conflict):
        h.server.heartbeat(h.route["id"])
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    state = dispatch(h, "create")
    h.server.heartbeat(state["id"])
    assert h.safety_calls[-1]["operation"] == "heartbeat"
    assert len(h.store.get(h.worker, state["id"])["effects"]) == 1
    command(h, "cancel")
    with pytest.raises((Conflict, Forbidden)):
        h.server.heartbeat(state["id"])
    assert h.safety_calls[-1]["operation"] == "cancel"


class LocalSaladHTTP(BaseHTTPRequestHandler):
    """Loopback-only wire fixture. Management effects are synthetic HTTP only."""

    def log_message(self, *unused):
        pass

    def _reply(self, status, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        owner = self.server
        if self.path.endswith("/gpu-classes"):
            self._reply(200, {"items": [gpu_class()]})
        elif self.path.endswith("/quotas"):
            self._reply(
                200,
                {
                    "container_groups_quotas": {
                        "container_replicas_quota": 2,
                        "container_replicas_used": 0 if owner.group is None else 1,
                    }
                },
            )
        elif self.path.endswith("/containers"):
            self._reply(200, {"items": [] if owner.group is None else [owner.group]})
        elif owner.group is None:
            self._reply(404)
        elif self.path.endswith("/instances"):
            self._reply(
                200,
                {
                    "instances": []
                    if owner.group["current_state"]["status"] == "stopped"
                    else [
                        dict(
                            id=str(uuid.UUID(hex="d" * 32)),
                            machine_id=str(uuid.UUID(hex="c" * 32)),
                            version=1,
                            state=owner.phase,
                        )
                    ]
                },
            )
        else:
            self._reply(200, owner.group)

    def do_POST(self):
        owner = self.server
        length = int(self.headers.get("Content-Length", "0"))
        assert 0 <= length <= 16384
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path.endswith("/sce-gpu-availability"):
            self._reply(200, {"available_gpu_high": 1})
        elif self.path == "/fixture/phase":
            owner.phase = body["phase"]
            self._reply(200, {})
        elif self.path.endswith("/containers"):
            assert owner.group is None
            value = group()
            value.update(body)
            value["organization_name"] = "fixture-org"
            value["project_name"] = "fixture-project"
            value["priority"] = "high"
            owner.group = value
            owner.events.append("create")
            self._reply(201, value)
        elif self.path.endswith(("/start", "/stop")):
            action = self.path.rsplit("/", 1)[1]
            owner.events.append(action)
            owner.group["current_state"]["status"] = "running" if action == "start" else "stopped"
            owner.group["current_state"]["instance_status_counts"]["running_count"] = int(
                action == "start"
            )
            self._reply(202)
        else:
            self._reply(404)
        (owner.root / "provider-events.json").write_text(json.dumps(owner.events))


def independent_journey_service(root):
    """Executed in a distinct process; only provider HTTP and signer are synthetic."""
    from cryptography.hazmat.primitives import serialization
    from test_salad_workload_jwt import ORG, key_document

    from tessera_control.salad_http import SaladHTTPTransport
    from tessera_control.salad_safety_service import SafetyHTTPServer, SaladSafetyService, serve
    from tessera_control.salad_workload_jwt import SaladWorkloadJWT

    provider = ThreadingHTTPServer(("127.0.0.1", 0), LocalSaladHTTP)
    provider.group, provider.phase, provider.events, provider.root = None, "creating", [], root
    provider.daemon_threads = True
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()

    def connections(host, *, port, timeout):
        assert (host, port) == ("api.salad.com", 443)
        return http.client.HTTPConnection("127.0.0.1", provider.server_port, timeout=timeout)

    signing = serialization.load_pem_private_key((root / "key.pem").read_bytes(), password=None)
    verifier = SaladWorkloadJWT()
    verifier.load(key_document(signing), observed_at=time.time())
    backend = resolver(root)
    (root / "custody").mkdir(mode=0o700)
    service = SaladSafetyService(
        root / "custody",
        organization_ids={"fixture-org": ORG},
        resolver=backend,
        reference="credential-ref:fixture",
        version=1,
        destination="localhost",
        verifier=verifier,
        transport_factory=lambda selection: SaladHTTPTransport(
            selection, connection_factory=connections
        ),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    server = SafetyHTTPServer(
        ("127.0.0.1", 0), service, (root / "admin-token").read_bytes(), context=context
    )
    (root / "ports.json").write_text(json.dumps([server.server_port, provider.server_port]))
    try:
        serve(service, server, shutdown=lambda: (root / "shutdown").exists())
    finally:
        server.server_close()
        service.close()
        backend.close()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=3)


def test_real_independent_tls_service_custody_stop_reconnect(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from test_salad_safety_service import provision_tls
    from test_salad_workload_jwt import claims, signed

    from tessera_control.salad_safety_service import SaladSafetyService, encoded

    root = tmp_path / "independent-service"
    root.mkdir(mode=0o700)
    pin = provision_tls(root)
    script = (
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[2]); "
        "from test_salad_server import independent_journey_service; "
        "independent_journey_service(Path(sys.argv[1]))"
    )
    with (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(root), str(Path(__file__).parent)],
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not (root / "ports.json").exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert (root / "ports.json").exists(), "independent service did not become ready"
            safety_port, provider_port = json.loads((root / "ports.json").read_text())
            safety = SaladSafetyHTTPSClient(
                "localhost",
                safety_port,
                token_file=root / "admin-token",
                ca_file=root / "cert.pem",
                certificate_sha256=pin,
            )
            h = fixture(tmp_path, monkeypatch, safety=safety, smoke=True)

            def connections(host, *, port, timeout):
                assert (host, port) == ("api.salad.com", 443)
                return http.client.HTTPConnection("127.0.0.1", provider_port, timeout=timeout)

            h.server.transport._factory = connections
            from tessera_control.remote_execution_models import RemoteProposal

            admission = h.store.preflight_authority(
                h.worker,
                h.route["id"],
                RemoteProposal.model_validate(h.route["proposal"]),
                "create",
            )
            assert admission["runtime_package"]["plan"]["execution_spec"]["output_paths"] == [
                "output.json"
            ]
            for change in ("placeholder", "budget", "output-path", "digest"):
                bad = dict(
                    operation="reserve",
                    route_id=h.route["id"],
                    org_id=admission["org_id"],
                    project_id=admission["project_id"],
                    proposal=copy.deepcopy(admission["proposal"]),
                    routing_package=copy.deepcopy(admission["routing_package"]),
                    runtime_package=copy.deepcopy(admission["runtime_package"]),
                    deadline=admission["guardian_deadline"],
                )
                runtime = bad["runtime_package"]
                if change == "placeholder":
                    runtime["plan"]["execution_spec"]["argv"][3] = "effect-id"
                elif change == "budget":
                    runtime["plan"]["budget_cents"] = 0
                elif change == "output-path":
                    runtime["plan"]["execution_spec"]["output_paths"] = ["unexpected.txt"]
                runtime["plan_digest"] = canonical_digest(runtime["plan"])
                bad["proposal"]["plan_digest"] = runtime["plan_digest"]
                bad["proposal"]["package_digest"] = (
                    "0" * 64 if change == "digest" else canonical_digest(runtime)
                )
                bad["routing_package"]["proposal"] = bad["proposal"]
                bad["route_digest"] = canonical_digest(bad["routing_package"])
                bad["reservation"] = canonical_digest([bad["route_id"], bad["route_digest"]])
                with pytest.raises(ValueError):
                    safety._call(bad)
            with sqlite3.connect(root / "custody" / "service.db") as database:
                assert database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
            state = dispatch(h, "create")
            assert state["provider_id"] == ID and state["termination_confirmed"]
            time.sleep(0.25)  # The real loop must not seal custody on stopped creation.
            assert safety.status(h.store.get(h.worker, state["id"]))["status"] == "armed"
            assert dispatch(h, "start")["receipts"][-1]["response_status"] == 202
            first = dispatch(h, "observe")
            assert first["allocation_id"] == canonical_digest(
                {"instance_id": "d" * 32, "machine_id": "c" * 32}
            )
            connection = http.client.HTTPConnection("127.0.0.1", provider_port, timeout=2)
            connection.request(
                "POST",
                "/fixture/phase",
                body=json.dumps({"phase": "running"}),
                headers={"Content-Type": "application/json"},
            )
            assert connection.getresponse().status == 200
            connection.close()
            assert not dispatch(h, "observe")["reallocated"]
            h.server.heartbeat(state["id"])
            signing = serialization.load_pem_private_key(
                (root / "key.pem").read_bytes(), password=None
            )
            payload = claims(time.time())
            payload.update(
                salad_organization_name="fixture-org",
                salad_workload_id=ID,
                salad_workload_name=h.server.policy.target.selection.salad.group,
            )
            token = signed(signing, payload)
            request = SaladSafetyService._request(
                h.server.policy.target.selection.salad.execution.argv
            )
            context = ssl.create_default_context(cafile=root / "cert.pem")

            def workload(path, body):
                conn = http.client.HTTPSConnection(
                    "localhost", safety_port, context=context, timeout=3
                )
                try:
                    conn.request(
                        "POST",
                        path,
                        encoded(body),
                        {"Content-Type": "application/json", "Authorization": "Bearer " + token},
                    )
                    reply = conn.getresponse()
                    return reply.status, json.loads(reply.read(4096))
                finally:
                    conn.close()

            assert workload("/v1/workload/begin-once", dict(binding=request))[0] == 200
            assert workload("/v1/workload/begin-once", dict(binding=request))[0] == 401
            output = {"schema": "isolated-public-output", "synthetic_gpu": True, "result": 123}
            checksum = hashlib.sha256(encoded(output)).hexdigest()
            status, reply = workload(
                "/v1/workload/output", dict(binding=request, output=output, sha256=checksum)
            )
            assert status == 200 and reply["retained"]
            command(h, "cancel")
            assert dispatch(h, "stop")["cancellation"] == "pending"
            stopped = dispatch(h, "observe")
            assert stopped["termination_confirmed"] and stopped["cancellation"] == "confirmed"
            deadline = time.monotonic() + 5
            while (
                safety.status(h.store.get(h.worker, state["id"]))["status"] != "stopped"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            imported = h.server.import_retained(state["id"])
            assert imported["output"] == output and imported["manifest"]["complete"]
            # The independent database retains real bytes, not just a signed claim.
            with sqlite3.connect(root / "custody" / "custody.db") as database:
                retained = database.execute("SELECT data FROM files").fetchone()[0]
            assert retained == encoded(output)
            restart = TrustedSaladServer(
                state_dir=h.journey.state,
                worker=h.worker,
                policy=h.server.policy,
                resolver=h.server.resolver,
                safety=safety,
            )
            restart.transport._factory = connections
            reconnect = restart.store.get(h.worker, state["id"])
            assert reconnect["receipts"] == stopped["receipts"]
            assert restart.import_retained(state["id"])["output"] == output
            with pytest.raises(Conflict):
                restart.dispatcher(state["id"]).dispatch(state["id"], "create")
            events = json.loads((root / "provider-events.json").read_text())
            assert events.count("create") == events.count("start") == 1
            assert reconnect["cost_cents"] is None
        finally:
            (root / "shutdown").touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    "index,value",
    [
        (2, "b" * 32),
        (3, "2"),
        (4, "evil.invalid"),
        (5, "a" * 64),
        (6, "30"),
        (7, "512"),
        (8, "2"),
        (9, "5"),
    ],
)
def test_smoke_binding_cannot_override_approved_argv(index, value):
    from tessera_control.remote_execution_models import derived_salad_argv

    execution = profile() | {"argv_binding": "smoke_operation_v1"}
    approved = [
        "/tessera/bin/run",
        "v1",
        "public-integers-v1",
        "operation-id",
        "operation-epoch",
        "localhost",
        execution["watchdog_profile"],
        "60",
        "1024",
        "1",
        "4",
    ]
    execution["argv"] = list(approved[1:])
    execution["argv"][2:4] = ["a" * 32, "1"]
    assert derived_salad_argv(execution, approved, "a" * 32, 1, 60) == execution["argv"]
    execution["argv"][index] = value
    with pytest.raises(ValueError):
        derived_salad_argv(execution, approved, "a" * 32, 1, 60)


@pytest.mark.parametrize(
    "change", ["unknown-mode", "arbitrary-placeholder", "moved-slot", "exact-mode"]
)
def test_smoke_binding_is_not_free_form_template(change):
    from tessera_control.remote_execution_models import derived_salad_argv

    execution = profile() | {"argv_binding": "smoke_operation_v1"}
    approved = [
        "/tessera/bin/run",
        "v1",
        "public-integers-v1",
        "operation-id",
        "operation-epoch",
        "localhost",
        execution["watchdog_profile"],
        "60",
        "1024",
        "1",
        "4",
    ]
    execution["argv"] = list(approved[1:])
    execution["argv"][2:4] = ["a" * 32, "1"]
    if change == "unknown-mode":
        execution["argv_binding"] = "template"
    elif change == "exact-mode":
        execution["argv_binding"] = "exact"
    elif change == "arbitrary-placeholder":
        approved[3] = "effect-id"
    else:
        approved[5] = "operation-id"
    with pytest.raises(ValueError):
        derived_salad_argv(execution, approved, "a" * 32, 1, 60)


def test_reservation_forwards_only_retained_runtime_package(tmp_path, monkeypatch):
    from tessera_control.remote_execution_models import RemoteProposal

    h = fixture(tmp_path, monkeypatch, smoke=True)
    wire(h, *preflight(h, absent=True))
    h.server.preflight(RemoteProposal.model_validate(h.route["proposal"]), h.route["id"], "create")
    reservation = h.safety_calls[0]
    with sqlite3.connect(h.store.database_path) as database:
        retained = json.loads(
            database.execute(
                "SELECT package_json FROM execution_runtime_packages WHERE operation_id=?",
                (h.body["operation_id"],),
            ).fetchone()[0]
        )
    assert reservation["runtime_package"] == retained
    assert canonical_digest(retained) == h.body["package_digest"]
    assert retained["plan"]["execution_spec"]["argv"][3:5] == ["operation-id", "operation-epoch"]
    assert retained["plan"]["execution_spec"]["output_paths"] == ["output.json"]
    assert "runtime_package" not in h.store.get(h.worker, h.route["id"])


def test_exact_profile_serializes_pre_binding_shape_and_smoke_stays_explicit():
    from tessera_control.remote_execution_models import SaladExecution

    legacy = profile() | {"gateway": False, "ssh": False, "exec": False}
    for source in (legacy, legacy | {"argv_binding": "exact"}):
        parsed = SaladExecution.model_validate(source)
        assert parsed.argv_binding == "exact"
        assert parsed.model_dump(mode="json") == legacy
        assert json.loads(parsed.model_dump_json()) == legacy
        assert canonical_digest(parsed.model_dump(mode="json")) == canonical_digest(legacy)
    smoke = SaladExecution.model_validate(legacy | {"argv_binding": "smoke_operation_v1"})
    assert smoke.model_dump(mode="json")["argv_binding"] == "smoke_operation_v1"
    assert json.loads(smoke.model_dump_json())["argv_binding"] == "smoke_operation_v1"
    with pytest.raises(ValueError):
        SaladExecution.model_validate(legacy | {"argv_binding": "template"})


def test_retained_legacy_exact_route_reopens_and_fake_dispatches_without_rehash(tmp_path):
    from test_salad_proxy import fixture as fake_fixture

    from tessera_control.remote_execution_models import RemoteExecutionPackage
    from tessera_control.salad_proxy import FakeSaladProxy
    from tessera_worker.remote_dispatch import RemoteDispatcher
    from tessera_worker.salad_remote import SaladProxyFakeAdapter

    # At original admission (before human reviews), the exact serializer writes
    # precisely the old shape. No late DB edits, digest replacement or grants.
    h = fake_fixture(tmp_path)
    with sqlite3.connect(h.store.database_path) as database:
        original = json.loads(
            database.execute(
                "SELECT payload_json FROM events WHERE event_type='remote_execution.state' "
                "AND resource_revision=1 AND json_extract(payload_json,'$.id')=?",
                (h.route["id"],),
            ).fetchone()[0]
        )
    assert "argv_binding" not in original["proposal"]["selection"]["salad"]["execution"]
    assert (
        "argv_binding"
        not in original["routing_package"]["proposal"]["selection"]["salad"]["execution"]
    )
    original_proposal = copy.deepcopy(original["proposal"])
    original_package = copy.deepcopy(original["routing_package"])
    original_digest = original["proposal_digest"]
    assert canonical_digest(original_package) == original_digest
    decoded = RemoteExecutionPackage.model_validate(original_package)
    assert decoded.model_dump(mode="json") == original_package
    assert canonical_digest(json.loads(decoded.model_dump_json())) == original_digest
    h.store = RemoteExecutionStore(h.journey.state, policies=[h.policy])
    proxy = FakeSaladProxy(
        store=h.store,
        worker=h.worker,
        policy=h.proxy_policy,
        resolver=h.resolver,
        transport=h.transport,
    )
    h.dispatcher = RemoteDispatcher(
        h.store,
        h.worker,
        {
            h.policy.selection.target_digest: SaladProxyFakeAdapter(proxy, h.route["id"]),
        },
    )
    assert dispatch(h, "create")["state"] == "stopped"
    assert dispatch(h, "start")["state"] == "running"
    state = h.store.get(h.worker, h.route["id"])
    assert state["proposal"] == original_proposal
    assert state["routing_package"] == original_package
    assert state["proposal_digest"] == original_digest
    assert all(receipt["provenance"] == "synthetic" for receipt in state["receipts"])
