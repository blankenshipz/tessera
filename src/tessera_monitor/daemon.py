"""Two-process execution-plane service. Guardian never shares an open journal.

The parent owns only its unreaped direct child. After confirmed child death it
acquires the journal lock and performs first-stop/read-only reconciliation. Both
processes are separate from the broker/control/agent. Killing both or the host is
NOT survivable; receipt state remains unknown until independent reconciliation.
"""

import multiprocessing
import time

from .protocol import Assignment, Denied, Envelope, Pulse, bound, digest, open_envelope, seal


def supervise_child(
    process,
    peer,
    assignment,
    key,
    *,
    clock=time.monotonic,
    wall=lambda: time.time_ns() // 1_000_000,
):
    deadline = clock() + min(
        assignment.limits.runtime_seconds, max(0, (assignment.expires_ms - wall()) / 1000)
    )
    last = clock()
    sequence = 1
    import secrets

    def challenge():
        return Pulse(
            assignment=digest(assignment),
            nonce=assignment.nonce,
            monitor=assignment.monitor,
            sequence=sequence,
            challenge=secrets.token_hex(32),
            action="challenge",
            issued_ms=wall(),
        )

    request = challenge()
    try:
        peer.send_bytes(seal(request, key, b"guardian-challenge").model_dump_json().encode())
        while process.is_alive():
            now = clock()
            if (
                now < last
                or now >= deadline
                or now - last >= assignment.limits.lease_seconds
                or wall() >= assignment.expires_ms
            ):
                break
            if peer.poll(0.05):
                response = open_envelope(
                    Envelope.model_validate_json(peer.recv_bytes(4096)),
                    Pulse,
                    key,
                    b"guardian-response",
                )
                if response != request.model_copy(update={"action": "continue"}):
                    raise Denied("guardian response rejected")
                now = clock()
                if now >= deadline or now - last >= assignment.limits.lease_seconds:
                    break
                last = now
                sequence += 1
                request = challenge()
                peer.send_bytes(
                    seal(request, key, b"guardian-challenge").model_dump_json().encode()
                )
            process.join(timeout=0.01)
    except Exception:
        pass  # No diagnostic text/secret-bearing input in evidence.
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=2)
        peer.close()
    if process.is_alive():
        raise Denied("monitor death unknown; cannot adopt journal")


class GuardianPeer:
    def __init__(self, peer, a, key):
        self.peer, self.a, self.key, self.sequence = peer, a, key, 0

    def check(self):
        if not self.peer.poll(0.5):
            raise Denied("guardian unavailable")
        pulse = open_envelope(
            Envelope.model_validate_json(self.peer.recv_bytes(4096)),
            Pulse,
            self.key,
            b"guardian-challenge",
        )
        bound(pulse, self.a)
        if pulse.action != "challenge" or pulse.sequence != self.sequence + 1:
            raise Denied("guardian challenge rejected")
        self.sequence = pulse.sequence
        self.peer.send_bytes(
            seal(pulse.model_copy(update={"action": "continue"}), self.key, b"guardian-response")
            .model_dump_json()
            .encode()
        )


def _monitor_child(config, document, keys, peer):
    from .channel import connect_tls, serve_monitor
    from .cli import adapter_for
    from .journal import MonitorJournal
    from .runtime import Monitor

    journal = MonitorJournal(config.journal)
    try:
        monitor = Monitor(
            document,
            identity=config.identity,
            journal=journal,
            adapter=adapter_for(config),
            preclaimed=True,
            **keys,
        )
        try:
            channel = connect_tls(**config.tls.model_dump())
            guard = GuardianPeer(peer, monitor.a, keys["channel_key"])
            serve_monitor(monitor, channel, guard=guard.check)
        except Exception:
            monitor.stop()
    finally:
        journal.close()
        peer.close()


def run_guarded(config, document, keys):
    from .cli import adapter_for
    from .journal import MonitorJournal
    from .runtime import Monitor

    a = open_envelope(document, Assignment, keys["authority_key"], b"assignment")
    if (
        a.monitor != config.identity
        or not a.issued_ms <= time.time_ns() // 1_000_000 < a.expires_ms
    ):
        raise Denied("monitor enrollment or expiry mismatch")
    adapter_for(config).preflight(a)
    journal = MonitorJournal(config.journal)
    try:
        if not journal.claim(document, a):
            raise Denied("assignment consumed; use stop-only recovery")
    finally:
        journal.close()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_monitor_child, args=(config, document, keys, child))
    try:
        process.start()
        child.close()
        supervise_child(process, parent, a, keys["channel_key"])
    except Exception:
        if process.pid is not None and process.is_alive():
            process.kill()
            process.join(timeout=2)
    finally:
        child.close()
        parent.close()
        # Never acquire custody while a child may still have an in-flight effect.
        if process.pid is not None and process.is_alive():
            raise Denied("monitor death unknown")
    journal = MonitorJournal(config.journal)
    try:
        Monitor(
            document,
            identity=config.identity,
            journal=journal,
            adapter=adapter_for(config),
            recover=True,
            **keys,
        )
    finally:
        journal.close()
        if process.pid is not None:
            process.close()
