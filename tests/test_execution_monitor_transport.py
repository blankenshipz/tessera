"""Isolated real sockets/processes, fake workload effects. No external endpoint."""

import multiprocessing
import os
import socket
import ssl
import time

import pytest
from test_execution_monitor import KEYS, FakeAdapter, assignment

from tessera_control.monitor_evidence import MonitorReceiver
from tessera_monitor.channel import Channel, connect_tls, serve_monitor, upload
from tessera_monitor.conformance import prerequisites
from tessera_monitor.journal import MonitorJournal
from tessera_monitor.protocol import Assignment, Denied, open_envelope, seal
from tessera_monitor.receiver import receive_one
from tessera_monitor.runtime import Monitor
from tessera_worker.local_docker_namespace import NamespaceUnknown


def _run_fake(root, document, sock, crash=False, ready=None, startup_delay=0):
    time.sleep(startup_delay)
    a = open_envelope(document, Assignment, KEYS["authority_key"], b"assignment")
    journal = MonitorJournal(root)
    try:
        m = Monitor(document, identity=a.monitor, journal=journal, adapter=FakeAdapter(a), **KEYS)
        if crash:
            os._exit(2)  # simulate loss after durable arming; lock released by OS
        if ready is not None:
            ready.set()  # imports, journal and durable arming completed in the child
        serve_monitor(m, Channel(sock, timeout=5))
    finally:
        journal.close()
        sock.close()


def live_clock_assignment():
    now = time.time_ns() // 1_000_000
    base = assignment()
    return assignment(
        issued_ms=now,
        expires_ms=now + 60000,
        limits=base.limits.model_copy(update={"runtime_seconds": 60, "lease_seconds": 10}),
    )


@pytest.mark.parametrize("startup_delay", [0, 2.5], ids=["normal", "slow-spawn"])
def test_separate_process_authenticated_channel_and_upload(tmp_path, startup_delay):
    a = live_clock_assignment()
    document = seal(a, KEYS["authority_key"], b"assignment")
    control = MonitorJournal(tmp_path / "control")
    receiver = MonitorReceiver(
        document,
        journal=control,
        tenant=a.tenant,
        project=a.project,
        monitor=a.monitor,
        now=lambda: time.time_ns() // 1_000_000,
        **KEYS,
    )
    left, right = socket.socketpair()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_run_fake,
        args=(tmp_path / "monitor", document, right, False, ready, startup_delay),
    )
    process.start()
    right.close()
    channel = Channel(left, timeout=5)
    try:
        # Spawn/import/fsync readiness is distinct from the framed-I/O deadline.
        # The slow-spawn case exceeds the old two-second receive budget on purpose.
        assert ready.wait(30), f"monitor did not arm; child exitcode={process.exitcode}"
        revoked = []

        def revoke(assigned):
            revoked.append(assigned)
            return False

        # Independent control explicitly revokes; monitor stops exact fake workload,
        # then uploads signed chain and consumes acknowledgements before exiting.
        while True:
            try:
                receive_one(channel, receiver, authorized=revoke)
            except Denied as error:
                assert str(error) == "channel closed"
                break
        process.join(5)
        assert process.exitcode == 0
        assert revoked == [a]  # real authenticated challenge reached control authority
        assert receiver.status()["termination_confirmed"]
        assert receiver.status()["cleanup_confirmed"]
        assert receiver.status()["activation"] == "blocked"
        assert {r.kind for r in receiver.records()} >= {"armed", "stop_intent", "terminal"}
    finally:
        if process.is_alive():
            process.kill()
        process.join(2)
        process.close()
        channel.close()
        control.close()


def test_actual_monitor_crash_stop_only_recovery(tmp_path):
    a = live_clock_assignment()
    document = seal(a, KEYS["authority_key"], b"assignment")
    left, right = socket.socketpair()
    process = multiprocessing.get_context("spawn").Process(
        target=_run_fake,
        args=(tmp_path / "monitor", document, right, True),
    )
    process.start()
    right.close()
    try:
        process.join(5)
        assert process.exitcode == 2
        j = MonitorJournal(tmp_path / "monitor")
        adapter = FakeAdapter(a)
        try:
            m = Monitor(
                document, identity=a.monitor, journal=j, adapter=adapter, recover=True, **KEYS
            )
            assert m.stopped and adapter.kills == adapter.cleanups == 1
            assert [r.kind for r in m.records][:2] == ["armed", "recovery"]
        finally:
            j.close()
    finally:
        if process.is_alive():
            process.kill()
        process.join(2)
        process.close()
        left.close()


def test_upload_replay_uses_same_receipts_and_no_effects(tmp_path):
    a = live_clock_assignment()
    document = seal(a, KEYS["authority_key"], b"assignment")
    monitor_journal, control_journal = (
        MonitorJournal(tmp_path / "m"),
        MonitorJournal(tmp_path / "c"),
    )
    adapter = FakeAdapter(a)
    try:
        m = Monitor(document, identity=a.monitor, journal=monitor_journal, adapter=adapter, **KEYS)
        receiver = MonitorReceiver(
            document,
            journal=control_journal,
            tenant=a.tenant,
            project=a.project,
            monitor=a.monitor,
            now=lambda: time.time_ns() // 1_000_000,
            **KEYS,
        )

        class LocalTransport:
            def send(self, doc):
                self.reply = receiver.ingest(doc)

            def receive(self):
                return self.reply

        m.stop()
        assert upload(m.journal, LocalTransport(), a, KEYS["channel_key"]) == len(m.records)
        assert upload(m.journal, LocalTransport(), a, KEYS["channel_key"]) == len(m.records)
        assert adapter.kills == adapter.cleanups == 1
    finally:
        monitor_journal.close()
        control_journal.close()


def test_tls_requires_certificate_hostname_pin_and_no_fallback(monkeypatch):
    events = []

    class FakeSocket:
        def close(self):
            events.append("closed")

        def getpeercert(self, binary_form):
            assert binary_form is True
            return b"synthetic-peer-certificate"

    sock = FakeSocket()

    class Context:
        def load_cert_chain(self, certificate, private_key, password):
            assert password == ""
            events.append((certificate, private_key))

        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "control.example"
            assert self.minimum_version == ssl.TLSVersion.TLSv1_3
            return sock

    def create_context(*, cafile):
        assert cafile == "pinned-ca"
        return Context()

    monkeypatch.setattr("tessera_monitor.channel.ssl.create_default_context", create_context)
    monkeypatch.setattr("tessera_monitor.channel.socket.create_connection", lambda *a, **k: sock)
    with pytest.raises(Denied, match="TLS unavailable"):
        connect_tls(
            address="127.0.0.1",
            port=443,
            server_name="control.example",
            ca="pinned-ca",
            certificate="monitor-cert-reference",
            private_key="monitor-key-reference",
            peer_sha256="f" * 64,
        )
    assert ("monitor-cert-reference", "monitor-key-reference") in events
    assert "closed" in events


def test_remote_target_requires_explicit_ip_no_ambient_dns():
    with pytest.raises(ValueError):
        connect_tls(
            address="ambient-hostname",
            port=443,
            server_name="control.example",
            ca="none",
            certificate="none",
            private_key="none",
            peer_sha256="f" * 64,
        )


def test_prerequisite_failure_never_invokes_docker(monkeypatch):
    monkeypatch.setattr("tessera_monitor.conformance.shutil.which", lambda _: "/fake/docker")

    def missing(self):
        raise NamespaceUnknown("daemon_host_namespace_observer_unavailable")

    monkeypatch.setattr("tessera_monitor.conformance.NativeNamespaceIdentity.preflight", missing)
    result = prerequisites()
    assert result["effects"] == 0 and result["state"] == "blocked"
    assert result["gate"] == "daemon_host_namespace_observer_unavailable"
