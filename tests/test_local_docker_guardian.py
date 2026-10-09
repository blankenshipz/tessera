"""Isolated process/protocol/journal regressions. Never contacts Docker/Kubernetes."""

import importlib.util
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest
from test_local_docker_fixture import CID, IMAGE_ID, KEY, FakeEngine, plan

from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.capability_journal import JournalConflict
from tessera_worker.local_docker_contract import LocalFixtureConfig, message
from tessera_worker.local_docker_fixture import FixtureJournal, supervise
from tessera_worker.local_docker_guardian import (
    PEER_LEASE,
    GuardedFixtureAdapter,
    GuardianLease,
    channel_key,
    monitor_child,
    reap_interrupted,
)


def interrupted(root, p, *, stop=False):
    journal = FixtureJournal(root)
    journal.claim(p)
    journal.event("guardian_armed", {"test_only": True})
    journal.event("image", {"image_id": IMAGE_ID})
    journal.event("create_intent", {"container_name": p.name, "image_id": IMAGE_ID})
    journal.event("created", {"container_id": CID})
    journal.event("start_intent", {"container_id": CID})
    if stop:
        journal.event("stop_intent", {"container_id": CID, "image_id": IMAGE_ID})
    journal.close()


def test_default_adapter_and_cli_zero_effects(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("default performed an effect")

    monkeypatch.setattr(multiprocessing, "get_context", forbidden)
    adapter = GuardedFixtureAdapter()
    with pytest.raises(BackendBlocked):
        adapter.launch(plan(), tmp_path / "no-state")
    with pytest.raises(BackendBlocked):
        adapter.preflight(object())
    spec = importlib.util.spec_from_file_location(
        "sandbox_cli", Path(__file__).parents[1] / "scripts/check_sandbox_supervision.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "run_once", forbidden)
    assert module.main(["--image", "not-an-image", "--evidence-dir", "/does/not/exist"]) == 0
    assert '"effects": 0' in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("reason", ["supervisor_exit", "supervisor_deadline_or_hang"])
def test_supervisor_dies_or_hangs_stop_only_after_expiry(tmp_path, reason):
    p = plan(mode="wait", runtime_seconds=3)
    root = tmp_path / "receipt"
    interrupted(root, p)
    engine = FakeEngine(p)
    # No current workload authority supplied: historical identity authorizes stop only.
    result = reap_interrupted(root, LocalFixtureConfig(enabled=True), p, reason, engine=engine)
    assert result["state"] == "unknown"
    assert result["termination_confirmed"] and result["cleanup_confirmed"]
    assert engine.calls.count("kill") == engine.calls.count("cleanup") == 1
    assert not {"create", "start", "go"}.intersection(engine.calls)
    assert (
        reap_interrupted(root, LocalFixtureConfig(enabled=True), p, reason, engine=engine) == result
    )
    assert engine.calls.count("kill") == 1


@pytest.mark.parametrize("fault", ["kill", "cleanup"])
def test_guard_lost_reply_never_retries(tmp_path, fault):
    p, root = plan(), tmp_path / "receipt"
    interrupted(root, p)
    engine = FakeEngine(p, fault)
    config = LocalFixtureConfig(enabled=True)
    result = reap_interrupted(root, config, p, "supervisor_exit", engine=engine)
    calls = list(engine.calls)
    assert result["state"] == "unknown" and not result["cleanup_confirmed"]
    assert reap_interrupted(root, config, p, "supervisor_exit", engine=engine) == result
    assert calls == engine.calls


@pytest.mark.parametrize("intent", ["kill_pending", "stop_intent"])
def test_uncertain_prior_kill_not_redispatched(tmp_path, intent):
    p, root = plan(), tmp_path / "receipt"
    interrupted(root, p)
    journal = FixtureJournal(root)
    journal.event(intent, {"state": "unknown"})
    journal.close()
    engine = FakeEngine(p)
    result = reap_interrupted(
        root, LocalFixtureConfig(enabled=True), p, "supervisor_exit", engine=engine
    )
    assert result["state"] == "unknown" and not result["termination_confirmed"]
    assert engine.calls == []


def test_guarded_failed_stop_claim_defers_first_kill_not_duplicate(tmp_path, monkeypatch):
    p, root = plan(mode="wait"), tmp_path / "receipt"
    journal = FixtureJournal(root)
    journal.claim(p)
    event = journal.event

    def fail_claim(key, *args, **kwargs):
        if key == "kill_pending":
            raise OSError("synthetic durable-state failure")
        return event(key, *args, **kwargs)

    monkeypatch.setattr(journal, "event", fail_claim)
    engine = FakeEngine(p)
    try:
        with pytest.raises(OSError):
            supervise(p, journal, engine, lambda _: "lost", preclaimed=True)
        assert "kill" not in engine.calls
    finally:
        journal.close()
    result = reap_interrupted(
        root, LocalFixtureConfig(enabled=True), p, "supervisor_exit", engine=engine
    )
    assert result["state"] == "unknown" and result["cleanup_confirmed"]
    assert engine.calls.count("kill") == 1


def test_guard_does_not_adopt_other_operation_or_live_lock(tmp_path):
    p, root = plan(), tmp_path / "receipt"
    interrupted(root, p)
    engine = FakeEngine(p)
    config = LocalFixtureConfig(enabled=True)
    with pytest.raises(JournalConflict):
        reap_interrupted(root, config, plan(nonce="3" * 64), "supervisor_exit", engine=engine)
    journal = FixtureJournal(root)
    try:
        with pytest.raises(JournalConflict):
            reap_interrupted(root, config, p, "supervisor_exit", engine=engine)
    finally:
        journal.close()
    assert engine.calls == []


def test_preclaimed_plan_cannot_start_twice(tmp_path):
    p = plan()
    journal = FixtureJournal(tmp_path / "receipt")
    journal.claim(p)
    engine = FakeEngine(p)
    try:
        supervise(p, journal, engine, lambda _: "heartbeat", preclaimed=True)
        calls = list(engine.calls)
        with pytest.raises(JournalConflict):
            supervise(p, journal, engine, lambda _: "heartbeat", preclaimed=True)
        assert engine.calls == calls
    finally:
        journal.close()


class Link:
    def __init__(self, incoming):
        self.incoming, self.sent = list(incoming), []

    def poll(self, timeout=0):
        return bool(self.incoming)

    def recv_bytes(self, limit):
        value = self.incoming.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def send_bytes(self, data):
        self.sent.append(data)


@pytest.mark.parametrize("fault", ["replay", "wrong_nonce", "wrong_key", "death", "cancel"])
def test_guardian_authentication_and_loss(fault):
    p = plan()
    correct = message(channel_key(KEY, b"challenge"), p, 1, "heartbeat")
    invalid = {
        "replay": correct,
        "wrong_nonce": message(
            channel_key(KEY, b"challenge"), plan(nonce="3" * 64), 2, "heartbeat"
        ),
        "wrong_key": message(KEY, p, 2, "heartbeat"),
        "death": EOFError(),
        "cancel": message(channel_key(KEY, b"challenge"), p, 2, "cancel"),
    }[fault]
    link = Link([correct, invalid])
    lease = GuardianLease(link, KEY, p)
    lease.check()
    assert link.sent == [message(channel_key(KEY, b"ack"), p, 1, "heartbeat")]
    with pytest.raises((BackendBlocked, EOFError)):
        lease.check()
    assert len(link.sent) == 1


@pytest.mark.parametrize("elapsed", [-1, PEER_LEASE, PEER_LEASE + 1])
def test_stale_queued_heartbeat_cannot_renew_expired_lease(elapsed):
    p, clock = plan(), [10.0]
    link = Link([message(channel_key(KEY, b"challenge"), p, 1, "heartbeat")])
    lease = GuardianLease(link, KEY, p, clock=lambda: clock[0])
    clock[0] += elapsed
    with pytest.raises(BackendBlocked):
        lease.check()
    assert not link.sent


def _ignore_term(peer):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    peer.send_bytes(b"ready")
    while True:
        time.sleep(0.1)


def _exit(peer):
    peer.close()


def test_independent_monitor_force_kills_hung_sigterm_ignoring_child():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_ignore_term, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv_bytes() == b"ready"
        process.terminate()  # genuine ignored SIGTERM, not a fake signal result
        process.join(timeout=0.1)
        assert process.is_alive()
        # Accelerate only the observer's clock; child is actually hung/unkilled.
        ticks = iter([0, 0, PEER_LEASE + 1])
        reason = monitor_child(process, parent, KEY, plan(), clock=lambda: next(ticks))
        assert reason == "supervisor_deadline_or_hang"
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
        process.join(2)
        parent.close()
        process.close()


def test_dead_child_handle_never_signals_reused_pid(monkeypatch):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_exit, args=(child,))
    process.start()
    child.close()
    process.join(5)
    assert not process.is_alive()

    def forbidden(*args):
        pytest.fail("reaped child PID must never be signalled")

    monkeypatch.setattr(os, "kill", forbidden)
    try:
        monitor_child(process, parent, KEY, plan())
    finally:
        parent.close()
        process.close()


def _fake_supervisor(root, p, config, broker, broker_key, peer, peer_key):
    # Injection is local to this isolated test process; production has no fake flag.
    from tessera_worker import local_docker_fixture as fixture
    from tessera_worker.local_docker_guardian import _guarded_child

    fixture.DockerCLI = lambda *args: None
    fixture.LocalDockerEngine = lambda run: FakeEngine(p)
    _guarded_child(root, p, config, broker, broker_key, peer, peer_key)


def _hung_supervisor(root, p, config, broker, broker_key, peer, peer_key):
    lease = GuardianLease(peer, peer_key, p)
    lease.check()
    journal = FixtureJournal(root)
    journal.event("image", {"image_id": IMAGE_ID})
    journal.event("create_intent", {"container_name": p.name, "image_id": IMAGE_ID})
    journal.event("created", {"container_id": CID})
    journal.event("start_intent", {"container_id": CID})
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Readiness is sent only after authenticated arming, durable exact identity,
    # exclusive owner lock acquisition and installation of the hung-owner behavior.
    broker.send_bytes(b"hung-owner-ready")
    while True:
        time.sleep(0.1)  # Holds journal lock; cannot execute cleanup callbacks.


def _fake_guardian(root, p, config, broker, key, child_reaped):
    from tessera_worker import local_docker_guardian as guardian

    monitor = guardian.monitor_child

    def observe_reaped(process, connection, peer_key, plan):
        try:
            return monitor(process, connection, peer_key, plan)
        finally:
            # The production monitor owns this direct child handle and performs
            # kill/join itself. Notify teardown only after death is confirmed.
            if not process.is_alive():
                child_reaped.set()

    guardian._guarded_child = _hung_supervisor
    guardian.monitor_child = observe_reaped
    guardian.DockerCLI = lambda *args: None
    guardian.LocalDockerEngine = lambda run: FakeEngine(p)
    guardian._guardian(root, p, config, broker, key)


@pytest.mark.parametrize("lost", ["guardian", "broker"])
def test_real_pipe_peer_death_stops_fake_workload(tmp_path, lost):
    p, root = plan(mode="wait"), tmp_path / "receipt"
    journal = FixtureJournal(root)
    journal.claim(p)
    journal.close()
    context = multiprocessing.get_context("spawn")
    broker, supervisor_broker = context.Pipe()
    guardian, supervisor_peer = context.Pipe()
    process = context.Process(
        target=_fake_supervisor,
        args=(
            root,
            p,
            LocalFixtureConfig(enabled=True),
            supervisor_broker,
            KEY,
            supervisor_peer,
            KEY,
        ),
    )
    process.start()
    supervisor_broker.close()
    supervisor_peer.close()
    try:
        guardian.send_bytes(message(channel_key(KEY, b"challenge"), p, 1, "heartbeat"))
        assert guardian.poll(5)
        guardian.recv_bytes()
        if lost == "guardian":
            guardian.close()
        else:
            broker.close()
        process.join(5)
        assert not process.is_alive() and process.exitcode == 0
        journal = FixtureJournal(root)
        try:
            result = journal.report()
            assert result["state"] == "unknown"
            assert result["termination_confirmed"] and result["cleanup_confirmed"]
        finally:
            journal.close()
    finally:
        if process.is_alive():
            process.kill()
        process.join(2)
        process.close()
        guardian.close()
        broker.close()


def test_real_guardian_kills_hung_owner_then_takes_lock_for_exact_stop(tmp_path):
    p, root = plan(mode="wait"), tmp_path / "receipt"
    context = multiprocessing.get_context("spawn")
    broker, child = context.Pipe()
    child_reaped = context.Event()
    process = context.Process(
        target=_fake_guardian,
        args=(root, p, LocalFixtureConfig(enabled=True), child, KEY, child_reaped),
    )
    process.start()
    child.close()
    try:
        # Separate bounded spawn/import scheduling from the stop deadline. This
        # one-way readiness message does not tick/renew either guardian lease.
        assert broker.poll(30), "hung owner did not reach its authenticated locked startup boundary"
        assert broker.recv_bytes() == b"hung-owner-ready"
        # Broker does not tick or assist; independently running guardian owns time.
        process.join(12)
        assert not process.is_alive() and process.exitcode == 0
        assert child_reaped.is_set()
        journal = FixtureJournal(root)
        try:
            report = journal.report()
            assert report["state"] == "unknown"
            assert report["cleanup_confirmed"] and report["termination_confirmed"]
            assert report["records"]["guardian_observed"]["observation"]["reason"] == (
                "supervisor_deadline_or_hang"
            )
            assert "guard_intent" in report["records"]
            assert "stop_intent" not in report["records"]
        finally:
            journal.close()
    finally:
        # Do not kill the guardian while it still owns an unreaped child. The
        # independently bounded monitor must first confirm that exact child dead.
        if process.is_alive():
            assert child_reaped.wait(12), "guardian still owns a child; refuse orphaning cleanup"
            process.kill()
        process.join(2)
        process.close()
        broker.close()
