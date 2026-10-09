"""Synthetic node/API observations only. No socket, process, kernel or cluster effects."""

import copy
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_capability_activation import NOW, candidate  # noqa: F401
from test_capability_collector import collection, receipt  # noqa: F401
from test_capability_kubernetes_runner import bind_plan, raw_fixture

from tessera_worker.capability_activation import (
    ActivationJournal,
    CollectorActivationGate,
    canonical,
)
from tessera_worker.capability_collector import CollectorJournal
from tessera_worker.capability_collector_cli import main
from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.capability_inspection import probe_requests
from tessera_worker.capability_kube_client import APIResponse, CollectionError, decode, encoded
from tessera_worker.capability_witness import (
    KINDS,
    WitnessPin,
    WitnessReport,
    challenge_for,
    open_report,
    safe_payload,
    seal_report,
    validate_report,
)
from tessera_worker.capability_witness_fake import fake_report
from tessera_worker.capability_witness_probes import (
    AdmissionWitness,
    CNIWitness,
    ExternalWatchdogWitness,
    QuotaWitness,
    UnavailableNodeSensors,
)
from tessera_worker.capability_witness_transport import (
    REQUEST_DOMAIN,
    IndependentRuntimeWitnesses,
    WitnessDaemon,
    WitnessRPC,
    cleanup_owned,
    partition,
)


def pin_for(plan, kind):
    return WitnessPin(
        kind=kind,
        identity=plan.activation.watchdog.identity if kind == "watchdog" else "observer-" + kind,
        namespace="observers",
        namespace_uid="observer-ns",
        node_uid="node-uid",
        boot_id="boot-uid",
    )


@pytest.fixture
def reports(candidate):  # noqa: F811
    plan, pvc = bind_plan(candidate.manifest)
    raw = raw_fixture(plan, pvc)
    requests = {k: v for k, v in probe_requests(plan).items() if "dryRun" not in v}
    result = {}
    for kind, inputs in partition(requests).items():
        pin = pin_for(plan, kind)
        request = challenge_for(
            plan,
            kind,
            challenge="9" * 64,
            started_at=NOW.isoformat(),
            deadline=(NOW + timedelta(seconds=5)).isoformat(),
            requests=inputs,
        )
        report = fake_report(plan, request, pin, {key: raw["probes"][key] for key in inputs})
        result[kind] = report
    return SimpleNamespace(plan=plan, reports=result)


def validate(h, report):
    expected = h.reports[report.request.kind]
    validate_report(
        report,
        expected.request,
        expected.witness,
        h.plan,
        now=NOW + timedelta(seconds=4),
        fake=True,
    )


@pytest.mark.parametrize("kind", KINDS)
def test_all_four_raw_fixtures_pass_and_canonical_signature(reports, kind):
    r = reports.reports[kind]
    validate(reports, r)
    envelope = seal_report(r, b"k" * 32)
    assert open_report(envelope, b"k" * 32) == r
    assert envelope["digest"] == hashlib.sha256(canonical(r)).hexdigest()
    with pytest.raises(CollectionError):
        open_report(envelope, b"z" * 32)
    with pytest.raises(CollectionError):
        validate_report(r, r.request, r.witness, reports.plan, now=NOW + timedelta(seconds=4))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "namespace",
        "node",
        "boot",
        "operation",
        "tenant",
        "project",
        "package",
        "manifest",
        "nonce",
        "challenge",
        "stale",
        "future",
        "partial",
        "unknown",
        "failed",
        "cleanup",
    ],
)
def test_common_binding_freshness_partial_and_cleanup(reports, kind, fault):
    original = reports.reports[kind]
    data = original.model_dump(mode="json")
    if fault in {"identity", "namespace", "node", "boot"}:
        field = {"node": "node_uid", "boot": "boot_id"}.get(fault, fault)
        data["witness"][field] = "wrong"
    elif fault in {"operation", "tenant", "project", "package", "manifest", "nonce", "challenge"}:
        field = {
            "operation": "operation_id",
            "tenant": "org_id",
            "project": "project_id",
            "package": "package_digest",
            "manifest": "manifest_digest",
            "nonce": "run_nonce",
        }.get(fault, fault)
        data["request"][field] = (
            "a" * 64 if fault in {"package", "manifest", "nonce", "challenge"} else "wrong"
        )
    elif fault == "stale":
        data["started_at"] = (NOW - timedelta(seconds=6)).isoformat()
    elif fault == "future":
        data["completed_at"] = (NOW + timedelta(seconds=5)).isoformat()
    elif fault == "partial":
        data["probes"].pop(next(iter(data["probes"])))
    elif fault in {"unknown", "failed"}:
        data["state"] = fault
    else:
        data["cleanup"][0]["preconditions"]["uid"] = "other-uid"
    with pytest.raises(CollectionError):
        validate(reports, WitnessReport.model_validate_json(encoded(data)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("connect", "refused"),
        ("connect", "connected"),
        ("cni", "UNKNOWN"),
        ("http_status", 200),
        ("http_status", 403),
        ("dns_rcode", "NXDOMAIN"),
        ("packets_dropped", 0),
        ("packets_sent", 0),
        ("policy_uid", "wrong"),
        ("proxy_used", True),
        ("proxy_environment", ["HTTP_PROXY"]),
        ("endpoint_challenge", "stale"),
        ("identity", "system:serviceaccount:wrong:probe"),
    ],
)
def test_network_ambiguity_proxy_and_deny_evidence(reports, field, value):
    data = reports.reports["cni"].model_dump(mode="json")
    data["observations"]["samples"][2][field] = value
    with pytest.raises(CollectionError):
        validate(reports, WitnessReport.model_validate_json(encoded(data)))


@pytest.mark.parametrize(
    "fault",
    ["unsupported", "underreported", "cpu", "oom", "pids", "storage", "pvc", "escape", "attempts"],
)
def test_quota_requires_kernel_events_and_hard_filesystem_enforcement(reports, fault):
    data = reports.reports["quota"].model_dump(mode="json")
    o = data["observations"]
    if fault == "unsupported":
        o["filesystems"][0]["mechanism"] = "local-path"
    elif fault == "underreported":
        o["filesystems"][0]["usage_before"] -= 1
    elif fault == "cpu":
        o["cpu_stat_after"]["nr_throttled"] = 0
    elif fault == "oom":
        o["termination"]["reason"] = "Completed"
    elif fault == "pids":
        o["pids_max"] += 1
    elif fault == "storage":
        o["filesystems"][0]["errno"] = "ENOSPC"
    elif fault == "pvc":
        o["pvc_uid"] = "wrong"
    elif fault == "escape":
        o["outside_workspace"]["parent_escape"] = "success"
    else:
        o["allocation_bytes"] = 0
    with pytest.raises(CollectionError):
        validate(reports, WitnessReport.model_validate_json(encoded(data)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("process_exit", None),
        ("cgroup_populated", 1),
        ("write_count_after", 2),
        ("heartbeat_sequence", 0),
        ("heartbeat_challenge", "lost"),
        ("credential_state", "unknown"),
        ("kill_requests", 2),
        ("quiet_until", NOW.isoformat()),
    ],
)
def test_watchdog_false_termination_lost_heartbeat_or_continued_writes(reports, field, value):
    data = reports.reports["watchdog"].model_dump(mode="json")
    data["observations"]["cases"][0][field] = value
    with pytest.raises(CollectionError):
        validate(reports, WitnessReport.model_validate_json(encoded(data)))


@pytest.mark.parametrize(
    "fault",
    [
        "mutated",
        "defaulted",
        "allow",
        "rbac-deny",
        "ignore",
        "generation",
        "version",
        "uid",
        "missing-pod",
    ],
)
def test_admission_mutation_defaulting_policy_and_persisted_identity(reports, fault):
    data = reports.reports["admission"].model_dump(mode="json")
    o = data["observations"]
    if fault == "mutated":
        o["actual"][0]["persisted"]["spec"]["template"]["spec"]["hostNetwork"] = True
    elif fault == "defaulted":
        o["dry_runs"]["Pod:positive"]["response"]["spec"]["schedulerName"] = "unexpected"
    elif fault == "allow":
        o["dry_runs"]["Pod:admission-privileged"]["status"] = 201
    elif fault == "rbac-deny":
        o["dry_runs"]["Pod:admission-privileged"]["response"]["reason"] = "Forbidden"
    elif fault == "ignore":
        o["policies"][0]["spec"]["failurePolicy"] = "Ignore"
    elif fault == "generation":
        o["policies"][0]["status"]["observedGeneration"] += 1
    elif fault == "version":
        o["actual"][0]["persisted"]["metadata"]["resourceVersion"] = "changed"
    elif fault == "uid":
        o["actual"][0]["persisted"]["metadata"]["uid"] = "other"
    else:
        o["actual"].pop()
    with pytest.raises(CollectionError):
        validate(reports, WitnessReport.model_validate_json(encoded(data)))


@pytest.mark.parametrize(
    "secret",
    [
        {"token": "private"},
        {"Authorization": "x"},
        {"message": "Bearer credential"},
        {"nested": [{"private_key": "x"}]},
        {"message": "-----BEGIN PRIVATE KEY-----"},
    ],
)
def test_secret_payloads_rejected_before_custody(secret, caplog):
    with pytest.raises(CollectionError) as exc:
        safe_payload(secret)
    assert "private" not in str(exc.value) and "credential" not in caplog.text
    with pytest.raises(CollectionError):
        safe_payload({"message": "known-runtime-value"}, secrets=(b"known-runtime-value",))


@pytest.mark.parametrize("fault", ["none", "uid", "nonce", "namespace", "version", "still-present"])
def test_cleanup_exact_owned_uid_preconditions_only(reports, fault):
    r = reports.reports["quota"]
    resource = r.resources[0]
    obj = {
        "metadata": {
            "name": resource.name,
            "namespace": resource.namespace,
            "uid": resource.uid,
            "resourceVersion": resource.resource_version,
            "annotations": {"tessera.io/nonce": resource.run_nonce},
        }
    }
    if fault in {"uid", "namespace", "version"}:
        obj["metadata"]["resourceVersion" if fault == "version" else fault] = "wrong"
    elif fault == "nonce":
        obj["metadata"]["annotations"]["tessera.io/nonce"] = "a" * 64
    calls = []

    def read(_):
        return (
            obj
            if not calls or fault == "still-present"
            else {"kind": "Status", "code": 404, "reason": "NotFound"}
        )

    def delete(r, preconditions):
        calls.append((r, preconditions))

    if fault == "none":
        cleanup_owned(resource, r.request, read=read, delete=delete)
        assert calls == [
            (resource, {"uid": resource.uid, "resourceVersion": resource.resource_version})
        ]
    else:
        with pytest.raises(CollectionError):
            cleanup_owned(resource, r.request, read=read, delete=delete)
        assert len(calls) == (1 if fault == "still-present" else 0)


class FakeDaemonTransport:
    """Synthetic transport injected in code only, never selectable by a config file."""

    def __init__(self, plan, snapshot, pin, request_key, report_key, clock, fault=None):
        self.plan, self.snapshot, self.pin = plan, snapshot, pin
        self.request_key, self.report_key, self.clock = request_key, report_key, clock
        self.fault, self.calls = fault, 0

    def send(self, request, *, deadline, limit):
        self.calls += 1
        message = decode(request.body)
        payload = encoded(message["request"])
        assert (
            message["signature"]
            == hmac.new(self.request_key, REQUEST_DOMAIN + payload, "sha256").hexdigest()
        )
        from tessera_worker.capability_witness import Challenge

        challenge = Challenge.model_validate_json(payload)
        probes = {k: self.snapshot["probes"][k] for k in challenge.inputs["probes"]}
        report = fake_report(self.plan, challenge, self.pin, probes)
        # Simulate trusted daemon wire provenance ONLY in this explicitly fake transport.
        data = report.model_dump(mode="json")
        data["source"] = "independent-daemon"
        if self.pin.kind == "cni":
            policy = next(o for o in self.snapshot["objects"] if o["kind"] == "NetworkPolicy")
            data["observations"]["policy_resource_version"] = policy["metadata"]["resourceVersion"]
        elif self.pin.kind == "admission":
            for key, kind in (
                ("policies", "ValidatingAdmissionPolicy"),
                ("bindings", "ValidatingAdmissionPolicyBinding"),
            ):
                data["observations"][key] = [
                    copy.deepcopy(o) for o in self.snapshot["objects"] if o["kind"] == kind
                ]
        if self.fault:
            self.fault(data)
        report = WitnessReport.model_validate_json(encoded(data))
        self.clock[0] = NOW + timedelta(seconds=4)
        return APIResponse(200, encoded(seal_report(report, self.report_key)), "fake-rpc-audit")


def independent(h, *, fault=None):
    clock, channels = [NOW], {}
    for index, kind in enumerate(KINDS):
        pin = pin_for(h.plan, kind)
        request_key, report_key = bytes([10 + index]) * 32, bytes([20 + index]) * 32
        transport = FakeDaemonTransport(
            h.plan,
            h.snapshot,
            pin,
            request_key,
            report_key,
            clock,
            fault=fault if kind == "quota" else None,
        )
        channels[kind] = WitnessRPC(
            pin=pin,
            transport=transport,
            request_key=request_key,
            report_key=report_key,
            monotonic=lambda: 10.0,
        )
    h.collector.allow_fake_witnesses = False
    h.collector.now = lambda: clock[0]
    h.verifier.now = lambda: clock[0]
    h.collector.witness = IndependentRuntimeWitnesses(
        channels=channels, journal=h.journal, now=lambda: clock[0], monotonic=lambda: 10.0
    )
    return channels, clock


def test_legacy_rpc_collection_is_not_target_activation_and_cannot_replay(collection, tmp_path):  # noqa: F811
    h = collection
    channels, _ = independent(h)
    journal = ActivationJournal(tmp_path / "runtime-gate")
    try:
        gate = CollectorActivationGate(collector=h.collector, verifier=h.verifier, journal=journal)
        with pytest.raises(BackendBlocked, match="target profile required"):
            gate.authorize(h.plan, h.verifier.verify(h.candidate.package))
        assert set(receipt(h)["records"]["witnesses"]) == set(KINDS)
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, h.verifier.verify(h.candidate.package))
        assert all(c.transport.calls == 1 for c in channels.values())
        with h.journal.connect() as db:
            assert db.execute("SELECT count(*) FROM witness_reports").fetchone()[0] == 4
            with pytest.raises(Exception, match="retained witness"):
                db.execute("DELETE FROM witness_attempts")
    finally:
        journal.close()


@pytest.mark.parametrize("fault", ["unknown", "partial", "contradiction", "wrong-nonce"])
def test_one_witness_blocks_activation_and_burns_attempt(collection, tmp_path, fault):  # noqa: F811
    h = collection

    def mutate(data):
        if fault == "unknown":
            data["state"] = "unknown"
        elif fault == "partial":
            data["probes"].clear()
        elif fault == "contradiction":
            data["observations"]["memory_max"] += 1
        else:
            data["request"]["run_nonce"] = "a" * 64

    channels, _ = independent(h, fault=mutate)
    journal = ActivationJournal(tmp_path / "runtime-gate")
    try:
        gate = CollectorActivationGate(collector=h.collector, verifier=h.verifier, journal=journal)
        with pytest.raises(CollectionError):
            gate.authorize(h.plan, h.verifier.verify(h.candidate.package))
        assert receipt(h)["result"] != "pass"
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, h.verifier.verify(h.candidate.package))
        assert all(c.transport.calls == 1 for c in channels.values())
    finally:
        journal.close()


def test_unmarked_operator_witness_and_fake_without_opt_in_blocked(collection):  # noqa: F811
    h = collection
    h.collector.allow_fake_witnesses = False
    with pytest.raises(CollectionError, match="auth-denied"):
        h.collector.collect(h.plan, probe_requests(h.plan))


@pytest.mark.parametrize(
    "program", [CNIWitness, QuotaWitness, ExternalWatchdogWitness, AdmissionWitness]
)
def test_missing_node_sensor_is_unknown_without_effects(reports, program):
    request = reports.reports[program.kind].request
    result = program(UnavailableNodeSensors(), monotonic=lambda: 10.0)(request, deadline=15.0)
    assert result == dict(state="unknown", resources=(), cleanup=(), observations={}, probes={})


def test_daemon_authentication_durable_replay_and_revocation(reports, tmp_path):
    report = reports.reports["quota"]
    journal = CollectorJournal(tmp_path / "daemon")
    calls = []
    authority = [False]
    daemon = WitnessDaemon(
        pin=report.witness,
        request_key=b"\x91" * 32,
        report_key=b"\x92" * 32,
        journal=journal,
        approved_plan=reports.plan,
        harmless_images=frozenset({reports.plan.activation.image}),
        execute=lambda *a, **kw: calls.append(True),
        authority=lambda _: authority[0],
        now=lambda: NOW,
    )
    payload = canonical(report.request)
    message = {
        "request": decode(payload),
        "signature": hmac.new(b"\x91" * 32, REQUEST_DOMAIN + payload, "sha256").hexdigest(),
    }
    try:
        bad = copy.deepcopy(message)
        bad["signature"] = "0" * 64
        with pytest.raises(CollectionError):
            daemon.handle(encoded(bad))
        with pytest.raises(CollectionError):
            daemon.handle(encoded(message))
        authority[0] = True
        with pytest.raises(CollectionError):
            daemon.handle(encoded(message))
        assert not calls
        with journal.connect() as db:
            assert db.execute("SELECT count(*) FROM witness_attempts").fetchone()[0] == 1
    finally:
        journal.close()


def test_witness_command_zero_default_effects_and_schema(monkeypatch, capsys):
    monkeypatch.setattr(
        "tessera_worker.capability_collector_cli.private_read", lambda *_: pytest.fail("read")
    )
    assert main(["--config", "/never-read"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert main(["--witness-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "witnesses" in schema["required"]
    assert schema["additionalProperties"] is False


def test_revocation_during_witness_collection_never_authorizes(collection, tmp_path):  # noqa: F811
    h = collection
    channels, _ = independent(h)
    channels["quota"].transport.fault = lambda _: setattr(h.verifier, "authority", lambda _: False)
    verified = h.verifier.verify(h.candidate.package)
    journal = ActivationJournal(tmp_path / "revoke-during-witness")
    try:
        gate = CollectorActivationGate(collector=h.collector, verifier=h.verifier, journal=journal)
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, verified)
        assert receipt(h)["result"] == "pass"  # observation is not current authority
        h.verifier.authority = lambda _: True
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, verified)
        assert all(c.transport.calls == 1 for c in channels.values())
    finally:
        journal.close()


def test_secret_daemon_reply_not_retained(collection, caplog):  # noqa: F811
    h = collection
    independent(h, fault=lambda data: data["observations"].update(token="never-retain-this-secret"))
    with pytest.raises(CollectionError):
        h.collector.collect(h.plan, probe_requests(h.plan))
    with h.journal.connect() as db:
        for table in ("collector_evidence", "witness_reports"):
            for row in db.execute(f"SELECT payload FROM {table}"):
                assert b"never-retain-this-secret" not in row[0]
    assert "never-retain-this-secret" not in caplog.text


def test_daemon_executes_only_fresh_authenticated_challenge(reports, tmp_path):
    r = reports.reports["quota"]
    journal = CollectorJournal(tmp_path / "daemon-success")
    calls = []

    def execute(request, *, deadline):
        calls.append(request.challenge)
        return {
            k: getattr(r, k) for k in ("state", "resources", "cleanup", "observations", "probes")
        }

    daemon = WitnessDaemon(
        pin=r.witness,
        request_key=b"\x91" * 32,
        report_key=b"\x92" * 32,
        journal=journal,
        approved_plan=reports.plan,
        harmless_images=frozenset({reports.plan.activation.image}),
        execute=execute,
        authority=lambda _: True,
        now=lambda: NOW,
        monotonic=lambda: 10.0,
    )
    payload = canonical(r.request)
    message = encoded(
        {
            "request": decode(payload),
            "signature": hmac.new(b"\x91" * 32, REQUEST_DOMAIN + payload, "sha256").hexdigest(),
        }
    )
    try:
        response = open_report(decode(daemon.handle(message)), b"\x92" * 32)
        assert response.source == "independent-daemon"
        assert calls == [r.request.challenge]
        with pytest.raises(CollectionError):
            daemon.handle(message)
        assert calls == [r.request.challenge]
    finally:
        journal.close()
