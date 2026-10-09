"""Opt-in two-process fixture supervision. No control/autonomy activation authority.

The guardian owns the supervisor child handle, not an arbitrary PID. Each process
can fence work if its peer dies. Daemon/kernel loss and simultaneous loss of both
remain unknown; no workload or uncertain stop is automatically retried.
"""

from __future__ import annotations

import hmac
import multiprocessing
import os
import secrets
import time

from .capability_effects import BackendBlocked
from .capability_journal import JournalConflict
from .local_docker_contract import authenticate, message
from .local_docker_engine import DockerCLI, LocalDockerEngine
from .local_docker_fixture import FixtureJournal, LocalFixtureAdapter, _child, terminate

PEER_LEASE = 6.0  # exceeds the bounded five-second, pre-effect Docker discovery
STOP_ALLOWANCE = 12.0  # absolute outer bound, never renewed by either peer


def channel_key(key, direction):
    return hmac.digest(key, b"tessera-local-guardian-v1\0" + direction, "sha256")


class GuardianLease:
    """Only one outstanding challenge: no pipe-fill hang or heartbeat backlog."""

    def __init__(self, connection, key, plan, *, clock=time.monotonic):
        self.connection, self.key, self.plan, self.clock = connection, key, plan, clock
        self.sequence = 1
        self.last = clock()
        self.armed = False

    def check(self):
        now = self.clock()
        if now < self.last or now - self.last >= PEER_LEASE:
            raise BackendBlocked("guardian lease expired")
        if self.connection.poll(2 if not self.armed else 0):
            raw = self.connection.recv_bytes(1024)
            action = authenticate(
                raw, channel_key(self.key, b"challenge"), self.plan, self.sequence
            )
            if action != "heartbeat":
                raise BackendBlocked("guardian challenge rejected")
            self.connection.send_bytes(
                message(channel_key(self.key, b"ack"), self.plan, self.sequence, "heartbeat")
            )
            self.last = self.clock()
            self.sequence += 1
            self.armed = True
        if not self.armed:
            raise BackendBlocked("guardian not armed")


def monitor_child(process, connection, key, plan, *, clock=time.monotonic):
    """No broker callbacks, arbitrary PIDs, rearm, or process resurrection.

    Only this single-threaded owner polls/reaps this child. An exited but unreaped
    child retains its PID; a reaped Process handle will not signal a recycled PID.
    The caller must join before acquiring the child's journal or touching Docker.
    """
    deadline = clock() + plan.runtime_seconds + STOP_ALLOWANCE
    last = clock()
    sequence = 1
    reason = "supervisor_exit"
    try:
        connection.send_bytes(message(channel_key(key, b"challenge"), plan, sequence, "heartbeat"))
        while process.is_alive():
            now = clock()
            if now < last or now >= deadline or now - last >= PEER_LEASE:
                reason = "supervisor_deadline_or_hang"
                break
            if connection.poll(0.05):
                if (
                    authenticate(
                        connection.recv_bytes(1024), channel_key(key, b"ack"), plan, sequence
                    )
                    != "heartbeat"
                ):
                    raise BackendBlocked("supervisor acknowledgement rejected")
                # Replies cannot renew an expired lease, including delayed I/O.
                now = clock()
                if now < last or now >= deadline or now - last >= PEER_LEASE:
                    reason = "supervisor_deadline_or_hang"
                    break
                last = now
                sequence += 1
                connection.send_bytes(
                    message(channel_key(key, b"challenge"), plan, sequence, "heartbeat")
                )
            process.join(timeout=0.05)
    except (EOFError, OSError, BackendBlocked):
        reason = "supervisor_channel_lost"
    finally:
        if process.is_alive():
            process.kill()  # SIGKILL, not SIGTERM; only our unreaped direct child
        process.join(timeout=2)
        connection.close()
    if process.is_alive():
        raise BackendBlocked("supervisor death unconfirmed; journal must not be adopted")
    return reason


def reap_interrupted(root, config, plan, reason, *, engine=None):
    """Stop-only historical authority, after child exit and exclusive lock acquisition.

    No automatic retry of a dispatched/uncertain stop. Repeated calls are reads.
    Explicit old-fixture recover remains a separate operator action, not used here.
    """
    if not config.enabled:
        raise BackendBlocked("explicit local fixture opt-in required")
    if reason not in {"supervisor_exit", "supervisor_deadline_or_hang", "supervisor_channel_lost"}:
        raise BackendBlocked("invalid guardian observation")
    journal = FixtureJournal(root)
    try:
        if journal.plan() != plan:
            raise JournalConflict("guardian operation identity mismatch")
        records = journal.records()
        if "guardian_observed" in records:
            return journal.report()
        journal.event("guardian_observed", dict(reason=reason, supervisor_exited=True))
        if "terminal" not in records:
            journal.event("terminal", dict(state="unknown", reason="guardian_intervention"))
        # The first supervisor may have died mid-kill or mid-removal. Do not issue
        # a duplicate effect simply because no reply was durably received.
        stop_dispatched = any(
            k in records for k in ("kill_pending", "stop_intent", "recovery_intent", "guard_intent")
        )
        if (
            not journal.report()["cleanup_confirmed"]
            and "create_intent" in records
            and not stop_dispatched
        ):
            image = records["image"]["observation"]["image_id"]
            cid = records.get("created", {}).get("observation", {}).get("container_id")
            # Unlike the legacy emergency stop helper, this first-attempt owner
            # requires durable intent before dispatch. Storage failure stays unknown.
            journal.event("guard_intent", dict(container_id=cid, image_id=image))
            engine = engine or LocalDockerEngine(DockerCLI(config, journal.root / "docker-config"))
            terminate(journal, engine, plan, image, cid, "guard")
        return journal.report()
    finally:
        journal.close()


def _guarded_child(root, plan, config, broker, broker_key, peer, peer_key):
    lease = GuardianLease(peer, peer_key, plan)
    try:
        lease.check()  # authenticated arm acknowledgement before any Docker effect
        _child(root, plan, config, broker, broker_key, guard=lease.check, preclaimed=True)
    finally:
        peer.close()


def _guardian(root, plan, config, broker, broker_key):
    os.setsid()
    # Durable one-use intent and guardian arming precede child creation. Close the
    # owner lock before transferring it to the child; never share SQLite handles.
    journal = FixtureJournal(root)
    try:
        journal.claim(plan)
        journal.event(
            "guardian_armed",
            dict(
                guardian_pid=os.getpid(),
                peer_lease_seconds=PEER_LEASE,
                absolute_seconds=plan.runtime_seconds + STOP_ALLOWANCE,
            ),
        )
    finally:
        journal.close()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    key = secrets.token_bytes(32)
    process = context.Process(
        target=_guarded_child, args=(root, plan, config, broker, broker_key, child, key)
    )
    process.start()
    child.close()
    broker.close()  # only child holds broker endpoint; broker death produces EOF
    reason = monitor_child(process, parent, key, plan)
    reap_interrupted(root, config, plan, reason)
    process.close()


class GuardedFixtureAdapter(LocalFixtureAdapter):
    """Explicit fixture seam. Inherited preflight always rejects real authority."""

    supervisor = staticmethod(_guardian)
