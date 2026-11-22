"""Guardian-held input delivery for a Salad workload (ADR 0036 phase 2, ADR 0037 s3).

The provider input ticket is a bearer secret.  It is minted by the trusted server (which holds
the worker identity) and handed to the guardian on the authenticated admin channel.  It is
held in guardian memory only: never persisted, never placed in the Salad profile, env or argv,
never returned by any endpoint or status.  The workload receives only verified bytes.

The guardian fetches from control ``/provider-inputs/*`` (loopback HTTP or HTTPS, the same
client constraints as ``tessera_worker.provider_inputs``), then verifies the manifest digest
against the route's approved ``manifests.data`` (re-derived from the manifest), the chunk
index, every chunk's sha256 and size, and the total byte bounds.  One chunk is in memory at a
time; nothing is written to disk.

Single-threaded by design: the guardian's ``SafetyHTTPServer`` is a plain ``HTTPServer`` whose
handlers, admin calls and loop tick all run on one thread, so the session table, the
``bytes_served`` budget and the failure counters are never touched concurrently and take no
lock.  Any threaded caller must add one.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

from tessera_worker.chunked_inputs import (
    MAX_BYTES,
    _decode_chunk,
    _verify_index,
    _verify_manifest,
)
from tessera_worker.provider_inputs import (
    MAX_CHUNK_RESPONSE_BYTES,
    MAX_MANIFEST_RESPONSE_BYTES,
    TIMEOUT_SECONDS,
    _default_open,
    _get,
    _origin,
)

from .salad_workload_contract import INPUT_RANGE_BYTES, INPUT_REPLY_BYTES
from .store import _parse_utc

REJECTED = "workload_rejected"
TICKET_KEYS = {"token", "expires_at", "snapshot_id", "snapshot_digest"}
# A retried chunk is legitimate; an unbounded re-read is not.
MAX_SERVED_FACTOR = 2
# Failed control fetches are bounded so a workload cannot spin on a slow or failing control.
MAX_FAILED_PER_ITEM = 3
MAX_FAILED_PER_TICKET = 12
# A reply cut off on the wire is refunded (the workload retries the range); refunds are
# bounded so a client that never finishes reading cannot re-read without limit.
MAX_REFUNDS_PER_TICKET = 16
# Counters carried across a ticket drop and re-install (expiry, revoke) per operation,
# so a new ticket for the same snapshot never resets the served and refund budgets.
MAX_CARRIED_OPERATIONS = 256


class _Session:
    def __init__(self, ticket: dict[str, Any], expires_at: float, max_bytes: int, deadline: Any):
        self.token: str | None = ticket["token"]
        self.expires_at = expires_at
        self.snapshot_id: str = ticket["snapshot_id"]
        self.digest: str = ticket["snapshot_digest"]
        self.max_bytes = max_bytes
        # Reservation stop deadline: a float, or a callable re-read on every fetch because the
        # stop deadline shrinks once the job is running.
        self.deadline = deadline
        self.snapshot: dict[str, Any] | None = None  # verified manifest + index
        self.chunks_served = 0
        self.bytes_served = 0
        self.failures: dict[Any, int] = {}
        # One verified stored chunk (seq, bytes): ranges of the same chunk are served
        # without another control fetch. Bounded to one 4 MiB chunk per operation.
        self.cached: tuple[int, bytes] | None = None
        self.refunds = 0


class GuardianInputs:
    def __init__(
        self,
        origin: str,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
        fetch_timeout: float = TIMEOUT_SECONDS,
        fetch_budget: Callable[[float], Any] | None = None,
    ):
        self.base = _origin(origin)
        self.opener = opener or _default_open
        self.clock = clock
        # Socket timeout for one control fetch; the guardian passes its request budget.
        self.fetch_timeout = fetch_timeout
        # Optional wall-clock bound around a whole control fetch (the guardian passes its
        # main-thread alarm), so a slow-drip control cannot exceed the timeout in total.
        self.fetch_budget = fetch_budget or (lambda _seconds: contextlib.nullcontext())
        self._sessions: dict[str, _Session] = {}
        # operation -> (snapshot_id, digest, chunks_served, bytes_served, refunds, failures)
        self._carried: dict[str, tuple] = {}

    # ---- admin side --------------------------------------------------------------------
    def install(
        self,
        operation: str,
        ticket: Any,
        *,
        expected_digest: str,
        max_bytes: int,
        deadline: float | Callable[[], float],
    ) -> None:
        """Hold (or replace) the operation's ticket; ``None`` drops it."""
        if ticket is None:
            self.drop(operation)
            return
        if type(ticket) is not dict or set(ticket) != TICKET_KEYS:
            raise ValueError("invalid_input_ticket")
        token = ticket["token"]
        if (
            type(token) is not str
            or not 16 <= len(token) <= 512
            or any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in token)
            or type(ticket["snapshot_id"]) is not str
            or not ticket["snapshot_id"]
            or ticket["snapshot_digest"] != expected_digest
            or type(ticket["expires_at"]) is not str
        ):
            raise ValueError("invalid_input_ticket")
        expires = _parse_utc(ticket["expires_at"]).timestamp()
        if expires <= self.clock() or self._deadline_of(deadline) <= self.clock():
            raise ValueError("invalid_input_ticket")
        fresh = _Session(ticket, expires, max_bytes, deadline)
        old = self._sessions.get(operation)
        if old is not None and (old.snapshot_id, old.digest) == (fresh.snapshot_id, fresh.digest):
            # Refresh: same snapshot, new bearer.  Verified metadata and counters carry over.
            fresh.snapshot = old.snapshot
            fresh.chunks_served, fresh.bytes_served = old.chunks_served, old.bytes_served
            fresh.failures = old.failures
            fresh.cached, fresh.refunds = old.cached, old.refunds
        else:
            carried = self._carried.get(operation)
            if carried is not None and carried[:2] == (fresh.snapshot_id, fresh.digest):
                # Re-install after a drop (expiry or revoke): budgets continue, never reset.
                (_, _, fresh.chunks_served, fresh.bytes_served, fresh.refunds,
                 fresh.failures) = carried  # fmt: skip
        self._sessions[operation] = fresh

    def drop(self, operation: str) -> None:
        session = self._sessions.pop(operation, None)
        if session is not None:
            self._carried.pop(operation, None)
            self._carried[operation] = (
                session.snapshot_id, session.digest, session.chunks_served,
                session.bytes_served, session.refunds, dict(session.failures),
            )  # fmt: skip
            while len(self._carried) > MAX_CARRIED_OPERATIONS:
                self._carried.pop(next(iter(self._carried)))
            session.token = None
            session.snapshot = None
            session.cached = None

    def status(self, operation: str) -> dict[str, Any]:
        """Metadata only; the token is never reported."""
        session = self._sessions.get(operation)
        if session is None:
            return dict(ticket_held=False)
        return dict(
            ticket_held=self.clock() < session.expires_at,
            expires_at=session.expires_at,
            snapshot_id=session.snapshot_id,
            snapshot_digest=session.digest,
            chunks_served=session.chunks_served,
            bytes_served=session.bytes_served,
        )

    # ---- workload side -----------------------------------------------------------------
    def _live(self, operation: str) -> _Session:
        session = self._sessions.get(operation)
        if session is None or session.token is None:
            raise ValueError(REJECTED)
        if self.clock() >= session.expires_at:
            self.drop(operation)
            raise ValueError(REJECTED)
        return session

    @staticmethod
    def _deadline_of(deadline: Any) -> float:
        # A failing deadline source (e.g. the reservation row is gone) fails closed.
        try:
            return float(deadline() if callable(deadline) else deadline)
        except Exception:
            raise ValueError(REJECTED) from None

    def _check_deadline(self, session: _Session) -> None:
        """Refuse (and the caller discards bytes) once the live stop deadline has passed."""
        if session.deadline is not None and self._deadline_of(session.deadline) <= self.clock():
            raise ValueError(REJECTED)

    def _timeout(self, session: _Session) -> float:
        """Per-socket-operation timeout bounded by the live remaining stop deadline.

        This bounds each blocking socket operation (connect, each read), not the whole
        request, so callers re-check the deadline after the fetch returns (``_check_deadline``)
        and discard the bytes if it passed.  Refuses once the deadline, re-read now, has
        passed.
        """
        if session.deadline is None:
            return self.fetch_timeout
        remaining = self._deadline_of(session.deadline) - self.clock()
        if remaining <= 0:
            raise ValueError(REJECTED)
        return min(self.fetch_timeout, remaining)

    @staticmethod
    def _may_fetch(session: _Session, item: Any) -> None:
        if (
            session.failures.get(item, 0) >= MAX_FAILED_PER_ITEM
            or sum(session.failures.values()) >= MAX_FAILED_PER_TICKET
        ):
            raise ValueError(REJECTED)

    def _verified(self, session: _Session) -> dict[str, Any]:
        if session.snapshot is not None:
            return session.snapshot
        self._may_fetch(session, "manifest")
        timeout = self._timeout(session)
        try:
            with self.fetch_budget(timeout):
                body = _get(
                    self.opener,
                    self.base,
                    "/provider-inputs/manifest",
                    session.token,
                    MAX_MANIFEST_RESPONSE_BYTES,
                    timeout,
                )
            self._check_deadline(session)
            snapshot = {
                "id": body.get("snapshot_id"),
                "digest": body.get("digest"),
                "manifest": body.get("manifest"),
                "delivery": body.get("delivery"),
                "chunks": body.get("chunks"),
            }
            if (
                snapshot["id"] != session.snapshot_id
                or snapshot["digest"] != session.digest
                or snapshot["delivery"] != "chunked"
            ):
                raise ValueError(REJECTED)
            # Re-derives sha256 of the canonical manifest and compares it to the approved digest.
            _verify_manifest(snapshot, session.digest)
            _verify_index(snapshot)
            if snapshot["manifest"]["total_bytes"] > min(MAX_BYTES, session.max_bytes):
                raise ValueError(REJECTED)
            # The manifest reply must fit one rate-bounded guardian reply.
            if len(_encoded(self._manifest_reply(snapshot))) > INPUT_REPLY_BYTES:
                raise ValueError(REJECTED)
        except Exception:
            session.failures["manifest"] = session.failures.get("manifest", 0) + 1
            raise ValueError(REJECTED) from None
        session.snapshot = snapshot
        return snapshot

    @staticmethod
    def _manifest_reply(snapshot: dict[str, Any]) -> dict[str, Any]:
        return dict(
            schema="tessera.input-manifest.v1",
            snapshot_id=snapshot["id"],
            digest=snapshot["digest"],
            manifest=snapshot["manifest"],
            delivery="chunked",
            chunks=snapshot["chunks"],
        )

    def manifest(self, operation: str) -> dict[str, Any]:
        session = self._live(operation)
        return self._manifest_reply(self._verified(session))

    def _content(self, session: _Session, snapshot: dict[str, Any], seq: int) -> bytes:
        """The verified bytes of stored chunk ``seq``, from the one-chunk cache or control."""
        if session.cached is not None and session.cached[0] == seq:
            return session.cached[1]
        item = snapshot["chunks"][seq]
        self._may_fetch(session, seq)
        timeout = self._timeout(session)
        try:
            with self.fetch_budget(timeout):
                chunk = _get(
                    self.opener,
                    self.base,
                    f"/provider-inputs/chunks/{seq}",
                    session.token,
                    MAX_CHUNK_RESPONSE_BYTES,
                    timeout,
                )
            self._check_deadline(session)
            content = _decode_chunk(snapshot, item, chunk)  # sha256, size and index agreement
        except Exception:
            session.failures[seq] = session.failures.get(seq, 0) + 1
            raise ValueError(REJECTED) from None
        session.cached = (seq, content)
        return content

    def planned_requests(self, operation: str) -> int:
        """Reads a complete pull needs: the manifest plus every canonical range.

        Before the manifest is verified only the manifest read is planned.
        """
        session = self._sessions.get(operation)
        if session is None or session.snapshot is None:
            return 1
        return 1 + sum(-(-c["size"] // INPUT_RANGE_BYTES) for c in session.snapshot["chunks"])

    def chunk(self, operation: str, seq: Any, offset: Any, length: Any) -> dict[str, Any]:
        """One canonical range of stored chunk ``seq``.

        Ranges are canonical: ``offset`` is a multiple of INPUT_RANGE_BYTES and
        ``length`` is ``min(INPUT_RANGE_BYTES, size - offset)``, so a pull has exactly
        ``planned_requests`` reads and every reply stays within INPUT_REPLY_BYTES.
        The served-byte budget is charged here and refunded (bounded) if the reply
        never left.
        """
        if (
            type(seq) is not int
            or type(offset) is not int
            or type(length) is not int
            or seq < 0
            or offset < 0
            or offset % INPUT_RANGE_BYTES
        ):
            raise ValueError(REJECTED)
        session = self._live(operation)
        snapshot = self._verified(session)
        index = snapshot["chunks"]
        if seq >= len(index):
            raise ValueError(REJECTED)
        item = index[seq]
        if offset >= item["size"] or length != min(INPUT_RANGE_BYTES, item["size"] - offset):
            raise ValueError(REJECTED)
        budget = snapshot["manifest"]["total_bytes"] * MAX_SERVED_FACTOR
        if session.bytes_served + length > budget:
            raise ValueError(REJECTED)
        content = self._content(session, snapshot, seq)
        session.chunks_served += 1
        session.bytes_served += length
        return dict(
            schema="tessera.input-range.v1",
            snapshot_id=snapshot["id"],
            seq=seq,
            offset=offset,
            length=length,
            content_base64=base64.b64encode(content[offset : offset + length]).decode("ascii"),
        )

    def refund(self, operation: str, served: int) -> None:
        """Undo the charge of a reply that was cut off before it left (bounded)."""
        session = self._sessions.get(operation)
        if session is None or type(served) is not int or served <= 0:
            return
        if session.refunds >= MAX_REFUNDS_PER_TICKET:
            return
        session.refunds += 1
        session.chunks_served = max(0, session.chunks_served - 1)
        session.bytes_served = max(0, session.bytes_served - served)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
