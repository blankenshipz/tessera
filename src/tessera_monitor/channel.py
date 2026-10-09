"""Bounded framed transport: inherited local socket or explicitly pinned mutual TLS.

No discovery, redirects, proxy environment, insecure TLS fallback or retry. HMAC
documents remain mandatory over TLS. Deployment owns enrollment/key provisioning.
"""

import hashlib
import socket
import ssl
import struct
import time

from .protocol import MAX_FRAME, Denied, Envelope, encode


class Channel:
    def __init__(self, sock, *, timeout=0.5):
        self.sock, self.timeout = sock, timeout

    def send(self, document):
        raw = encode(document)
        self.sock.settimeout(self.timeout)
        self.sock.sendall(struct.pack("!I", len(raw)) + raw)

    def receive(self):
        deadline = time.monotonic() + self.timeout

        def read(size):
            result = bytearray()
            while len(result) < size:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Denied("channel deadline")
                self.sock.settimeout(left)
                part = self.sock.recv(size - len(result))
                if not part:
                    raise Denied("channel closed")
                result.extend(part)
            return bytes(result)

        size = struct.unpack("!I", read(4))[0]
        if not 0 < size <= MAX_FRAME:
            raise Denied("channel frame bound")
        try:
            return Envelope.model_validate_json(read(size))
        except Exception:
            raise Denied("invalid channel frame") from None

    def close(self):
        self.sock.close()


def connect_tls(*, address, port, server_name, ca, certificate, private_key, peer_sha256):
    # address is an explicit numeric IP: no unbounded resolver/ambient target.
    import ipaddress

    ipaddress.ip_address(address)
    context = ssl.create_default_context(cafile=ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # Never prompt for an encrypted key on a monitor thread or reconciliation CLI.
    context.load_cert_chain(certificate, private_key, password="")
    sock = socket.create_connection((address, port), timeout=0.5)
    try:
        tls = context.wrap_socket(sock, server_hostname=server_name)
        if hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() != peer_sha256:
            tls.close()
            raise Denied("TLS peer pin mismatch")
        return Channel(tls)
    except Exception:
        sock.close()
        raise Denied("monitor TLS unavailable") from None


def upload(journal, channel, assignment, key, *, after=0):
    from tessera_control.monitor_evidence import accept_ack

    for _ in range(32):
        batch = journal.documents(after)
        for document in batch:
            channel.send(document)
            accept_ack(journal, channel.receive(), assignment, key)
            after += 1
        if len(batch) < 32:
            break
    return after


def serve_monitor(monitor, channel, *, guard=lambda: None):
    """Runs only in the independent monitor process, never a broker callback thread."""
    after = 0
    try:
        while not monitor.stopped:
            guard()
            channel.send(monitor.challenge())
            monitor.accept(channel.receive())
            if not monitor.stopped:
                monitor.tick()
            after = upload(monitor.journal, channel, monitor.a, monitor.channel_key, after=after)
            time.sleep(0.5)
    except Exception:
        monitor.stop()
    finally:
        channel.close()
