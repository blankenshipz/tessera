"""Offline monitor/receiver conformance. No Docker, cloud, credentials or provider calls."""

import json
import multiprocessing
import os
import signal
import socket
import struct
import time
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from tessera_control.monitor_evidence import Ack, MonitorReceiver, accept_ack
from tessera_monitor.channel import Channel
from tessera_monitor.cli import main, private_read
from tessera_monitor.daemon import GuardianPeer, supervise_child
from tessera_monitor.journal import MonitorJournal
from tessera_monitor.protocol import (
    CATEGORIES,
    MAX_FRAME,
    Artifact,
    Assignment,
    Denied,
    Envelope,
    Fact,
    Limits,
    Observation,
    Pulse,
    Workload,
    digest,
    encode,
    open_envelope,
    seal,
)
from tessera_monitor.runtime import DisabledAdapter, Monitor
from tessera_worker.capability_journal import JournalConflict

KEYS = dict(authority_key=b"a" * 32, channel_key=b"c" * 32, evidence_key=b"e" * 32)


@dataclass
class Clock:
    seconds: float = 0.0
    base: int = 100000

    def monotonic(self):
        return self.seconds

    def wall(self):
        return self.base + int(self.seconds * 1000)


def assignment(**changes):
    return Assignment(
        **(
            dict(
                tenant="tenant-a",
                project="project-a",
                plan_digest="1" * 64,
                package_digest="2" * 64,
                operation="operation-a",
                nonce="3" * 64,
                monitor="monitor-a",
                worker="worker-a",
                authority="authority-a",
                adapter="fake-v1",
                workload=Workload(
                    uid="4" * 64,
                    image_digest="5" * 64,
                    instance_digest="6" * 64,
                    target_digest="7" * 64,
                ),
                limits=Limits(
                    runtime_seconds=20,
                    lease_seconds=3,
                    memory_bytes=1024,
                    cpu_millicores=250,
                    pids=32,
                    filesystem_bytes=4096,
                    filesystem_inodes=32,
                    output_bytes=0,
                ),
                privilege_profile="fake",
                issued_ms=100000,
                expires_ms=120000,
            )
            | changes
        )
    )


def observed(category, state="verified", **facts):
    return Observation(
        category=category, state=state, facts=tuple(Fact(name=k, value=v) for k, v in facts.items())
    )


class FakeAdapter:
    kind, source = "fake-v1", "fake"

    def __init__(self, a, fault=None):
        self.expected, self.fault = a, fault
        self.running, self.removed = True, False
        self.kills, self.cleanups = 0, 0

    def preflight(self, a):
        if a != self.expected or self.fault == "preflight":
            raise Denied("fake binding mismatch")

    def observe(self, a):
        self.preflight(a)
        if self.fault == "observe":
            raise RuntimeError("SECRET_CANARY")
        result = [
            observed(k, "unknown" if self.fault == k else "verified", running=self.running)
            for k in ("identity", "namespace", "network", "resources", "filesystem", "custody")
        ]
        if not self.running:
            result.append(observed("termination", running=False))
        if self.removed:
            result.append(observed("cleanup", absent=True))
        return tuple(result)

    def kill(self, a):
        self.preflight(a)
        self.kills += 1
        if self.fault == "kill_lost":
            raise RuntimeError("SECRET_CANARY")
        self.running = False

    def cleanup(self, a):
        self.preflight(a)
        assert not self.running
        self.cleanups += 1
        if self.fault == "cleanup_lost":
            raise RuntimeError("SECRET_CANARY")
        self.removed = True


@pytest.fixture
def setup(tmp_path):
    a, clock = assignment(), Clock()
    j = MonitorJournal(tmp_path / "monitor")
    control = MonitorJournal(tmp_path / "control")
    document = seal(a, KEYS["authority_key"], b"assignment")
    adapter = FakeAdapter(a)
    m = Monitor(
        document,
        identity=a.monitor,
        journal=j,
        adapter=adapter,
        wall=clock.wall,
        clock=clock.monotonic,
        **KEYS,
    )
    receiver = MonitorReceiver(
        document,
        journal=control,
        tenant=a.tenant,
        project=a.project,
        monitor=a.monitor,
        now=clock.wall,
        **KEYS,
    )
    yield m, receiver, adapter, clock, document
    j.close()
    control.close()


def heartbeat(m, receiver):
    m.accept(receiver.respond(m.challenge(), authorized=lambda a: True))


def reconcile(m, receiver):
    for document in m.journal.documents():
        accept_ack(m.journal, receiver.ingest(document), m.a, KEYS["channel_key"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant", "tenant-b"),
        ("project", "project-b"),
        ("operation", "other"),
        ("plan_digest", "a" * 64),
        ("package_digest", "b" * 64),
        ("nonce", "c" * 64),
        ("monitor", "monitor-b"),
        ("worker", "worker-b"),
        ("authority", "authority-b"),
        ("adapter", "kubernetes-v1"),
        ("privilege_profile", "disabled"),
        ("expires_ms", 119000),
    ],
)
def test_assignment_signature_binds_every_field(field, value):
    doc = seal(assignment(), KEYS["authority_key"], b"assignment")
    payload = json.loads(doc.payload)
    payload[field] = value
    bad = Envelope(payload=encode(payload).decode(), mac=doc.mac)
    with pytest.raises(Denied, match="invalid authenticated"):
        open_envelope(bad, Assignment, KEYS["authority_key"], b"assignment")


@pytest.mark.parametrize("field", ["uid", "image_digest", "instance_digest", "target_digest"])
def test_assignment_binds_workload(field):
    doc = seal(assignment(), KEYS["authority_key"], b"assignment")
    payload = json.loads(doc.payload)
    payload["workload"][field] = "f" * 64
    with pytest.raises(Denied):
        open_envelope(
            Envelope(payload=encode(payload).decode(), mac=doc.mac),
            Assignment,
            KEYS["authority_key"],
            b"assignment",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("required", ("network",)),
        ("expires_ms", 100000),
        ("expires_ms", 121000),
        ("monitor", "worker-a"),
        ("nonce", "arbitrary-secret"),
        ("issued_ms", True),
    ],
)
def test_assignment_rejects_weakened_contract(field, value):
    with pytest.raises(ValidationError):
        assignment(**{field: value})


def test_immutable_nested_assignment():
    a = assignment()
    with pytest.raises(ValidationError):
        a.limits.pids = 999
    with pytest.raises(ValidationError):
        a.workload.uid = "f" * 64


def test_authenticated_heartbeat_reconciliation_and_fail_closed_activation(setup):
    m, r, adapter, _, _ = setup
    heartbeat(m, r)
    m.tick()
    reconcile(m, r)
    assert not m.stopped and not adapter.kills
    assert r.status()["activation"] == "blocked"
    assert not r.status()["authentic"]  # fake observations cannot claim live provenance
    assert r.status()["fresh"]
    assert {"termination", "cleanup", "recovery"} <= set(r.status()["unknown"])


@pytest.mark.parametrize("advance", [3.0, 20.0, -0.1])
def test_independent_clock_deadline_kill(setup, advance):
    m, r, adapter, clock, _ = setup
    heartbeat(m, r)
    clock.seconds += advance
    m.tick()
    assert m.stopped and adapter.kills == adapter.cleanups == 1
    assert any(o.category == "termination" for x in m.records for o in x.observations)


def test_control_plane_loss_expires_locally_and_no_re_effect(setup):
    m, r, adapter, clock, _ = setup
    heartbeat(m, r)
    clock.seconds = 3
    m.tick()
    for _ in range(3):
        m.tick()
        m.stop()
    assert adapter.kills == adapter.cleanups == 1
    reconcile(m, r)
    assert r.status()["termination_confirmed"] and r.status()["cleanup_confirmed"]
    assert r.status()["activation"] == "blocked"


def test_heartbeats_do_not_extend_absolute_deadline(setup):
    m, r, adapter, clock, _ = setup
    for i in range(10):
        clock.seconds = i * 2
        heartbeat(m, r)
    clock.seconds = 20
    m.tick()
    assert adapter.kills == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "f" * 64),
        ("assignment", "e" * 64),
        ("monitor", "other"),
        ("challenge", "d" * 64),
        ("sequence", 2),
        ("issued_ms", 100001),
        ("action", "challenge"),
    ],
)
def test_nonce_challenge_sequence_bindings_fail_and_kill(setup, field, value):
    m, r, adapter, _, _ = setup
    reply = r.respond(m.challenge(), authorized=lambda a: True)
    pulse = open_envelope(reply, Pulse, KEYS["channel_key"], b"response")
    with pytest.raises(Denied):
        m.accept(seal(pulse.model_copy(update={field: value}), KEYS["channel_key"], b"response"))
    assert adapter.kills == 1


def test_late_valid_response_cannot_resurrect(setup):
    m, r, adapter, clock, _ = setup
    response = r.respond(m.challenge(), authorized=lambda a: True)
    clock.seconds = 3
    with pytest.raises(Denied):
        m.accept(response)
    assert adapter.kills == 1


def test_replay_response_fences_monitor(setup):
    m, r, adapter, _, _ = setup
    response = r.respond(m.challenge(), authorized=lambda a: True)
    m.accept(response)
    m.challenge()
    with pytest.raises(Denied):
        m.accept(response)
    assert adapter.kills == 1


def test_replay_challenge_rejected_durably(setup):
    m, r, _, _, _ = setup
    challenge = m.challenge()
    r.respond(challenge, authorized=lambda a: True)
    with pytest.raises(Denied):
        r.respond(challenge, authorized=lambda a: True)


def test_revoke_is_stop_only(setup):
    m, r, adapter, _, _ = setup
    m.accept(r.respond(m.challenge(), authorized=lambda a: False))
    assert m.stopped and adapter.kills == 1


@pytest.mark.parametrize(
    "fault", ["identity", "namespace", "network", "resources", "filesystem", "custody", "observe"]
)
def test_unknown_observation_fails_closed(setup, fault):
    m, r, adapter, _, _ = setup
    adapter.fault = fault
    heartbeat(m, r)
    m.tick()
    assert m.stopped and adapter.kills == 1
    assert b"SECRET_CANARY" not in b"".join(encode(d) for d in m.journal.documents())


@pytest.mark.parametrize("fault", ["kill_lost", "cleanup_lost", None])
def test_monitor_crash_recovery_never_retries_effect(setup, fault):
    m, _, adapter, clock, document = setup
    adapter.fault = fault
    m.stop()
    before = adapter.kills, adapter.cleanups
    clock.seconds = 30  # historical cleanup allowed after authority expires
    m.journal.close()
    reopened = MonitorJournal(m.journal.root)
    try:
        recovered = Monitor(
            document,
            identity=m.a.monitor,
            journal=reopened,
            adapter=adapter,
            recover=True,
            wall=clock.wall,
            clock=clock.monotonic,
            **KEYS,
        )
        assert recovered.stopped
        assert (adapter.kills, adapter.cleanups) == before
        assert "recovery" in {x.kind for x in recovered.records}
    finally:
        reopened.close()


def test_crash_before_stop_dispatch_allows_one_historical_stop(setup):
    m, _, adapter, clock, document = setup
    m.journal.close()
    clock.seconds = 40
    j = MonitorJournal(m.journal.root)
    try:
        Monitor(
            document,
            identity=m.a.monitor,
            journal=j,
            adapter=adapter,
            recover=True,
            wall=clock.wall,
            clock=clock.monotonic,
            **KEYS,
        )
        assert adapter.kills == adapter.cleanups == 1
    finally:
        j.close()


def test_unknown_stop_can_be_confirmed_by_later_read_without_retry(setup):
    m, r, adapter, _, _ = setup
    adapter.fault = "kill_lost"
    m.stop()
    reconcile(m, r)
    assert not r.status()["termination_confirmed"]
    adapter.running = False
    adapter.fault = None
    m.emit("observation", adapter.observe(m.a))
    reconcile(m, r)
    assert r.status()["termination_confirmed"]
    assert adapter.kills == 1 and adapter.cleanups == 0


def test_exclusive_journal_owner_and_one_use_assignment(setup):
    m, _, adapter, clock, doc = setup
    with pytest.raises(JournalConflict):
        MonitorJournal(m.journal.root)
    with pytest.raises(Denied, match="consumed"):
        Monitor(
            doc,
            identity=m.a.monitor,
            journal=m.journal,
            adapter=adapter,
            wall=clock.wall,
            clock=clock.monotonic,
            **KEYS,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("assignment", "f" * 64),
        ("nonce", "f" * 64),
        ("monitor", "other"),
        ("sequence", 2),
        ("previous", "f" * 64),
        ("observed_ms", 100001),
    ],
)
def test_receiver_rejects_wrong_evidence_binding_or_chain(setup, field, value):
    m, r, _, _, _ = setup
    bad = m.records[0].model_copy(update={field: value})
    with pytest.raises(Denied):
        r.ingest(seal(bad, KEYS["evidence_key"], b"evidence"))


def test_receiver_verifies_evidence_signature_not_operator_claim(setup):
    m, r, _, _, _ = setup
    bad = seal(m.records[0], b"x" * 32, b"evidence")
    with pytest.raises(Denied):
        r.ingest(bad)
    assert r.status()["unknown"] == list(CATEGORIES)


def test_evidence_replay_is_idempotent_not_authority(setup):
    m, r, _, clock, _ = setup
    doc = m.journal.documents()[0]
    assert r.ingest(doc) == r.ingest(doc)
    clock.seconds = 30
    assert r.ingest(doc)  # historical reconciliation permitted
    assert not r.status()["fresh"] and r.status()["activation"] == "blocked"


def test_conflicting_replay_rejected(setup):
    m, r, _, _, _ = setup
    r.ingest(m.journal.documents()[0])
    conflicting = m.records[0].model_copy(update={"kind": "unknown"})
    with pytest.raises(Denied):
        r.ingest(seal(conflicting, KEYS["evidence_key"], b"evidence"))


def test_ack_binding_and_no_receipt_deletion(setup):
    m, r, _, _, _ = setup
    ack = r.ingest(m.journal.documents()[0])
    accept_ack(m.journal, ack, m.a, KEYS["channel_key"])
    parsed = open_envelope(ack, Ack, KEYS["channel_key"], b"ack")
    for changes in ({"nonce": "f" * 64}, {"receipt": "f" * 64}, {"sequence": 2}):
        with pytest.raises(Denied):
            accept_ack(
                m.journal,
                seal(parsed.model_copy(update=changes), KEYS["channel_key"], b"ack"),
                m.a,
                KEYS["channel_key"],
            )
    assert len(m.journal.documents()) == 1
    with m.journal.connect() as db, pytest.raises(Exception, match="retained"):
        db.execute("DELETE FROM monitor_receipts")


def test_artifact_receipt_link_requires_independent_custody(setup):
    m, r, _, _, _ = setup
    artifact = Artifact(sha256="8" * 64, size=0, custody_receipt="9" * 64)
    m.emit(
        "observation",
        (
            Observation(
                category="custody",
                state="verified",
                artifact=artifact,
                facts=(
                    Fact(name="sha256", value=artifact.sha256),
                    Fact(name="bytes", value=0),
                    Fact(name="writers_frozen", value=True),
                ),
            ),
        ),
    )
    r.ingest(m.journal.documents()[0])
    with pytest.raises(Denied, match="custody"):
        r.ingest(m.journal.documents()[1])
    r.custody = lambda a, output: a == m.a and output == artifact
    r.ingest(m.journal.documents()[1])
    assert "custody" not in r.status()["unknown"]
    assert r.status()["activation"] == "blocked"


@pytest.mark.parametrize("kind", ["kubernetes-v1", "salad-v1"])
@pytest.mark.parametrize("method", ["preflight", "observe", "kill", "cleanup"])
def test_disabled_portable_adapters(kind, method):
    with pytest.raises(Denied, match="disabled"):
        getattr(DisabledAdapter(kind), method)(
            assignment(adapter=kind, privilege_profile="disabled")
        )


def test_protocol_bounds_canonical_and_domain_separation():
    a = assignment()
    doc = seal(a, KEYS["authority_key"], b"assignment")
    with pytest.raises(Denied):
        open_envelope(doc, Assignment, KEYS["authority_key"], b"evidence")
    with pytest.raises(Denied):
        encode("x" * MAX_FRAME)
    with pytest.raises(Denied):
        seal(a, b"weak", b"assignment")
    with pytest.raises(ValidationError):
        Fact(name="token", value="x" * 64)
    with pytest.raises(ValidationError):
        Fact(name="container", value="SECRET_CANARY")


def test_bounded_channel_and_eof():
    a, b = socket.socketpair()
    try:
        left, right = Channel(a), Channel(b)
        doc = seal(assignment(), KEYS["authority_key"], b"assignment")
        left.send(doc)
        assert right.receive() == doc
        a.sendall(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(Denied, match="bound"):
            right.receive()
        a.close()
        with pytest.raises(Denied, match="closed"):
            right.receive()
    finally:
        a.close()
        b.close()


def test_channel_slow_or_partial_frame_is_bounded():
    a, b = socket.socketpair()
    try:
        a.sendall(struct.pack("!I", 100) + b"{")
        start = time.monotonic()
        with pytest.raises((TimeoutError, Denied)):
            Channel(b, timeout=0.05).receive()
        assert time.monotonic() - start < 1
    finally:
        a.close()
        b.close()


def _hung_child(peer):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    peer.send_bytes(b"ready")
    time.sleep(60)


def test_independent_guardian_kills_owned_hung_child():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    p = context.Process(target=_hung_child, args=(child,))
    p.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv_bytes() == b"ready"
        a = assignment(
            issued_ms=1,
            expires_ms=1001,
            limits=assignment().limits.model_copy(
                update={"runtime_seconds": 1, "lease_seconds": 1}
            ),
        )
        supervise_child(p, parent, a, KEYS["channel_key"], wall=lambda: 1)
        assert p.exitcode == -signal.SIGKILL
    finally:
        if p.is_alive():
            p.kill()
        p.join(2)
        p.close()
        parent.close()


def test_guardian_peer_authentication_and_replay():
    a = assignment()
    parent, child = multiprocessing.Pipe()
    guard = GuardianPeer(child, a, KEYS["channel_key"])
    pulse = Pulse(
        assignment=digest(a),
        nonce=a.nonce,
        monitor=a.monitor,
        sequence=1,
        challenge="f" * 64,
        action="challenge",
        issued_ms=100000,
    )
    doc = seal(pulse, KEYS["channel_key"], b"guardian-challenge")
    try:
        parent.send_bytes(encode(doc))
        guard.check()
        reply = open_envelope(
            Envelope.model_validate_json(parent.recv_bytes()),
            Pulse,
            KEYS["channel_key"],
            b"guardian-response",
        )
        assert reply.action == "continue"
        parent.send_bytes(encode(doc))
        with pytest.raises(Denied):
            guard.check()
    finally:
        parent.close()
        child.close()


def test_cli_default_has_no_effects_or_config_reads(monkeypatch, capsys):
    monkeypatch.setattr("tessera_monitor.cli.private_read", lambda *a: pytest.fail("read config"))
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out) == dict(
        state="disabled", effects=0, activation="blocked"
    )


def test_private_runtime_key_file_permissions_and_symlinks(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"a" * 32)
    key.chmod(0o600)
    assert private_read(key, 32) == b"a" * 32
    key.chmod(0o644)
    with pytest.raises(Denied):
        private_read(key, 32)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(OSError):
        private_read(link, 32)


def test_no_raw_pid_control_surface():
    assert not hasattr(Monitor, "start") and not hasattr(Monitor, "submit")
    assert "pid" not in Assignment.model_fields and "pid" not in Workload.model_fields
    assert os.name == "posix"  # existing journal lock is deliberately POSIX-only


@pytest.mark.parametrize("time_ms", [99999, 120000, 120001])
def test_initial_assignment_future_or_expired_is_rejected(tmp_path, time_ms):
    a = assignment()
    j = MonitorJournal(tmp_path / "state")
    adapter = FakeAdapter(a)
    try:
        with pytest.raises(Denied, match="expired or future"):
            Monitor(
                seal(a, KEYS["authority_key"], b"assignment"),
                identity=a.monitor,
                journal=j,
                adapter=adapter,
                wall=lambda: time_ms,
                **KEYS,
            )
        assert adapter.kills == 0 and j.documents() == []
    finally:
        j.close()


def test_duplicate_categories_cannot_hide_missing_gate(setup):
    m, _, adapter, _, _ = setup
    adapter.observe = lambda a: tuple(observed("identity", running=True) for _ in range(6))
    m.tick()
    assert m.stopped and adapter.kills == 1


def test_receiver_restart_does_not_reset_challenge_sequence(setup):
    m, r, _, clock, document = setup
    challenge = m.challenge()
    r.respond(challenge, authorized=lambda a: True)
    root = r.journal.root
    r.journal.close()
    reopened = MonitorJournal(root)
    try:
        receiver = MonitorReceiver(
            document,
            journal=reopened,
            tenant=m.a.tenant,
            project=m.a.project,
            monitor=m.a.monitor,
            now=clock.wall,
            **KEYS,
        )
        with pytest.raises(Denied, match="replayed"):
            receiver.respond(challenge, authorized=lambda a: True)
    finally:
        reopened.close()


def test_receiver_tenant_and_monitor_enrollment_is_not_request_claim(tmp_path):
    a = assignment()
    j = MonitorJournal(tmp_path / "receiver")
    try:
        with pytest.raises(Denied, match="enrollment"):
            MonitorReceiver(
                seal(a, KEYS["authority_key"], b"assignment"),
                journal=j,
                tenant="other-tenant",
                project=a.project,
                monitor=a.monitor,
                now=lambda: 100000,
                **KEYS,
            )
        assert not j.documents()
    finally:
        j.close()


def test_future_contracts_cannot_be_enabled():
    from tessera_monitor.targets import KubernetesMonitorTarget, SaladMonitorTarget

    for model in (KubernetesMonitorTarget, SaladMonitorTarget):
        with pytest.raises(ValidationError):
            model.model_validate({"enabled": True})


def test_disabled_example_config_is_valid_and_contains_no_keys():
    from pathlib import Path

    from tessera_monitor.cli import Config

    root = Path(__file__).resolve().parents[1]
    config = Config.model_validate_json((root / "deploy/monitor/docker.example.json").read_bytes())
    assert not config.enabled and not config.allow_host_socket
