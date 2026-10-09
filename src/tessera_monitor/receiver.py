"""Receiver transport loop for an explicitly enrolled monitor, not a public API.

The deployment supplies an already mutually authenticated, pinned TLS channel (or
a private inherited local socket), receiver ledger and a current-authority callback.
No operator claims or bearer owner credentials are accepted on this wire.
"""

from .protocol import Denied, Pulse, Receipt, open_envelope


def receive_one(channel, receiver, *, authorized):
    document = channel.receive()
    try:
        open_envelope(document, Pulse, receiver.channel_key, b"challenge")
    except Denied:
        # A failed challenge signature does not downgrade authentication: evidence
        # must independently verify under a different key and signing domain.
        open_envelope(document, Receipt, receiver.evidence_key, b"evidence")
        reply = receiver.ingest(document)
    else:
        reply = receiver.respond(document, authorized=authorized)
    channel.send(reply)
