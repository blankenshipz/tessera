"""Control's own read of the guardian's per-frame record (ADR 0041 sections 2 and 7).

A billable frame settles on a guardian verdict that *control* read over the authenticated
status channel, not on what the executor reported. ``GuardianRecordReader.read`` returns the
guardian's ``frame_record`` for one reservation after a closed-shape check, or ``None`` when the
guardian cannot be reached or answers anything else. It never raises, so the settlement path
treats every failure the same way: no verdict, the hold stays.

The HTTPS reader uses the same pinned, authenticated admin channel as the executor's guardian
status read. Its closed config file is an owner-only (0600) JSON document naming the endpoint and
the files that hold the token and CA; no secret value lives in it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

VERDICTS = frozenset({"open", "stopped", "reconciled", "absent"})
FRAME_RECORD_KEYS = frozenset({"reservation", "verdict", "sequence", "record_digest"})
CONFIG_SCHEMA = "tessera.salad.guardian-read.v1"
_CONFIG_KEYS = frozenset({"schema", "host", "port", "token_file", "ca_file", "certificate_sha256"})
_HEX = frozenset("0123456789abcdef")


class GuardianRecordReader(Protocol):
    def read(self, reservation: str) -> dict[str, Any] | None:
        """The validated ``frame_record`` for ``reservation``, or None. Never raises."""


def validate_frame_record(value: object, reservation: str) -> dict[str, Any] | None:
    """The closed ``frame_record`` shape for exactly this reservation, else None."""
    if (
        type(value) is not dict
        or set(value) != FRAME_RECORD_KEYS
        or value["reservation"] != reservation
        or value["verdict"] not in VERDICTS
        or type(value["sequence"]) is not int
        or value["sequence"] < 0
        or type(value["record_digest"]) is not str
        or len(value["record_digest"]) != 64
        or set(value["record_digest"]) - _HEX
    ):
        return None
    return dict(value)


class HttpsGuardianRecordReader:
    """Reads ``frame_record`` from the guardian's status reply over a pinned TLS client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def read(self, reservation: str) -> dict[str, Any] | None:
        try:
            reply = self._client._call({"operation": "status", "reservation": reservation})
        except Exception:
            return None
        if type(reply) is not dict:
            return None
        return validate_frame_record(reply.get("frame_record"), reservation)


def load_reader_config(path: Path | str) -> dict[str, Any]:
    """The closed, validated reader config; ``ValueError`` (no value echoed) otherwise."""
    from .salad_live_check import _private_bytes

    try:
        document = json.loads(_private_bytes(path, 16384))
    except (OSError, ValueError):
        raise ValueError("the guardian read config is unavailable or not private JSON") from None
    if type(document) is not dict or set(document) != _CONFIG_KEYS:
        raise ValueError("the guardian read config must have exactly its documented keys")
    host, port, digest = document["host"], document["port"], document["certificate_sha256"]
    if (
        document["schema"] != CONFIG_SCHEMA
        or type(host) is not str
        or not host
        or any(c in host for c in "/:@\\\r\n ")
        or type(port) is not int
        or port != 443
        or type(digest) is not str
        or len(digest) != 64
        or set(digest) - _HEX
        or any(
            type(document[name]) is not str or not document[name].startswith("/")
            for name in ("token_file", "ca_file")
        )
    ):
        raise ValueError("the guardian read config has an invalid value")
    return {name: document[name] for name in _CONFIG_KEYS - {"schema"}}


def build_https_reader(path: Path | str) -> HttpsGuardianRecordReader:
    """The reader a config file describes (no connection is made until the first read)."""
    from .salad_server import SaladSafetyHTTPSClient

    fields = load_reader_config(path)
    return HttpsGuardianRecordReader(SaladSafetyHTTPSClient(**fields))
