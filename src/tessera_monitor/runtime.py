"""Independent monitor state machine. It never submits, starts or resumes workloads."""

from __future__ import annotations

import secrets
import time
from typing import Protocol

from .protocol import (
    Assignment,
    Denied,
    Fact,
    Observation,
    Pulse,
    Receipt,
    bound,
    digest,
    open_envelope,
    seal,
)


class Adapter(Protocol):
    kind: str
    source: str

    def preflight(self, assignment: Assignment) -> None: ...
    def observe(self, assignment: Assignment) -> tuple[Observation, ...]: ...
    def kill(self, assignment: Assignment) -> None: ...
    def cleanup(self, assignment: Assignment) -> None: ...


class DisabledAdapter:
    """Kubernetes and Salad expose the same lifecycle but no enabled operations.

    Kubernetes must pin cluster/node boot/namespace/Job/Pod UIDs and CRI generation,
    use UID+resourceVersion delete preconditions, native CNI/cgroup/fs witnesses and
    per-operation RBAC. Salad needs allocation generation, native enforcement and
    independently observed provider cancellation/custody, not a provider status claim.
    """

    source = "execution-monitor"

    def __init__(self, kind):
        if kind not in {"kubernetes-v1", "salad-v1"}:
            raise Denied("unknown adapter")
        self.kind = kind

    def preflight(self, assignment):
        raise Denied("adapter disabled")

    observe = kill = cleanup = preflight


class Monitor:
    def __init__(
        self,
        document,
        *,
        identity,
        authority_key,
        channel_key,
        evidence_key,
        journal,
        adapter,
        wall=lambda: time.time_ns() // 1_000_000,
        clock=time.monotonic,
        recover=False,
        preclaimed=False,
    ):
        if len({authority_key, channel_key, evidence_key}) != 3 or any(
            type(k) is not bytes or len(k) != 32 for k in (authority_key, channel_key, evidence_key)
        ):
            raise Denied("distinct monitor keys required")
        a = open_envelope(document, Assignment, authority_key, b"assignment")
        if a.monitor != identity or a.adapter != adapter.kind:
            raise Denied("monitor identity or adapter mismatch")
        self.a, self.journal, self.adapter = a, journal, adapter
        self.channel_key, self.evidence_key = channel_key, evidence_key
        self.wall, self.clock = wall, clock
        self.start, self.last_clock, self.last_wall = clock(), clock(), wall()
        self.deadline = self.start + min(a.limits.runtime_seconds, (a.expires_ms - wall()) / 1000)
        self.lease = self.start + a.limits.lease_seconds
        self.pending = None
        self.pulse_sequence = 0
        self.stopped = False
        self.records = journal.receipts(evidence_key)
        for r in self.records:
            bound(r, a)
        if recover:
            if journal.assignment(authority_key) != a:
                raise Denied("recovery requires matching historical assignment")
            self.emit("recovery", (Observation(category="recovery", state="unknown"),))
            self.stop()
            return
        if not a.issued_ms <= wall() < a.expires_ms:
            raise Denied("assignment expired or future")
        adapter.preflight(a)
        fresh = journal.claim(document, a)
        if not fresh and not (preclaimed and not self.records):
            raise Denied("assignment consumed; stop-only recovery required")
        self.emit("armed")

    def emit(self, kind, observations=()):
        previous = self.journal.documents(len(self.records) - 1, 1) if self.records else []
        elapsed = max(0, int((self.clock() - self.start) * 1000))
        # Recovery spans a new clock epoch but cannot regress the receipt counter.
        if self.records:
            elapsed += self.records[0].elapsed_ms if self.records[0].kind == "recovery" else 0
            elapsed = max(elapsed, self.records[-1].elapsed_ms)
        receipt = Receipt(
            assignment=digest(self.a),
            nonce=self.a.nonce,
            monitor=self.a.monitor,
            sequence=len(self.records) + 1,
            previous=digest(previous[0]) if previous else "0" * 64,
            observed_ms=self.wall(),
            elapsed_ms=elapsed,
            kind=kind,
            source=self.adapter.source,
            observations=observations,
        )
        self.journal.append(seal(receipt, self.evidence_key, b"evidence"), receipt)
        self.records.append(receipt)
        return receipt

    def alive(self):
        now, wall = self.clock(), self.wall()
        valid = (
            not self.stopped
            and self.last_clock <= now < min(self.deadline, self.lease)
            and self.last_wall <= wall < self.a.expires_ms
        )
        self.last_clock, self.last_wall = now, wall
        return valid

    def challenge(self):
        if not self.alive() or self.pending is not None:
            raise Denied("challenge unavailable")
        self.pending = Pulse(
            assignment=digest(self.a),
            nonce=self.a.nonce,
            monitor=self.a.monitor,
            sequence=self.pulse_sequence + 1,
            challenge=secrets.token_hex(32),
            action="challenge",
            issued_ms=self.wall(),
        )
        return seal(self.pending, self.channel_key, b"challenge")

    def accept(self, document):
        try:
            response = open_envelope(document, Pulse, self.channel_key, b"response")
            if (
                not self.alive()
                or self.pending is None
                or response.action not in {"continue", "revoke"}
                or response != self.pending.model_copy(update={"action": response.action})
            ):
                raise Denied("heartbeat rejected")
            self.pending = None
            self.pulse_sequence = response.sequence
            if response.action == "revoke":
                self.stop()
                return
            self.emit(
                "heartbeat",
                (
                    Observation(
                        category="watchdog",
                        state="verified",
                        facts=(Fact(name="sequence", value=response.sequence),),
                    ),
                ),
            )
            if not self.alive():
                raise Denied("heartbeat expired during persistence")
            self.lease = min(self.deadline, self.clock() + self.a.limits.lease_seconds)
        except Exception:
            self.stop()
            raise Denied("heartbeat rejected") from None

    def tick(self):
        if not self.alive():
            self.stop()
            return
        try:
            observations = self.adapter.observe(self.a)
            self.emit("observation", observations)
            active = [
                o
                for o in observations
                if o.category
                in {
                    "identity",
                    "namespace",
                    "network",
                    "resources",
                    "filesystem",
                    "custody",
                }
            ]
            if len(active) != 6 or any(o.state != "verified" for o in active) or not self.alive():
                self.stop()
        except Exception:
            self.stop()

    def stop(self):
        """Historical exact-owned stop only. Intent consumes each destructive attempt.

        A lost kill/remove reply is reconciled by reads, never automatic redispatch.
        Storage failure prevents claiming a new effect and stays unknown.
        """
        if self.stopped:
            return
        self.stopped = True
        self.emit("unknown")
        try:
            kinds = {r.kind for r in self.records}
            if "stop_intent" not in kinds:
                self.emit("stop_intent")
                self.adapter.kill(self.a)
            observations = self.adapter.observe(self.a)
            self.emit("observation", observations)
            terminated = any(
                o.category == "termination" and o.state == "verified" for o in observations
            )
            if terminated and "cleanup_intent" not in kinds:
                self.emit("cleanup_intent")
                self.adapter.cleanup(self.a)
            self.emit("terminal", self.adapter.observe(self.a))
        except Exception:
            self.emit("unknown")
