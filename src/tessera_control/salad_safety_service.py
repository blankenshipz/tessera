"""Independent Salad stop/custody HTTPS service. Default CLI has zero effects.

Administrator RPC and workload JWT are distinct identities. Run on a separately
supervised trusted server, never in control/proxy/workload. Runtime configuration,
TLS keys and credential references are explicitly provisioned, not discovered.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import signal
import socket
import ssl
import stat
import struct
import sys
import time
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .models import ExecutionSpec
from .remote_execution_models import (
    RemoteExecutionPackage,
    RemoteProposal,
    derived_salad_argv,
    route_window_limit,
)
from .salad_custody import CustodyBinding, SaladCustody, private_database
from .salad_guardian import (
    DEFAULT_PRESTART_READ_DEADLINE_SECONDS,
    RECONCILE_OPERATOR,
    RECONCILE_PROVIDER,
    GuardianAssignment,
    ProviderObservation,
    SaladGuardian,
    is_deterministic_read_failure,
)
from .salad_http import CREATE_DEADLINE_SECONDS, REQUEST_TIMEOUT_SECONDS, SaladHTTPTransport
from .salad_input_delivery import GuardianInputs
from .salad_secret_files import PrivateFileSecretResolver
from .salad_workload_contract import (
    INPUT_RANGE_BYTES,
    INPUT_REPLY_BYTES,
    LEGACY_OUTPUT_NAME,
    OUTPUT_CHUNK_BYTES,
    RESERVED_UPLOAD_GRACE_SECONDS,
    output_chunk_count,
)
from .salad_workload_jwt import SaladWorkloadJWT, normalized_uuid, strict_json
from .store import _parse_utc, canonical_digest


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@contextmanager
def alarm_budget(seconds):
    """Absolute main-thread budget, preserving any enclosing request deadline."""

    def expired(*unused):
        raise TimeoutError("service_deadline")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    budget = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    signal.setitimer(signal.ITIMER_REAL, budget)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )


def read_admin_frame(connection):
    """Read one newline-terminated admin frame within the frame deadline and size cap."""
    deadline = time.monotonic() + UNIX_ADMIN_FRAME_SECONDS
    raw = bytearray()
    while not raw.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("frame_deadline")
        connection.settimeout(remaining)
        part = connection.recv(65536)
        if not part or len(raw) + len(part) > UNIX_ADMIN_FRAME_BYTES:
            raise ValueError("invalid_frame")
        raw.extend(part)
    return raw


def private_read(path, cap=16384):
    path = Path(path).absolute()
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("unsafe_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > cap
        ):
            raise ValueError("unsafe_file")
        return os.read(fd, cap + 1)
    finally:
        os.close(fd)


STARTED = frozenset({"running", "stopping"})
PRE_START = frozenset({"allocating", "downloading", "creating"})
# Most billed time a run can hide between two guardian polls. The loop runs only
# between requests on the single thread, and serve() handles at most one unix admin
# frame (UNIX_ADMIN_FRAME_SECONDS read + UNIX_ADMIN_BUDGET_SECONDS) and then one HTTPS
# request (at most CHUNK_REQUEST_SECONDS) between loop passes, so the gap must cover
# all three plus one worst-case loop pass (LOOP_PASS_ALLOWANCE_SECONDS, below):
# 2 + 10 + 20 + 12 = 44 <= 45; asserted below from the constants.
UNSEEN_RUN_GAP_SECONDS = 45
# Provider reads (published rate, group/instance): one fast transient failure (a Salad
# 5xx or reset, reported as provider_unknown) is retried, still fail-closed. At most
# len(BACKOFF) + 1 attempts per read; a retry starts only if another full read (the
# transport's request timeout) still fits PROVIDER_READ_BUDGET_SECONDS, measured from the
# first read of its kind (rate, or group/instance) in the loop pass and shared by every read
# of that kind in it; no retry runs past the window (first attempts are unchanged). A read
# that fails by timing out (a full transport deadline) is retried like any other transient
# failure, if the window still has room for its backoff and another full read.
PROVIDER_READ_BACKOFF_SECONDS = (0.2, 0.4)
PROVIDER_READ_ATTEMPTS = len(PROVIDER_READ_BACKOFF_SECONDS) + 1
PROVIDER_READ_SECONDS = float(REQUEST_TIMEOUT_SECONDS)
PROVIDER_READ_BUDGET_SECONDS = 4.5
# A refused workload call, and a failed provider read, record their closed code at most
# this often per code (per reservation).
WORKLOAD_REFUSAL_DEDUP_SECONDS = 60
# Worst-case loop pass: the full retry window (retries never run past it), then the
# first attempts of the group read and a follow-up instance read (each bounded by the
# transport deadline), plus 1 s for the pass's local work. Retries only start inside
# the window, so this is the whole pass, not just its retry part.
# The rate read and the group/instance reads each have their own window per pass, so a
# slow rate read cannot starve a group-read retry (and vice versa). Group and instance
# reads share ONE kind (provider_read_failed); a third kind would put the worst case past
# the unseen-run gap, so the set is pinned here and asserted against the gap below.
# The allowance is per pass for the reservation being polled: each further reservation's
# first attempts are bounded only by the transport deadline, not by these windows.
PROVIDER_READ_WINDOW_KIND_NAMES = frozenset({"rate_read_failed", "provider_read_failed"})
PROVIDER_READ_WINDOW_KINDS = len(PROVIDER_READ_WINDOW_KIND_NAMES)
RATE_READ_KIND = "rate_read_failed"
PROVIDER_READ_KIND = "provider_read_failed"
# Companion event kind suffix: the closed transport diagnostics of the same read failure.
READ_FAILURE_REASON_SUFFIX = "_reason"
LOOP_PASS_ALLOWANCE_SECONDS = (
    PROVIDER_READ_WINDOW_KINDS * PROVIDER_READ_BUDGET_SECONDS + PROVIDER_READ_SECONDS + 1.0
)
# A read that failed by timing out (a full PROVIDER_READ_SECONDS) still gets one retry.
assert 2 * PROVIDER_READ_SECONDS + PROVIDER_READ_BACKOFF_SECONDS[0] <= PROVIDER_READ_BUDGET_SECONDS
# Exact provider read that may close an uncertain orphan, at most this often.
CLOSE_CHECK_SECONDS = 15
UNIX_ADMIN_FRAME_SECONDS = 2
UNIX_ADMIN_BUDGET_SECONDS = 10
# Admin requests are judged against the guardian loop at acceptance (see admin()).
# Acceptance is stamped before the unix frame read, so a request may legitimately
# finish up to frame read + admin alarm after it; the HTTPS admin alarm (REQUEST_SECONDS,
# asserted below) is shorter. A completion beyond this window is refused.
ADMIN_ACCEPT_WINDOW_SECONDS = UNIX_ADMIN_FRAME_SECONDS + UNIX_ADMIN_BUDGET_SECONDS
# The loop must have completed a pass within this many seconds before acceptance.
ADMIN_LOOP_FRESH_SECONDS = 2
# Refusal codes on the trusted unix admin socket: closed identifiers only.
REFUSAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Largest admin frame on the trusted unix socket. A workload_v1 reserve carries the
# proposal and the runtime package, each embedding the reviewed contract (up to 128
# pinned fetches, a 64 KiB inline manifest, 32 argv tokens of 256 chars), so real
# reserves exceed the old 16 KiB cap (a 53-fetch contract is ~40 KiB). 512 KiB bounds
# the worst case with headroom; the socket is local and peer-uid checked, and the
# read is still bounded by UNIX_ADMIN_FRAME_SECONDS.
UNIX_ADMIN_FRAME_BYTES = 512 * 1024
# Request bodies: every path keeps the original cap except one chunk upload, which
# is a JSON envelope around at most OUTPUT_CHUNK_BYTES of base64 content.
REQUEST_BODY_BYTES = 131072
OUTPUT_CHUNK_BODY_BYTES = OUTPUT_CHUNK_BYTES * 4 // 3 + 4096
# Request time budget (all wall clock, measured from accept on the single guardian
# thread; the guardian loop runs only between requests):
#   * HEADER_READ_SECONDS: TLS handshake, request line and headers, until the cheap
#     pre-auth check passes. An unauthenticated client never holds the thread longer.
#   * body: ``length / BODY_FLOOR_BYTES_PER_SECOND + BODY_GRACE_SECONDS``. The floor
#     is 1.5 Mbit/s (187,500 B/s); a maximal 1.34 MiB chunk envelope (1,402,197 B)
#     gets 8.48 s, a 128 KiB small body 1.70 s. A slower sender is cut off.
#   * POST_BODY_WORK_SECONDS: auth, provider read (2 s bound), custody write and
#     reply (2 s socket bound) after the body.
# The alarm is the hard cap on the whole request. Small paths keep 10 s
# (3 + 1.70 + 5 = 9.7); only an already pre-authenticated chunk upload may run to 20 s
# (3 + 8.48 + 5 = 16.5, 3.5 s spare).
BODY_FLOOR_BYTES_PER_SECOND = 187_500
BODY_GRACE_SECONDS = 1.0
HEADER_READ_SECONDS = 3.0
POST_BODY_WORK_SECONDS = 5.0
REQUEST_SECONDS = 10.0
CHUNK_REQUEST_SECONDS = 20.0


def body_read_seconds(length):
    return length / BODY_FLOOR_BYTES_PER_SECOND + BODY_GRACE_SECONDS


BODY_READ_SECONDS = body_read_seconds(REQUEST_BODY_BYTES)  # small-body paths
OUTPUT_CHUNK_BODY_SECONDS = body_read_seconds(OUTPUT_CHUNK_BODY_BYTES)
assert HEADER_READ_SECONDS + BODY_READ_SECONDS + POST_BODY_WORK_SECONDS < REQUEST_SECONDS
assert (
    HEADER_READ_SECONDS + OUTPUT_CHUNK_BODY_SECONDS + POST_BODY_WORK_SECONDS < CHUNK_REQUEST_SECONDS
)
assert (
    UNIX_ADMIN_FRAME_SECONDS
    + UNIX_ADMIN_BUDGET_SECONDS
    + CHUNK_REQUEST_SECONDS
    + LOOP_PASS_ALLOWANCE_SECONDS
    <= UNSEEN_RUN_GAP_SECONDS
)
assert REQUEST_SECONDS <= ADMIN_ACCEPT_WINDOW_SECONDS


def log_unclosed_refusal(error, channel):
    """Journal line for a refusal without a closed code: the exception class only.

    ``internal`` and ``service_deadline`` say nothing about the cause; the class name
    (never the message, which may hold paths or values) makes the next one diagnosable.
    """
    if refusal_code(error) in {"internal", "service_deadline"}:
        print(f"guardian {channel} refusal: {type(error).__name__}", file=sys.stderr, flush=True)


class ProviderUnknown(ValueError):
    """A provider reply that was not observed. Its text stays the closed ``provider_unknown``
    (callers branch on that); ``reason`` / ``detail`` / ``field`` / ``request_kind`` carry the
    transport's closed diagnostics (never a value, body or organization/project path). The
    guardian reads them defensively, so an unlabeled failure is a generic transient one."""

    def __init__(self, reason=None, detail=None, field=None, request_kind=None):
        super().__init__("provider_unknown")
        self.request_kind = request_kind
        self.reason = reason
        self.detail = detail
        self.field = field


def read_failure_diagnostics(error):
    """Closed (reason, detail, field, request_kind) of a failed provider read (tokens or None)."""
    if isinstance(error, TimeoutError):
        return "timeout", "timeout", None, None
    if isinstance(error, ProviderUnknown):
        return error.reason, error.detail, error.field, error.request_kind
    return None, None, None, None


def refusal_code(error):
    """Closed refusal code for the trusted unix admin socket.

    Only a ValueError carrying a single identifier-shaped message (our own
    ``raise ValueError("guardian_loop_unready")`` style) is echoed; anything else,
    including library errors whose text may hold paths or values, is "internal". A
    request alarm or provider deadline (``TimeoutError``) is the closed code
    "service_deadline", never its message.
    """
    if isinstance(error, TimeoutError):
        return "service_deadline"
    if (
        isinstance(error, ValueError)
        and len(error.args) == 1
        and type(error.args[0]) is str
        and REFUSAL_CODE.fullmatch(error.args[0])
    ):
        return error.args[0]
    return "internal"


# Staged input replies (/v1/workload/input) run under the chunk alarm. The workload
# reads each stored 4 MiB chunk in ranges of at most INPUT_RANGE_BYTES, so one reply is
# at most INPUT_REPLY_BYTES (1,402,197 B), sent at the same 1.5 Mbit/s floor: 8.48 s.
# Before it: headers, the small request body, auth plus one bounded provider read, and
# on a cache miss one control fetch of the stored chunk (a wall-clock alarm of
# INPUT_CONTROL_FETCH_SECONDS around the whole fetch, on top of the per-socket-operation
# timeout, so a slow-drip control cannot stretch it; the guardian keeps the verified
# chunk for the next ranges). 3 + 1.70 + 3 + 2.5 + 8.48 = 18.68 s < 20 s.
INPUT_PATH = "/v1/workload/input"
INPUT_AUTH_WORK_SECONDS = 3.0
INPUT_CONTROL_FETCH_SECONDS = 2.5
INPUT_REPLY_SECONDS = body_read_seconds(INPUT_REPLY_BYTES)
assert INPUT_REPLY_BYTES == INPUT_RANGE_BYTES * 4 // 3 + 4096
assert (
    HEADER_READ_SECONDS
    + BODY_READ_SECONDS
    + INPUT_AUTH_WORK_SECONDS
    + INPUT_CONTROL_FETCH_SECONDS
    + INPUT_REPLY_SECONDS
    < CHUNK_REQUEST_SECONDS
)
OUTPUT_CHUNK_PATH = "/v1/workload/output-chunk"
# Dispatch chain after a guardian "ready" admission, on the trusted server: the IPC
# reply (salad_server.IPC_TIMEOUT_SECONDS, asserted there to be <= the first term), a
# local authority/credential recheck (an allowance, not an enforced timeout) and one
# create POST with its own total client deadline (salad_http.CREATE_DEADLINE_SECONDS).
DISPATCH_IPC_SECONDS = 12
DISPATCH_RECHECK_SECONDS = 6
DISPATCH_CHAIN_SECONDS = DISPATCH_IPC_SECONDS + DISPATCH_RECHECK_SECONDS + CREATE_DEADLINE_SECONDS
# Lost-create resolution margin past the reservation's route deadline. A create can
# only be dispatched by the trusted server after a guardian "ready" admission, which
# is refused once recovery is set and at the stop deadline (<= route deadline), so a
# legitimate create is sent by about deadline + DISPATCH_CHAIN_SECONDS (30 s). The
# margin is 30 times that, to also cover Salad finishing (or asynchronously
# materialising, 202) a request it accepted after our client gave up: live, Salad
# created a group whose create our client had already abandoned. Only after this margin, a
# definite 404 on the exact reserved name and a project group list (HTTP 200) without
# it may a never-bound reservation become terminal. The 404 body is identical for a
# missing project, so only the list proves the project. See salad-spend-safety.md.
LOST_CREATE_MARGIN_SECONDS = 900
# A create the provider cleanly rejected (HTTP 400/422 ProblemDetails, reported by the
# trusted server) may be released before that margin. In the honest flow the safety
# comes from one create effect per route and its one-use claim nonce: the rejected POST
# was the route's only create. The report is accepted only after the reservation's
# last "ready" and sets recovery at once (no later ready, so no later create). As
# defence in depth the guardian also waits this long after max(report, last ready),
# covering a create admitted by that ready that is still in flight. The rejection is
# the caller's claim and never evidence: release still needs the exact-name 404 and a
# complete project list without the name, read by the guardian itself.
CREATE_REJECTED_SETTLE_SECONDS = 30
assert CREATE_REJECTED_SETTLE_SECONDS >= DISPATCH_CHAIN_SECONDS
assert LOST_CREATE_MARGIN_SECONDS >= 10 * DISPATCH_CHAIN_SECONDS
# Seconds between lost-create absence reads, and between late-group watch reads.
LOST_CREATE_READ_SECONDS = 15
LOST_CREATE_WATCH_INTERVAL_SECONDS = 60
# After resolution the guardian keeps reading the reserved name at a low rate for this
# long; a late group is bound and stopped by the existing stop path, and blocks again.
LOST_CREATE_WATCH_SECONDS = 86400
# Per-operation authenticated request budget for the chunked-output endpoints:
# twice the planned chunks plus this slack (plan calls, retries, final-chunk acks).
OUTPUT_REQUEST_SLACK = 32
# Per-operation authenticated request budget for /v1/workload/input: twice the planned
# reads (one manifest plus every canonical 1 MiB range of the verified snapshot) plus
# this slack. Before the manifest is verified only the manifest read is planned.
INPUT_REQUEST_SLACK = 32
OUTPUT_PLAN_PATH = "/v1/workload/outputs"
WORKLOAD_PATHS = frozenset(
    {
        "/v1/workload/begin-once",
        "/v1/workload/output",
        "/v1/workload/input",
        OUTPUT_PLAN_PATH,
        OUTPUT_CHUNK_PATH,
    }
)


def _positively_not_running(observation):
    """True only when the read shows nothing can be running yet: a single
    pre-start instance, or a pending/stopped group with no instances."""
    instances = observation.get("instances") or []
    if instances:
        return all(i.get("state") in PRE_START for i in instances)
    return observation.get("state") in {"pending", "stopped"}


def _running(observation):
    """Group reports running, or its (single) instance already runs while deploying."""
    return observation.get("state") == "running" or any(
        i.get("state") == "running" for i in observation.get("instances") or []
    )


FRAME_RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS frame_records (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS frame_records_by_reservation ON frame_records(reservation, sequence);
CREATE TRIGGER IF NOT EXISTS frame_records_no_update BEFORE UPDATE ON frame_records
BEGIN SELECT RAISE(ABORT, 'frame_records is append-only'); END;
CREATE TRIGGER IF NOT EXISTS frame_records_no_delete BEFORE DELETE ON frame_records
BEGIN SELECT RAISE(ABORT, 'frame_records is append-only'); END;
"""
FRAME_VERDICTS = ("open", "stopped", "reconciled", "absent")
# Reconciliation reason codes that prove a create never existed (verdict ``absent``). A closed
# set: only these exact codes map to ``absent``, never a substring of a free-text reason.
REASON_LOST_CREATE_ABSENT = "lost_create_absent"
REASON_CREATE_REJECTED_ABSENT = "create_rejected_absent"
REASON_OPERATOR_LOST_CREATE_ABSENT = "operator_lost_create_absent"
ABSENT_REASONS = frozenset(
    {
        REASON_LOST_CREATE_ABSENT,
        REASON_CREATE_REJECTED_ABSENT,
        REASON_OPERATOR_LOST_CREATE_ABSENT,
    }
)


def frame_verdict(status, reason):
    """The frame verdict for a guardian status: ``stopped``, ``reconciled`` or ``absent`` (a
    create proven never to have existed), else ``open``. Nothing else is terminal."""
    if status == "stopped":
        return "stopped"
    if status == "reconciled":
        return "absent" if reason in ABSENT_REASONS else "reconciled"
    return "open"


class SaladSafetyService:
    """Single-thread service owns all SQLite connections and guardian execution."""

    def __init__(
        self,
        directory,
        *,
        organization_ids,
        resolver,
        reference,
        version,
        destination,
        verifier=None,
        transport_factory=SaladHTTPTransport,
        clock=time.time,
        total_cap_cents=None,
        control_origin=None,
        control_opener=None,
        max_open_billable_frames=1,
        max_prestart_rebinds=3,
        prestart_read_deadline_seconds=DEFAULT_PRESTART_READ_DEADLINE_SECONDS,
    ):
        # ADR 0041 resolved question 4b: concurrent billable frames need a later record that
        # covers total-cap races, partial stops and accrual interleaving, so only 1 is allowed.
        if type(max_open_billable_frames) is not int or max_open_billable_frames != 1:
            raise ValueError("invalid_max_open_billable_frames")
        self.max_open_billable_frames = max_open_billable_frames
        if total_cap_cents is not None and (
            type(total_cap_cents) is not int or not 1 <= total_cap_cents <= 1_000_000
        ):
            raise ValueError("invalid_total_cap")
        self.total_cap_cents = total_cap_cents
        # Optional: where the guardian pulls input snapshots from (loopback HTTP or HTTPS).
        self.inputs = (
            GuardianInputs(
                control_origin,
                opener=control_opener,
                clock=clock,
                fetch_timeout=INPUT_CONTROL_FETCH_SECONDS,
                fetch_budget=alarm_budget,
            )
            if control_origin
            else None
        )
        root = Path(directory)
        self.clock, self.resolver = clock, resolver
        self.organization_ids, self.reference, self.version = organization_ids, reference, version
        self.destination, self.transport_factory = destination, transport_factory
        self.jwt = verifier or SaladWorkloadJWT(clock=clock)
        self.custody = SaladCustody(root / "custody.db", clock=clock)
        self.db = private_database(root / "service.db")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reservations (
                reservation TEXT PRIMARY KEY, operation TEXT UNIQUE, tenant TEXT,
                project TEXT, proposal TEXT, binding TEXT, deadline REAL,
                heartbeat REAL, provider_id TEXT, allocation TEXT, machine TEXT,
                begun INTEGER DEFAULT 0, output INTEGER DEFAULT 0,
                recovery INTEGER DEFAULT 0, seen_running INTEGER DEFAULT 0);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(reservations)")}
        if "routing_package" not in columns:
            self.db.execute("ALTER TABLE reservations ADD COLUMN routing_package TEXT")
        if "route_id" not in columns:
            self.db.execute("ALTER TABLE reservations ADD COLUMN route_id TEXT")
        if "runtime_package" not in columns:
            self.db.execute("ALTER TABLE reservations ADD COLUMN runtime_package TEXT")
        if "allocation_uncertain" not in columns:
            self.db.execute(
                "ALTER TABLE reservations ADD COLUMN allocation_uncertain INTEGER DEFAULT 0"
            )
        for name, declaration in {
            "rate": "TEXT",
            "rate_at": "REAL",
            "accrued_cents": "TEXT DEFAULT '0'",
            "accounted_at": "REAL",
            "cost_cap": "INTEGER",
            "budget_fenced": "INTEGER DEFAULT 0",
            "rate_unknown": "INTEGER DEFAULT 1",
            "escalation": "INTEGER DEFAULT 0",
            "budget_binding": "TEXT",
            "reconciled_attestation": "TEXT",
            "first_running_at": "REAL",
            "last_unseen_at": "REAL",
            "seen_running_at": "REAL",
            "running_seconds": "REAL",
            # Authenticated chunked-output requests charged before any provider read.
            "output_requests": "INTEGER DEFAULT 0",
            # Authenticated staged-input requests charged before any provider read.
            "input_requests": "INTEGER DEFAULT 0",
            # JSON record of a never-bound reservation resolved as lost-create absent.
            "lost_create_resolution": "TEXT",
            # JSON record of a create the trusted server reported as cleanly rejected.
            "create_rejection": "TEXT",
            # Last successful guardian "ready" admission (a create may follow it).
            "last_ready_at": "REAL",
            # A fast release was voided by a group seen under the name: never again.
            "create_rejection_voided": "INTEGER DEFAULT 0",
            # Published-rate reads that needed a bounded retry (receipt evidence only).
            "rate_read_retries": "INTEGER DEFAULT 0",
            # Group/instance reads that needed a bounded retry (receipt evidence only).
            "provider_read_retries": "INTEGER DEFAULT 0",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE reservations ADD COLUMN {name} {declaration}")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, "
            "reservation TEXT, operation TEXT, at REAL, kind TEXT, detail TEXT)"
        )
        # Refused reserves (the open-billable-frame fence), kept apart from ``events`` so a
        # refusal never reads as evidence about a reservation.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fence_refusals (sequence INTEGER PRIMARY KEY, "
            "reservation TEXT, operation TEXT, at REAL, open_frames INTEGER, frame_limit INTEGER)"
        )
        self.db.executescript(FRAME_RECORD_SCHEMA)
        with self.db:
            self.db.execute("UPDATE reservations SET recovery=1")
        self.guardian = SaladGuardian(
            root / "guardian.db",
            custody=self.custody,
            observe=self._observe,
            stop=self._stop,
            clock=clock,
            custody_started=self._custody_started,
            max_prestart_rebinds=max_prestart_rebinds,
            prestart_read_deadline_seconds=prestart_read_deadline_seconds,
        )
        self.loop_at = None
        # Real-time pacing for bounded rate-read retries (tests may replace both).
        self.retry_sleep, self.retry_monotonic = time.sleep, time.monotonic
        # Per loop pass: read kind -> start of that kind's retry window (None outside a pass).
        self.retry_windows = None
        # Diagnostic writes (read failures, workload refusals) that themselves failed.
        self.diagnostic_write_failures = 0
        self.next_poll = {}
        self.next_close_check = {}
        self.next_rate = {}
        self.next_absence = {}

    def close(self):
        self.guardian.close()
        self.custody.db.close()
        self.db.close()

    def _frame_note(self, reservation, kind, detail=None, *, once=False):
        """Append one row to the reservation's frame record. The caller owns the transaction.

        ``once`` skips a row of the same kind and detail already recorded, so a repeated
        observation never grows the record."""
        text = None if detail is None else json.dumps(detail, sort_keys=True)
        if (
            once
            and self.db.execute(
                "SELECT 1 FROM frame_records WHERE reservation=? AND kind=? AND detail IS ?",
                (reservation, kind, text),
            ).fetchone()
        ):
            return
        self.db.execute(
            "INSERT INTO frame_records(reservation,at,kind,detail) VALUES(?,?,?,?)",
            (reservation, self.clock(), kind, text),
        )

    def _frame_record(self, reservation, status):
        """The additive ``frame_record`` of a status: verdict, last sequence and the digest of
        every row, in order. A terminal verdict appends its ``terminal`` row first, so a verdict
        returned here is always covered by the digest returned with it."""
        verdict = frame_verdict(status.get("status"), status.get("reason"))
        if verdict != "open":
            with self.db:
                self._frame_note(
                    reservation,
                    "terminal",
                    {"status": status.get("status"), "verdict": verdict},
                    once=True,
                )
        rows = self.db.execute(
            "SELECT sequence,kind,at,detail FROM frame_records WHERE reservation=? "
            "ORDER BY sequence",
            (reservation,),
        ).fetchall()
        digest = hashlib.sha256(
            json.dumps([list(row) for row in rows], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return dict(
            reservation=reservation,
            verdict=verdict,
            sequence=rows[-1][0] if rows else 0,
            record_digest=digest,
        )

    def _cancel(self, operation):
        """``guardian.cancel``, then record that a stop was requested for the frame."""
        self.guardian.cancel(operation)
        row = self.db.execute(
            "SELECT reservation FROM reservations WHERE operation=?", (operation,)
        ).fetchone()
        if row:
            with self.db:
                self._frame_note(row[0], "stop_requested", once=True)

    def _row(self, reservation=None, operation=None):
        self.db.row_factory = __import__("sqlite3").Row
        row = self.db.execute(
            "SELECT * FROM reservations WHERE "
            + ("reservation=?" if reservation else "operation=?"),
            (reservation or operation,),
        ).fetchone()
        if not row:
            raise ValueError("reservation_missing")
        return dict(row)

    def _send(self, row, action, provider_id=None):
        with alarm_budget(6):
            return self._bounded_send(row, action, provider_id)

    # Explicit admission reserve for stop/reconciliation after the run deadline.
    # This is not automatic renewal or proof the provider can always terminate.
    STOP_RECOVERY_MARGIN_SECONDS = 60

    def _credential(self):
        metadata = self.resolver.metadata(self.reference)
        now = self.clock()
        if (
            metadata.version != self.version
            or metadata.revoked
            or metadata.purpose != "salad_management"
            or not 0 <= now - metadata.checked_at <= 5
            or metadata.expires_at <= now + 2
        ):
            raise ValueError("credential_unavailable")
        return metadata

    def _require_stop_coverage(self, deadline):
        metadata = self._credential()
        if metadata.expires_at <= deadline + self.STOP_RECOVERY_MARGIN_SECONDS:
            raise ValueError("stop_credential_window_insufficient")

    def _bounded_send(self, row, action, provider_id):
        proposal = RemoteProposal.model_validate_json(row["proposal"])
        self._credential()
        with self.resolver.resolve(self.reference, self.version) as secret:
            transport = self.transport_factory(proposal.selection)
            if action == "gpu_class":
                result = transport.gpu_class(proposal.selection, secret)
            elif action == "read_absence":
                result = transport.read_absence(proposal.selection, secret)
            else:
                result = transport.send(action, proposal.selection, provider_id, secret)
        if result.status not in {"observed", "pending"}:
            raise ProviderUnknown(
                getattr(result, "reason", None),
                getattr(result, "detail", None),
                getattr(result, "field", None),
                getattr(result, "request_kind", None),
            )
        return result

    def _record_read_failure(self, row, kind, code, error=None):
        """Deduplicated (per code per minute) closed reason a provider read gave up with."""
        try:
            # Only bound runs: on a never-bound reservation a failed read is the expected
            # lost-create probe, not a run diagnostic.
            if not row.get("reservation") or not row.get("provider_id"):
                return
            now = self.clock()
            last = self.db.execute(
                "SELECT at FROM events WHERE reservation=? AND kind=? AND detail=? "
                "ORDER BY sequence DESC LIMIT 1",
                (row["reservation"], kind, code),
            ).fetchone()
            reason, detail, field, request_kind = read_failure_diagnostics(error)
            companion = (
                None
                if reason is None
                else json.dumps(
                    dict(reason=reason, detail=detail, field=field, request_kind=request_kind),
                    sort_keys=True,
                )
            )
            if (
                last
                and 0 <= now - last[0] < WORKLOAD_REFUSAL_DEDUP_SECONDS
                and self._last_event_detail(row["reservation"], kind + READ_FAILURE_REASON_SUFFIX)
                == companion
            ):
                return
            with self.db:
                self.db.execute(
                    "INSERT INTO events(reservation,operation,at,kind,detail) VALUES(?,?,?,?,?)",
                    (row["reservation"], row.get("operation"), now, kind, code),
                )
                if companion is not None:
                    # The transport's closed diagnostics for the same failure; the outward
                    # code above stays provider_unknown.
                    self.db.execute(
                        "INSERT INTO events(reservation,operation,at,kind,detail) "
                        "VALUES(?,?,?,?,?)",
                        (
                            row["reservation"],
                            row.get("operation"),
                            now,
                            kind + READ_FAILURE_REASON_SUFFIX,
                            companion,
                        ),
                    )
        except Exception:
            self.diagnostic_write_failures += 1

    def _last_event_at(self, reservation, kind):
        """Time of the latest recorded event of ``kind``: for a deduplicated diagnostic it
        is the first occurrence within its minute, not the latest occurrence."""
        last = self.db.execute(
            "SELECT at FROM events WHERE reservation=? AND kind=? ORDER BY sequence DESC LIMIT 1",
            (reservation, kind),
        ).fetchone()
        return last[0] if last else None

    def _read_failure_diagnostics(self, reservation, kind):
        """Closed transport diagnostics of the latest ``kind`` failure (None when unknown)."""
        raw = self._last_event_detail(reservation, kind + READ_FAILURE_REASON_SUFFIX)
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        if type(parsed) is not dict:
            parsed = {}
        prefix = "last_" + kind.replace("_failed", "_failure")
        return {
            f"{prefix}_{name}": parsed.get(name)
            for name in ("reason", "detail", "field", "request_kind")
        }

    def _last_event_detail(self, reservation, kind):
        last = self.db.execute(
            "SELECT detail FROM events WHERE reservation=? AND kind=? "
            "ORDER BY sequence DESC LIMIT 1",
            (reservation, kind),
        ).fetchone()
        return last[0] if last else None

    def _retrying_read(self, row, column, read, *, give_up=None, failure_kind):
        """Run ``read`` with a bounded retry of fast transient provider failures.

        Only provider_unknown (a failed or non-observed provider reply) and timeouts are
        retried, at most len(PROVIDER_READ_BACKOFF_SECONDS) times, after a short backoff
        and only while PROVIDER_READ_BUDGET_SECONDS (from the first read of this kind in
        the loop pass, or from this call outside the loop) leaves room for another full
        read. Anything else, and the
        last failure, propagates (as ``give_up`` when given): fail-closed exactly as
        before. Retries are added to the reservation's cumulative ``column`` when a
        reservation exists, whether or not a later attempt succeeds.
        """
        assert failure_kind in PROVIDER_READ_WINDOW_KIND_NAMES
        started = (
            self.retry_windows.setdefault(failure_kind, self.retry_monotonic())
            if self.retry_windows is not None
            else self.retry_monotonic()
        )
        retries = 0
        try:
            for attempt in range(PROVIDER_READ_ATTEMPTS):
                try:
                    return read()
                except (ValueError, TimeoutError) as error:
                    transient = (
                        isinstance(error, TimeoutError) or str(error) == "provider_unknown"
                    ) and not is_deterministic_read_failure(error)
                    backoff = (
                        PROVIDER_READ_BACKOFF_SECONDS[attempt]
                        if attempt < len(PROVIDER_READ_BACKOFF_SECONDS)
                        else None
                    )
                    elapsed = self.retry_monotonic() - started
                    if (
                        not transient
                        or backoff is None
                        or PROVIDER_READ_BUDGET_SECONDS - elapsed < backoff + PROVIDER_READ_SECONDS
                    ):
                        self._record_read_failure(
                            row,
                            failure_kind,
                            "timeout" if isinstance(error, TimeoutError) else refusal_code(error),
                            error,
                        )
                        if give_up is not None:
                            raise give_up from None
                        raise
                    retries += 1
                    self.retry_sleep(backoff)
            raise give_up or ValueError("provider_unknown")
        finally:
            if retries and row.get("reservation"):
                with self.db:
                    self.db.execute(
                        f"UPDATE reservations SET {column}={column}+? WHERE reservation=?",
                        (retries, row["reservation"]),
                    )

    def _rate(self, row):
        """Fresh published rate; a fast transient read failure is retried (bounded).

        Every attempt must yield an exact, fresh (<= 5 s) observation; a stale or
        mismatched observation, a credential refusal or any other error is never
        retried, and when all attempts fail the rate is unknown and the caller fences
        and stops. Retries count in rate_read_retries.
        """
        return self._retrying_read(
            row,
            "rate_read_retries",
            lambda: self._rate_once(row),
            give_up=ValueError("rate_unknown"),
            failure_kind=RATE_READ_KIND,
        )

    def _rate_once(self, row):
        result = self._send(row, "gpu_class")
        o = result.observation
        p = RemoteProposal.model_validate_json(row["proposal"])
        cpu_only = p.selection.resources.gpu_count == 0
        if (
            result.status != "observed"
            or type(o) is not dict
            or o.get("selection_digest") != canonical_digest(p.selection.model_dump(mode="json"))
            or (
                # A CPU-only route has no GPU class: its rate is the operator's reviewed CPU
                # rate (ADR 0062), never a published GPU price.
                o.get("gpu_class_id") is not None
                or o.get("price_basis") != "operator_cpu_rate_not_actual_cost"
                if cpu_only
                else (
                    normalized_uuid(o.get("gpu_class_id"))
                    != normalized_uuid(p.selection.resources.gpu_class)
                    or o.get("price_basis") != "published_gpu_hourly_rate_not_actual_cost"
                )
            )
            or type(o.get("hourly_price_usd")) is not str
        ):
            raise ValueError("rate_observation_invalid")
        at = _parse_utc(o["observed_at"]).timestamp()
        price = Decimal(o["hourly_price_usd"])
        if not price.is_finite() or price <= 0:
            raise ValueError("rate_observation_invalid")
        if not 0 <= self.clock() - at <= 5:
            raise ValueError("rate_stale")
        return price, at

    def _account(self, row, *, force=False):
        now, last = self.clock(), row["accounted_at"]
        if last is None:
            return
        try:
            stopped = self._lost_create_closed(row) or (
                row["provider_id"]
                and self.guardian.status(row["operation"])["status"] in {"stopped", "reconciled"}
            )
        except ValueError:
            stopped = False
        estimate = Decimal(row["accrued_cents"])
        # Salad bills only while an instance runs: nothing accrues before the
        # instance is first observed running (allocation/image pull are free).
        started = row["first_running_at"]
        if (not stopped or force) and row["rate"] and started is not None:
            elapsed = max(0, now - max(last, started))
            estimate += Decimal(row["rate"]) * Decimal(str(elapsed)) * 100 / 3600
        escalation = row["cost_cap"] is not None and estimate > row["cost_cap"]
        with self.db:
            self.db.execute(
                "UPDATE reservations SET accrued_cents=?,accounted_at=?,"
                "escalation=MAX(escalation,?) WHERE reservation=?",
                (format(estimate, "f"), max(now, last), int(escalation), row["reservation"]),
            )

    def _stop_deadline(self, row):
        """Earliest billed-time bound: route (provisioning) deadline, or the
        first-running instant plus the approved running window once running."""
        # Accrual is backdated (first_running_at) for cost; the stop deadline runs
        # from the actual first sighting so a backdate never cuts a run short.
        started = row["seen_running_at"] or row["first_running_at"]
        if row["running_seconds"] is None or started is None:
            return row["deadline"]
        return min(row["deadline"], started + row["running_seconds"])

    def _billed_remaining(self, row):
        """Worst-case remaining billed seconds from now."""
        now = self.clock()
        if row["running_seconds"] is None or row["first_running_at"] is None:
            window = row["deadline"] - now
            if row["running_seconds"] is not None:
                window = min(window, row["running_seconds"])
            return window
        return self._stop_deadline(row) - now

    @staticmethod
    def _lost_create_closed(row):
        """A never-bound reservation resolved lost-create absent (and still unbound)."""
        return bool(row["lost_create_resolution"]) and not row["provider_id"]

    def _settled(self, row):
        if self._lost_create_closed(row):
            return True
        try:
            return bool(row["provider_id"]) and self.guardian.status(row["operation"])[
                "status"
            ] in {"stopped", "reconciled"}
        except ValueError:
            return False

    def _committed(self, *, exclude=None, account=True):
        """Estimated spend plus still-possible per-run exposure over all reservations.

        A stopped/reconciled reservation contributes only its frozen accrued
        estimate; any other contributes the larger of its accrual and its
        admitted per-run cap."""
        total = Decimal(0)
        for (name,) in self.db.execute("SELECT reservation FROM reservations").fetchall():
            if name == exclude:
                continue
            row = self._row(name)
            if account:  # never inside a caller's write transaction (it commits)
                self._account(row)
                row = self._row(name)
            accrued = Decimal(row["accrued_cents"])
            if self._settled(row):
                total += accrued
                continue
            cap = row["cost_cap"]
            if cap is None:
                # No recorded cap yet: count the approved per-run cap from the
                # retained proposal, never ~0 for a reserved-but-unbilled run.
                proposal = RemoteProposal.model_validate_json(row["proposal"])
                cap = proposal.selection.salad.execution.max_cost_cents
            total += max(accrued, Decimal(cap))
        return total

    def _budget(self, row, *, refresh):
        self._account(row)
        row = self._row(row["reservation"])
        try:
            price, at = (
                self._rate(row)
                if refresh
                else (Decimal(row["rate"]) if row["rate"] else None, row["rate_at"])
            )
        except Exception:
            self._exposure_fence(row)
            raise ValueError("rate_unknown") from None
        changed = row["rate"] is not None and price is not None and price != Decimal(row["rate"])
        if changed:
            price = max(price, Decimal(row["rate"]))
        fresh = price is not None and at is not None and 0 <= self.clock() - at <= 5
        required = (
            Decimal(row["accrued_cents"])
            + (price or Decimal(0)) * Decimal(str(max(0, self._billed_remaining(row)))) * 100 / 3600
        )
        fenced = changed or not fresh or row["budget_fenced"] or required > row["cost_cap"]
        with self.db:
            self.db.execute(
                "UPDATE reservations SET rate=?,rate_at=?,rate_unknown=?,"
                "budget_fenced=MAX(budget_fenced,?) WHERE reservation=?",
                (
                    format(price, "f") if price else None,
                    at,
                    int(not fresh or changed),
                    int(fenced),
                    row["reservation"],
                ),
            )
        if fenced:
            self._exposure_fence(row)
            raise ValueError("exposure_fenced")

    def _open_billable_frames(self, operation):
        """Frames other than ``operation`` that may still bill: not yet ``stopped`` or
        ``reconciled`` (proven-absent frames reconcile). An ambiguous create with no provider
        id still counts until it is resolved (ADR 0041 section 7.2, resolved question 4a)."""
        others = self.db.execute(
            "SELECT reservation FROM reservations WHERE operation!=?", (operation,)
        ).fetchall()
        return sum(
            1
            for (other,) in others
            if self._status(self._row(other))["status"] not in {"stopped", "reconciled"}
        )

    def _over_total_cap(self, reservation, profile):
        return self.total_cap_cents is not None and (
            self._committed(exclude=reservation, account=False)
            + Decimal(profile.execution.max_cost_cents)
            > self.total_cap_cents
        )

    def _record_fence_refusal(self, reservation, operation, open_frames):
        """Durable record of a refused reserve: the count and the limit, nothing else."""
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO fence_refusals(reservation,operation,at,open_frames,frame_limit) "
                    "VALUES(?,?,?,?,?)",
                    (
                        reservation,
                        operation,
                        self.clock(),
                        open_frames,
                        self.max_open_billable_frames,
                    ),
                )
        except Exception:
            self.diagnostic_write_failures += 1

    def _exposure_fence(self, row):
        with self.db:
            self.db.execute(
                "UPDATE reservations SET recovery=1,budget_fenced=1,rate_unknown=1 "
                "WHERE reservation=?",
                (row["reservation"],),
            )
        try:
            self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
        finally:
            if row["provider_id"]:
                try:
                    self._cancel(row["operation"])
                except ValueError:
                    pass

    def _status(self, row):
        self._account(row)
        row = self._row(row["reservation"])
        if row["provider_id"]:
            status = self.guardian.status(row["operation"])
        elif self._lost_create_closed(row):
            record = json.loads(row["lost_create_resolution"])
            # Terminal by definite absence, never a stop: nothing was created.
            status = dict(
                status="reconciled",
                reason={
                    "automatic": REASON_LOST_CREATE_ABSENT,
                    "create_rejected": REASON_CREATE_REJECTED_ABSENT,
                }.get(record["kind"], REASON_OPERATOR_LOST_CREATE_ABSENT),
                orphan=False,
                allocation_uncertain=False,
                manifest=None,
                cost="unknown",
                pending_exposure=True,
                node_writers="unknown",
                quarantine=True,
                lost_create=record,
            )
        else:
            status = dict(status="reserved", quarantine=True)
            if row["recovery"] and not row["allocation"]:
                # Fail closed until this instant; then absence must be proven.
                status["lost_create_eligible_at"] = row["deadline"] + LOST_CREATE_MARGIN_SECONDS
            if row["create_rejection"]:
                status["create_rejection"] = json.loads(row["create_rejection"])
                status["create_rejected_eligible_at"] = self._rejection_eligible_at(row)
        status["input_delivery"] = (
            self.inputs.status(row["operation"]) if self.inputs else dict(ticket_held=False)
        )
        status.update(
            estimated_exposure_cents=row["accrued_cents"],
            actual_billed_cost_cents=None,
            published_hourly_rate_usd=row["rate"],
            rate_unknown=bool(row["rate_unknown"]),
            rate_read_retries=row["rate_read_retries"] or 0,
            provider_read_retries=row["provider_read_retries"] or 0,
            last_workload_refusal=self._last_workload_refusal(row["reservation"]),
            # Most recent ever (never cleared by later successful reads), with its time.
            last_rate_read_failure=self._last_event_detail(row["reservation"], "rate_read_failed"),
            last_rate_read_failure_at=self._last_event_at(row["reservation"], "rate_read_failed"),
            last_provider_read_failure=self._last_event_detail(
                row["reservation"], "provider_read_failed"
            ),
            last_provider_read_failure_at=self._last_event_at(
                row["reservation"], "provider_read_failed"
            ),
            **self._read_failure_diagnostics(row["reservation"], "provider_read_failed"),
            diagnostic_write_failures=self.diagnostic_write_failures,
            exposure_unknown=True,
            exposure_pending=True,
            budget_fenced=bool(row["budget_fenced"]),
            first_running_at=row["first_running_at"],
            total_cap_cents=self.total_cap_cents,
            max_open_billable_frames=self.max_open_billable_frames,
            total_committed_cents=None,
            total_remaining_cents=None,
            escalation_pending=bool(row["escalation"]),
            financial_hard_cap=False,
        )
        if self.total_cap_cents is not None:
            committed = self._committed()
            status["total_committed_cents"] = format(committed, "f")
            status["total_remaining_cents"] = format(
                max(Decimal(0), Decimal(self.total_cap_cents) - committed), "f"
            )
        binding = CustodyBinding(**json.loads(row["binding"]))
        status["retained_output"] = self.custody.retained_metadata(binding, LEGACY_OUTPUT_NAME)
        status["retained_outputs"] = self.custody.retained_outputs(binding)
        status["allocation_uncertain"] = bool(row["allocation_uncertain"]) or status.get(
            "allocation_uncertain", False
        )
        # ADR 0041 section 7: the guardian's own append-only per-frame record (additive).
        status["frame_record"] = self._frame_record(row["reservation"], status)
        return status

    def _reconcile(self, row, attestation):
        """Operator recovery for an orphan the guardian may not self-confirm."""
        if type(attestation) is not str or not 1 <= len(attestation) <= 256:
            raise ValueError("attestation_required")
        if not row["provider_id"]:
            # Never-bound reservation: only the lost-create absence path applies.
            self._resolve_lost_create(row, attestation=attestation)
            return self._status(self._row(row["reservation"]))
        self._close_uncertain_stopped(row, attestation=attestation)
        return self._status(self._row(row["reservation"]))

    def _close_uncertain_stopped(self, row, *, attestation=None):
        """Close an uncertain orphan once a fresh exact provider read shows it stopped.

        Shared by the operator reconcile (with an attestation) and the loop's
        automatic provider-confirmed close (attestation None). The provider's own
        read is authoritative for what it runs and bills, so this freezes accrual
        and releases the orphan fence; it never seals custody or accepts output.
        Every refusal raises before any side effect.
        """
        status = self.guardian.status(row["operation"])
        # Only an uncertain allocation needs this. An ordinary stop
        # reaches exact terminal (and keeps its retained output importable) by itself.
        if (
            status["status"] in {"stopped", "reconciled"}
            or not (status["allocation_uncertain"] or row["allocation_uncertain"])
            or (status["reason"] == "armed" and not row["recovery"])
        ):
            raise ValueError("reconcile_not_applicable")
        # Fresh exact provider read by this service; caller claims are not evidence.
        observation, *_ = self._provider(row)
        counts = observation.get("instance_status_counts")
        if not (
            observation.get("state") == "stopped"
            and type(counts) is dict
            and set(counts)
            == {"allocating_count", "creating_count", "running_count", "stopping_count"}
            and all(type(v) is int and v == 0 for v in counts.values())
            and observation.get("pending_change") is False
            and not observation.get("instances")
            # Counts Salad did not send (the transport zero-fills a CPU group's omissions) are
            # never evidence that no instance runs (ADR 0071).
            and observation.get("instance_counts_complete", True) is True
        ):
            raise ValueError("reconcile_requires_exact_stopped_group")
        # Freeze the estimate through now; reconciled operations stop accruing.
        self._account(row, force=True)
        # Writers close BEFORE the reconcile commits: a custody failure leaves the
        # operation unreconciled (and retried), never reconciled with writers open.
        self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
        self.guardian.reconcile(  # raises before any side effect
            row["operation"],
            reason=RECONCILE_PROVIDER if attestation is None else RECONCILE_OPERATOR,
        )
        if attestation is not None:
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET reconciled_attestation=? WHERE reservation=?",
                    (hashlib.sha256(attestation.encode()).hexdigest(), row["reservation"]),
                )

    @staticmethod
    def _never_bound(row):
        return not (
            row["provider_id"]
            or row["allocation"]
            or row["machine"]
            or row["lost_create_resolution"]
            or row["begun"]
            or row["output"]
            or row["seen_running"]
            or row["first_running_at"] is not None
            or row["allocation_uncertain"]
        )

    @staticmethod
    def _rejection_eligible_at(row):
        """Earliest fast-release instant: settle after max(report, last ready)."""
        if not row["create_rejection"] or row["create_rejection_voided"]:
            return None
        reported = json.loads(row["create_rejection"])["reported_at"]
        return max(reported, row["last_ready_at"] or reported) + CREATE_REJECTED_SETTLE_SECONDS

    def _fence(self, row):
        """Stop-only fence, as ``cancel``: recovery set, inputs dropped, writers closed."""
        if self.inputs:
            self.inputs.drop(row["operation"])
        with self.db:
            self.db.execute(
                "UPDATE reservations SET recovery=1 WHERE reservation=?", (row["reservation"],)
            )
        self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
        if row["provider_id"]:
            self._cancel(row["operation"])

    def _create_rejected(self, row, rejection):
        """Trusted-server report that the create was cleanly rejected (400/422).

        Fences at once (stop-only recovery, writers closed, inputs dropped) and
        records the bounded summary. It is a request, not evidence: the release is
        ``_resolve_lost_create(rejected=True)`` on the guardian's own reads. The
        first report is kept. A report for a bound or otherwise used reservation,
        one with no earlier ``ready`` admission (so no create could have been sent),
        or one whose earlier fast release was voided is refused after fencing.
        """
        summary = self._rejection_summary(rejection)
        now = self.clock()
        if (
            not self._never_bound(row)
            or row["create_rejection_voided"]
            or row["last_ready_at"] is None
            or now < row["last_ready_at"]
        ):
            self._fence(row)
            raise ValueError("create_rejection_not_applicable")
        record = dict(reported_at=now, **summary)
        if self.inputs:
            self.inputs.drop(row["operation"])
        with self.db:
            changed = self.db.execute(
                "UPDATE reservations SET recovery=1,create_rejection=? WHERE reservation=? "
                "AND create_rejection IS NULL AND create_rejection_voided=0 "
                "AND provider_id IS NULL AND allocation IS NULL "
                "AND machine IS NULL AND lost_create_resolution IS NULL "
                "AND first_running_at IS NULL AND begun=0 AND output=0 AND seen_running=0 "
                "AND allocation_uncertain=0",
                (json.dumps(record, sort_keys=True), row["reservation"]),
            ).rowcount
            if changed:
                self.db.execute(
                    "INSERT INTO events(reservation,operation,at,kind,detail) VALUES(?,?,?,?,?)",
                    (
                        row["reservation"],
                        row["operation"],
                        now,
                        "create_rejection_reported",
                        json.dumps(record, sort_keys=True),
                    ),
                )
                self._frame_note(
                    row["reservation"], "create_rejected", {"reported_at": now}, once=True
                )
            else:
                self.db.execute(
                    "UPDATE reservations SET recovery=1 WHERE reservation=?", (row["reservation"],)
                )
        self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
        return self._status(self._row(row["reservation"]))

    @staticmethod
    def _rejection_summary(rejection):
        """Re-validate the caller's summary: closed keys, bounded printable text."""
        if (
            type(rejection) is not dict
            or set(rejection) != {"status", "title", "detail", "error_fields"}
            or type(rejection["status"]) is not int
            # 400/422: a clean ProblemDetails rejection; 0: the create was never sent.
            or rejection["status"] not in {0, 400, 422}
            or type(rejection["error_fields"]) is not list
            or len(rejection["error_fields"]) > 16
            or None in rejection["error_fields"]
        ):
            raise ValueError("invalid_admin_request")
        for value in (rejection["title"], rejection["detail"], *rejection["error_fields"]):
            if value is not None and (
                type(value) is not str or len(value) > 200 or not value.isprintable()
            ):
                raise ValueError("invalid_admin_request")
        return dict(rejection)

    def _resolve_lost_create(self, row, *, attestation=None, rejected=False):
        """Make a never-bound recovering reservation terminal on proven absence.

        Automatic (attestation None) and operator paths share every precondition:
        recovery set, no provider_id/allocation/machine/begin/run/output ever
        recorded, now >= route deadline + LOST_CREATE_MARGIN_SECONDS, and a fresh
        authenticated read of the exact reserved name returning a definite 404. That
        404's body is the same for a missing project, so both paths also need a
        project group-list read (HTTP 200, well-formed) without the name: a complete
        page for the automatic path, a reached page (100+ items allowed) for the
        operator. A failed list read or a listed group refuses both.
        Any failure raises before a side effect, leaving the reservation blocking.

        ``rejected`` is the create-rejected path: the trusted server reported a clean
        400/422 rejection (``create_rejection``), so the deadline margin is replaced by
        CREATE_REJECTED_SETTLE_SECONDS after max(report, last ready); a voided fast
        release never applies again. Every other precondition and
        both reads are the automatic path's, with a complete-page list read.
        """
        if not self._never_bound(row) or not row["recovery"]:
            raise ValueError("reconcile_not_applicable")
        now = self.clock()
        rejection = json.loads(row["create_rejection"]) if row["create_rejection"] else None
        if rejected:
            eligible = self._rejection_eligible_at(row)
            if attestation is not None or eligible is None:
                raise ValueError("reconcile_not_applicable")
            if now < eligible:
                raise ValueError("create_rejection_settle_pending")
        elif now < row["deadline"] + LOST_CREATE_MARGIN_SECONDS:
            raise ValueError("lost_create_margin_pending")
        try:
            result = self._send(row, "read_absence")
        except Exception:
            # Transport error, timeout, 5xx or an unusable credential: never absence.
            raise ValueError("lost_create_absence_unconfirmed") from None
        o = result.observation
        if (
            result.status != "observed"
            or type(o) is not dict
            or o.get("group_not_found") is not True
            or o.get("list_read") not in {"absent", "absent_partial", "present", "unavailable"}
        ):
            raise ValueError("lost_create_absence_unconfirmed")
        if o["list_read"] == "present":
            raise ValueError("lost_create_group_listed")
        # Automatic: a complete page proves absence. Operator: the list read must still
        # have reached the project (a well-formed page, 100+ items allowed).
        accepted = {"absent"} if attestation is None else {"absent", "absent_partial"}
        if o["list_read"] not in accepted:
            raise ValueError("lost_create_absence_unconfirmed")
        now = self.clock()
        if rejected:
            kind, label = "create_rejected", "create_rejected_absent"
        elif attestation is None:
            kind, label = "automatic", "lost_create_absent"
        else:
            kind = "operator"
            label = "sha256:" + hashlib.sha256(attestation.encode()).hexdigest()
        record = dict(
            kind=kind,
            attestation=label,
            resolved_at=now,
            deadline=row["deadline"],
            margin_seconds=(
                CREATE_REJECTED_SETTLE_SECONDS if rejected else LOST_CREATE_MARGIN_SECONDS
            ),
            name_read="not_found",
            list_read=o["list_read"],
            accrued_cents=row["accrued_cents"],
        )
        if rejected:
            record["rejection"] = rejection
        detail = json.dumps(record, sort_keys=True)
        with self.db:
            changed = self.db.execute(
                "UPDATE reservations SET lost_create_resolution=?,reconciled_attestation="
                "COALESCE(?,reconciled_attestation) WHERE reservation=? AND recovery=1 "
                "AND provider_id IS NULL AND allocation IS NULL AND machine IS NULL "
                "AND lost_create_resolution IS NULL AND first_running_at IS NULL "
                "AND begun=0 AND output=0 AND seen_running=0 AND allocation_uncertain=0"
                + (
                    " AND create_rejection IS NOT NULL AND create_rejection_voided=0"
                    if rejected
                    else ""
                ),
                (
                    detail,
                    None if attestation is None else record["attestation"][7:],
                    row["reservation"],
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("reconcile_not_applicable")
            self.db.execute(
                "INSERT INTO events(reservation,operation,at,kind,detail) VALUES(?,?,?,?,?)",
                (
                    row["reservation"],
                    row["operation"],
                    now,
                    "create_rejected_absent_reconciled"
                    if rejected
                    else "lost_create_absent_reconciled"
                    if attestation is None
                    else "operator_lost_create_absent_reconciled",
                    detail,
                ),
            )
            self._frame_note(
                row["reservation"],
                "lost_create_absent",
                {"kind": record["kind"]},
                once=True,
            )
        if self.inputs:
            self.inputs.drop(row["operation"])
        self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))

    def _watch_lost_create(self, row, now):
        """Late-group watch: re-read absence; anything but a definite absence voids.

        A 200/202 on the exact name, a listed group or an unknown read clears the
        resolution, so the fence blocks again. The ordinary loop then binds and stops
        a group found by its reserved name, or re-resolves only after a fresh
        definite absence (with the same path's preconditions). Only a group actually
        seen also voids a create-rejected fast release for good; after an unknown
        read the fast path may resolve it again on a fresh definite absence."""
        try:
            result = self._send(row, "read_absence")
            o = result.observation
            observed = result.status == "observed" and type(o) is dict
        except Exception:
            observed, o = False, None
        if (
            observed
            and o.get("group_not_found") is True
            and o.get("list_read") in {"absent", "absent_partial", "unavailable"}
        ):
            return
        present = observed and (
            o.get("group_not_found") is False or o.get("list_read") == "present"
        )
        kind = "lost_create_late_group_observed" if present else "lost_create_resolution_voided"
        with self.db:
            self.db.execute(
                # A group seen under the name voids the create-rejected fast path for
                # good: only the deadline + margin path can resolve it again.
                "UPDATE reservations SET lost_create_resolution=NULL"
                + (",create_rejection=NULL,create_rejection_voided=1" if present else "")
                + " WHERE reservation=?",
                (row["reservation"],),
            )
            # Recorded before any bind attempt, so the void is visible even if it fails.
            self.db.execute(
                "INSERT INTO events(reservation,operation,at,kind,detail) VALUES(?,?,?,?,?)",
                (row["reservation"], row["operation"], now, kind, row["lost_create_resolution"]),
            )
        self.next_absence[row["reservation"]] = now + LOST_CREATE_READ_SECONDS
        try:
            self._provider(row)
            bound = self._row(row["reservation"])
            self.guardian.arm(self._assignment(bound), stop_only=True)
            self._cancel(bound["operation"])
        except Exception:
            pass  # unresolved again: the ordinary recovery loop keeps trying

    def _custody_started(self, operation):
        """True once anything beyond the provider placing the instance has happened for the
        operation: a run was seen, the workload began, an input was fetched, an output was
        requested or written, or the service is in recovery. Until then a change of allocation
        is only the provider re-scheduling an instance that never ran."""
        row = self._row(operation=operation)
        if (
            row["begun"]
            or row["output"]
            or row["seen_running"]
            or row["first_running_at"] is not None
            or row["input_requests"]
            or row["output_requests"]
            or row["allocation_uncertain"]
            or row["recovery"]
        ):
            return True
        return bool(self.inputs is not None and self.inputs.served(operation))

    def _provider(self, row):
        result = self._retrying_read(
            row,
            "provider_read_retries",
            lambda: self._send(
                row, "read_instance" if row["provider_id"] else "read_group", row["provider_id"]
            ),
            failure_kind=PROVIDER_READ_KIND,
        )
        observation = result.observation
        if type(observation) is not dict or not observation.get("provider_id"):
            raise ValueError("provider_unknown")
        group = normalized_uuid(observation["provider_id"])
        if row["provider_id"] and group != row["provider_id"]:
            raise ValueError("provider_identity_changed")
        with self.db:
            self.db.execute(
                "UPDATE reservations SET provider_id=? WHERE reservation=?",
                (group, row["reservation"]),
            )
            self._frame_note(row["reservation"], "bound", {"provider_id": group}, once=True)
        if "instances" not in observation:
            current = self._row(row["reservation"])
            observation = self._retrying_read(
                current,
                "provider_read_retries",
                lambda: self._send(current, "read_instance", group),
                failure_kind=PROVIDER_READ_KIND,
            ).observation
        instances = observation.get("instances")
        if type(instances) is not list or len(instances) > 1:
            raise ValueError("allocation_unknown")
        allocation = row["allocation"] or "unallocated"
        machine = row["machine"]
        if instances and not row["allocation"] and instances[0].get("state") not in STARTED:
            # Salad may try several nodes while an instance is
            # allocating/downloading/creating, then settle on another.
            # Nothing can write before the container runs, so placement is bound
            # only once it is running; any later identity change is still flagged.
            return observation, group, allocation, machine
        if (
            instances
            and row["allocation"]
            and instances[0].get("state") not in STARTED
            and not (instances[0].get("machine_id") and instances[0].get("instance_id"))
            and not self._custody_started(row["operation"])
        ):
            # A replacement the provider has not placed yet, before anything ran: keep the
            # bound placement until it is placed; the guardian's clocks keep running meanwhile.
            return observation, group, row["allocation"], row["machine"]
        if instances:
            instance = instances[0]
            machine = normalized_uuid(instance["machine_id"])
            allocation = normalized_uuid(instance["instance_id"]) + ":" + machine
            if (
                row["allocation"]
                and (allocation != row["allocation"] or machine != row["machine"])
                and not self._custody_started(row["operation"])
                and self.guardian.rebinds_left(row["operation"]) > 0
            ):
                # Re-scheduled before anything ran or any custody began: record the new placement
                # and let the guardian re-bind (it applies the bound on how often). It sees the
                # old allocation as ``prior_allocation_id`` and decides; nothing is flagged here.
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET allocation=?,machine=? WHERE reservation=?",
                        (allocation, machine, row["reservation"]),
                    )
                return observation, group, allocation, machine
            if row["allocation"] and (allocation != row["allocation"] or machine != row["machine"]):
                # Return changed identity to guardian, never adopt replacement.
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET allocation_uncertain=1,recovery=1 "
                        "WHERE reservation=?",
                        (row["reservation"],),
                    )
                self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
                return observation, group, allocation, machine
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET allocation=?,machine=? WHERE reservation=?",
                    (allocation, machine, row["reservation"]),
                )
                if instance.get("state") == "running":
                    # The placement is bound from an instance that is running right now: record
                    # that something ran (the flag only; accrual is still started by the poll),
                    # so a later change of allocation is never taken for a pre-start re-schedule.
                    self.db.execute(
                        "UPDATE reservations SET seen_running=1 WHERE reservation=?",
                        (row["reservation"],),
                    )
        return observation, group, allocation, machine

    def _assignment(self, row):
        proposal = RemoteProposal.model_validate_json(row["proposal"])
        p = proposal.selection.salad
        return GuardianAssignment(
            CustodyBinding(**json.loads(row["binding"])),
            p.organization,
            p.project,
            row["provider_id"],
            p.group,
            "unallocated",
            proposal.selection.manifests.image,
            row["deadline"],
            poll_seconds=15,
            running_seconds=row["running_seconds"],
        )

    def _observe(self, assignment):
        row = self._row(operation=assignment.binding.operation)
        observation, group, allocation, _ = self._provider(row)
        counts = observation.get("instance_status_counts")
        terminal = (
            observation.get("state") == "stopped"
            and type(counts) is dict
            and set(counts)
            == {"allocating_count", "creating_count", "running_count", "stopping_count"}
            and all(type(v) is int and v == 0 for v in counts.values())
            and observation.get("pending_change") is False
            and not observation.get("instances")
            and observation.get("instance_counts_complete", True) is True
        )
        status = self.guardian.status(assignment.binding.operation)
        stopping = status["reason"] != "armed" or row["recovery"]
        now = self.clock()
        if _running(observation):
            # Billing may have begun any time after the last poll that did not see
            # the instance running; start accrual there, conservatively.
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET seen_running=1,"
                    "first_running_at=COALESCE(first_running_at,last_unseen_at,?),"
                    "seen_running_at=COALESCE(seen_running_at,?) WHERE reservation=?",
                    # No earlier unseen poll: it may have run up to one poll gap.
                    (now - UNSEEN_RUN_GAP_SECONDS, now, row["reservation"]),
                )
        elif row["first_running_at"] is None:
            if terminal and row["provider_id"]:
                # Stopped without ever being observed running: it may have run and
                # stopped between two polls. Charge at least one poll gap.
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET first_running_at=MIN(?, "
                        "COALESCE(last_unseen_at,?)) WHERE reservation=? "
                        "AND first_running_at IS NULL",
                        (now - UNSEEN_RUN_GAP_SECONDS, now, row["reservation"]),
                    )
            elif _positively_not_running(observation):
                # Only positive evidence that nothing runs yet may advance this;
                # an empty or ambiguous read must never shrink the backdated window.
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET last_unseen_at=? WHERE reservation=? "
                        "AND first_running_at IS NULL",
                        (now, row["reservation"]),
                    )
        state = (
            "stopped"
            if terminal and (row["begun"] or row["seen_running"] or stopping)
            else "pending"
        )
        if _running(observation):
            state = "running"
        if row["allocation_uncertain"]:
            state = "unknown"
        return ProviderObservation(
            assignment.organization,
            assignment.provider_project,
            group,
            assignment.group_name,
            allocation,
            assignment.image_digest,
            state,
            self.clock(),
            prior_allocation_id=row["allocation"],
        )

    def _stop(self, assignment):
        row = self._row(operation=assignment.binding.operation)
        if row["provider_id"] != assignment.group_id:
            raise ValueError("stop_identity_mismatch")
        return self._send(row, "stop", assignment.group_id).status

    @staticmethod
    def _request(argv):
        if len(argv) != 10 or argv[:2] != ["v1", "public-integers-v1"]:
            raise ValueError("unsupported_workload")
        fields = (
            "version",
            "public_input",
            "operation",
            "epoch",
            "destination",
            "watchdog",
            "max_seconds",
            "max_output_bytes",
            "max_output_files",
            "dimension",
        )
        values = [*argv[:6], *map(int, argv[6:])]
        request = dict(zip(fields, values, strict=True))
        if not (
            1 <= request["max_seconds"] <= 300
            and 256 <= request["max_output_bytes"] <= 65536
            and request["max_output_files"] == 1
            and 2 <= request["dimension"] <= 32
        ):
            raise ValueError("unsupported_workload")
        return request

    @classmethod
    def _request_for(cls, proposal):
        """Dispatch to the reviewed binding; smoke keeps parsing its exact argv."""
        execution = proposal.selection.salad.execution
        if execution.argv_binding != "workload_v1":
            return cls._request(execution.argv)
        work = execution.workload
        if not work.outputs_supported():
            raise ValueError("outputs_unsupported")
        if work.workload_seconds > proposal.selection.resources.max_seconds:
            raise ValueError("unsupported_workload")
        return dict(
            operation=proposal.operation_id,
            epoch=str(proposal.epoch),
            destination=work.custody_host,
            watchdog=execution.watchdog_profile,
            max_seconds=work.workload_seconds,
            max_output_bytes=work.output_bytes,
            max_output_files=len(work.outputs),
        )

    @staticmethod
    def _declared_outputs(proposal):
        """workload_v1 declared outputs (name, max_bytes); None for the fixed smoke."""
        execution = proposal.selection.salad.execution
        if execution.argv_binding != "workload_v1":
            return None
        return execution.workload.output_plan()

    @staticmethod
    def _running_window(execution, request, remaining):
        """Billed running window: workload runtime (+ upload grace for workload_v1)."""
        if execution.argv_binding == "workload_v1":
            return min(request["max_seconds"] + RESERVED_UPLOAD_GRACE_SECONDS, remaining)
        return min(request["max_seconds"], remaining)

    def admin(self, body, accepted_at=None):
        """Administrator request. ``accepted_at`` is the service clock when the
        listener accepted the connection (unix socket or HTTPS), before the frame or
        body read; direct callers default to now.

        Loop freshness is judged at acceptance, as for workload requests: the loop
        and admin handling share one thread, so bounded provider reads made while
        handling this request (a heartbeat's rate refresh) delay the next loop pass
        but cannot make a stale loop look fresh. Expiry and deadlines are judged now.
        """
        accepted = self.clock() if accepted_at is None else accepted_at
        action, reservation = body["operation"], body["reservation"]
        fields = {"operation", "reservation"}
        if action == "reserve":
            fields |= {
                "route_id",
                "org_id",
                "project_id",
                "proposal",
                "routing_package",
                "runtime_package",
                "budget_binding",
                "route_digest",
                "deadline",
            }
        elif action == "bind":
            fields |= {"provider_id", "allocation_id"}
        elif action == "reconcile":
            fields |= {"attestation"}
        elif action == "input_ticket":
            fields |= {"input_ticket"}
        elif action == "import_chunk":
            fields |= {"name", "seq"}
        elif action == "create_rejected":
            fields |= {"rejection"}
        if set(body) != fields:
            raise ValueError("invalid_admin_request")
        now = self.clock()
        if action == "reserve":
            from .experiment_budget_models import SaladBudgetBinding

            budget = SaladBudgetBinding.model_validate(body["budget_binding"])
            p = RemoteProposal.model_validate(body["proposal"])
            package = RemoteExecutionPackage.model_validate(body["routing_package"])
            if (
                package.proposal != p
                or package.org_id != body["org_id"]
                or package.project_id != body["project_id"]
                or type(body["deadline"]) not in {int, float}
                or not math.isfinite(body["deadline"])
            ):
                raise ValueError("reservation_mismatch")
            profile = p.selection.salad
            if (
                not profile
                or not profile.execution
                or profile.organization not in self.organization_ids
            ):
                raise ValueError("provider_scope_unenrolled")
            if profile.execution.argv_binding == "workload_v1":
                # Refuse before any other work: custody cannot hold these outputs.
                self._request_for(p)
            runtime = body["runtime_package"]
            if (
                budget.admitted_attempt_id != p.attempt_id
                or budget.operation_id != p.operation_id
                or budget.lease_id != p.lease_id
                or budget.epoch != p.epoch
                or budget.package_digest != p.package_digest
                or budget.max_exposure_cents < profile.execution.max_cost_cents
                or budget.expires_at.timestamp() < body["deadline"]
                or budget.reservation_digest
                != canonical_digest(
                    [
                        budget.experiment_id,
                        budget.admitted_attempt_id,
                        budget.grant_id,
                        budget.authorization_revision,
                        budget.max_exposure_cents,
                    ]
                )
            ):
                raise ValueError("budget_binding_mismatch")
            budget_json = budget.model_dump_json()
            if (
                type(runtime) is not dict
                or canonical_digest(runtime) != p.package_digest
                or runtime["operation_id"] != p.operation_id
                or runtime["epoch"] != p.epoch
                or runtime["lease_id"] != p.lease_id
                or runtime["project_id"] != package.project_id
                or runtime["task_id"] != package.task_id
                or runtime.get("org_id", package.org_id) != package.org_id
                or runtime["plan_digest"] != p.plan_digest
                or canonical_digest(runtime["plan"]) != p.plan_digest
                or runtime["pinned_image"] != p.selection.manifests.image
                or runtime["snapshot_digest"] != p.selection.manifests.data
                or body["deadline"] > _parse_utc(runtime["hard_expires_at"]).timestamp()
            ):
                raise ValueError("runtime_package_mismatch")
            plan = runtime["plan"]
            spec = ExecutionSpec.model_validate(plan["execution_spec"])
            argv = derived_salad_argv(
                profile.execution.model_dump(mode="json"),
                spec.argv,
                p.operation_id,
                p.epoch,
                p.selection.resources.max_seconds,
            )
            declared = self._declared_outputs(p)
            if (
                argv != profile.execution.argv
                or profile.execution.max_output_bytes > spec.max_output_bytes
                or spec.output_paths
                != ([LEGACY_OUTPUT_NAME] if declared is None else [o["name"] for o in declared])
                or profile.execution.max_output_files != len(spec.output_paths)
                or spec.input_snapshot_digest != runtime["snapshot_digest"]
                or profile.execution.max_cost_cents > plan["budget_cents"]
                or p.selection.resources.max_seconds
                > route_window_limit(p.selection.target, plan["max_duration_seconds"])
            ):
                raise ValueError("approved_spec_mismatch")
            request = self._request_for(p)
            if (
                request["operation"] != p.operation_id
                or request["epoch"] != str(p.epoch)
                or request["destination"] != self.destination
                or request["watchdog"] != profile.execution.watchdog_profile
                or request["max_seconds"] > p.selection.resources.max_seconds
                or request["max_output_bytes"] > profile.execution.max_output_bytes
                or request["max_output_files"] > profile.execution.max_output_files
                or reservation != canonical_digest([body["route_id"], body["route_digest"]])
                or body["route_digest"] != canonical_digest(package.model_dump(mode="json"))
                # The guardian's outer stop covers the approved route window
                # (allocation + image pull + workload), not only the workload's
                # own runtime argument, which is already bounded by that window.
                or not now < body["deadline"] <= now + p.selection.resources.max_seconds
            ):
                raise ValueError("reservation_mismatch")
            binding = CustodyBinding(
                body["org_id"],
                body["project_id"],
                p.operation_id,
                p.epoch,
                profile.execution.artifact_destination,
            )
            serialized = p.model_dump_json()
            self._require_stop_coverage(body["deadline"])
            rate, rate_at = self._rate({"proposal": serialized})
            # Only billed (running) time is charged: the window is the workload's
            # own runtime argument, never longer than the route window.
            running = self._running_window(profile.execution, request, body["deadline"] - now)
            # Billed time can exceed the running window by up to one unseen poll gap.
            billed = running + UNSEEN_RUN_GAP_SECONDS
            if rate * Decimal(str(billed)) * 100 / 3600 > profile.execution.max_cost_cents:
                raise ValueError("budget_insufficient")
            # Statuses and accounting write, so gather them before the transaction.
            open_frames = self._open_billable_frames(p.operation_id)
            orphan = open_frames >= self.max_open_billable_frames
            if self.total_cap_cents is not None:
                self._committed(exclude=reservation)  # bring accounting up to date first
            if orphan and not self._over_total_cap(reservation, profile):
                # Receipted before the refusal: the transaction below rolls back, so the
                # record is written here (the total-cap refusal, checked first, is not a fence).
                self._record_fence_refusal(reservation, p.operation_id, open_frames)
            with self.db:
                # The total-cap check and the insert are one write transaction.
                self.db.execute("BEGIN IMMEDIATE")
                if self.total_cap_cents is not None and (
                    self._committed(exclude=reservation, account=False)
                    + Decimal(profile.execution.max_cost_cents)
                    > self.total_cap_cents
                ):
                    raise ValueError("total_budget_exhausted")
                if orphan:
                    raise ValueError("single_operation_orphan_exposure")
                self.db.execute(
                    "INSERT OR IGNORE INTO reservations(reservation,operation,tenant,project,"
                    "proposal,binding,deadline,heartbeat,routing_package,route_id,runtime_package,"
                    "budget_binding,running_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reservation,
                        p.operation_id,
                        body["org_id"],
                        body["project_id"],
                        serialized,
                        binding.key(),
                        body["deadline"],
                        now,
                        package.model_dump_json(),
                        body["route_id"],
                        encoded(runtime).decode(),
                        budget_json,
                        running,
                    ),
                )
                self._frame_note(reservation, "reserved", once=True)
                row = self._row(reservation)
                if (
                    row["proposal"] != serialized
                    or row["binding"] != binding.key()
                    or row["routing_package"] != package.model_dump_json()
                    or row["route_id"] != body["route_id"]
                    or row["runtime_package"] != encoded(runtime).decode()
                    or row["budget_binding"] != budget_json
                    or body["deadline"] < row["deadline"]
                ):
                    raise ValueError("reservation_mismatch")
                if row["accounted_at"] is None:
                    self.db.execute(
                        "UPDATE reservations SET rate=?,rate_at=?,accounted_at=?,cost_cap=?,"
                        "rate_unknown=0 WHERE reservation=?",
                        (
                            format(rate, "f"),
                            rate_at,
                            now,
                            profile.execution.max_cost_cents,
                            reservation,
                        ),
                    )
            # Aggregate operation cap is allocated once. The fixed smoke keeps one
            # undeclared bounded output; workload_v1 binds every declared output
            # name and per-file cap (ADR 0036 phase 3). No checkpoint destinations.
            self.custody.register(
                binding,
                byte_cap=request["max_output_bytes"],
                file_cap=request["max_output_files"],
                expected_files=1 if declared is None else len(declared),
                outputs=declared,
            )
        row = self._row(reservation)
        if action == "input_ticket" and body["input_ticket"] is None:
            # Dropping is always allowed: no readiness, budget or stop_only check may keep a
            # held ticket alive.
            if self.inputs is None:
                raise ValueError("input_unconfigured")
            self.inputs.drop(row["operation"])
            return dict(reservation=reservation, state="ready", observed_at=self.loop_at)
        if action in {"bind", "ready", "heartbeat", "input_ticket"}:
            from .experiment_budget_models import SaladBudgetBinding

            budget = SaladBudgetBinding.model_validate_json(row["budget_binding"] or "{}")
            if budget.expires_at.timestamp() <= now:
                raise ValueError("budget_binding_expired")
            self._require_stop_coverage(row["deadline"])
        if action == "bind":
            self._budget(row, refresh=True)
            # Caller-supplied allocation/group is not evidence. Independently
            # reconcile the exact approved selection through authenticated API.
            _, group, _, _ = self._provider(row)
            if normalized_uuid(body["provider_id"]) != group:
                raise ValueError("provider_identity_mismatch")
            row = self._row(reservation)
            self.guardian.arm(self._assignment(row))
        elif action in {"heartbeat", "ready"}:
            if row["recovery"] or now >= self._stop_deadline(row):
                raise ValueError("stop_only")
            self._budget(row, refresh=True)
            if action == "heartbeat":
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET heartbeat=? WHERE reservation=?",
                        (now, reservation),
                    )
                if row["provider_id"]:
                    self.guardian.heartbeat(row["operation"])
        elif action == "input_ticket":
            # From the authenticated trusted server only; the workload cannot reach this.
            if self.inputs is None:
                raise ValueError("input_unconfigured")
            if row["recovery"] or now >= self._stop_deadline(row):
                raise ValueError("stop_only")
            proposal = RemoteProposal.model_validate_json(row["proposal"])
            max_bytes = proposal.selection.resources.disk_mb * 1024 * 1024
            execution = proposal.selection.salad.execution
            if execution is not None and execution.argv_binding == "workload_v1":
                # A generic workload receives input only when its reviewed contract
                # declares one (bound to manifests.data by the selection model).
                declared = execution.workload.input
                if declared is None:
                    raise ValueError("input_not_declared")
                max_bytes = min(max_bytes, declared.max_bytes)
            # Installed only after the readiness checks below: a refused call holds nothing.
            install = dict(
                operation=row["operation"],
                ticket=body["input_ticket"],
                expected_digest=proposal.selection.manifests.data,
                max_bytes=max_bytes,
                # Re-read on every fetch: the stop deadline shrinks once the job runs.
                deadline=lambda: self._stop_deadline(self._row(reservation)),
            )
        elif action == "cancel":
            if self.inputs:
                self.inputs.drop(row["operation"])
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET recovery=1 WHERE reservation=?", (reservation,)
                )
            self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
            if row["provider_id"]:
                self._cancel(row["operation"])
        elif action == "status":
            return self._status(row)
        elif action == "create_rejected":
            # Like cancel, only fences: never gated on loop or budget readiness.
            return self._create_rejected(row, body["rejection"])
        elif action == "import":
            # A cancelled/unknown guardian is not a terminal provider readback.
            # Sealing here would otherwise close writers and expose retained bytes
            # while the GPU could still be running (or its allocation uncertain).
            if (
                not row["provider_id"]
                or self.guardian.status(row["operation"])["status"] != "stopped"
            ):
                raise ValueError("writers_not_stopped")
            binding = CustodyBinding(**json.loads(row["binding"]))
            manifest = self.custody.seal_operation(binding)
            if not self.custody.uploaded_chunked(binding) and (
                self.custody.retained_outputs(binding) is None
                or [f["name"] for f in manifest["inventories"][0]["files"]] == [LEGACY_OUTPUT_NAME]
            ):
                # Original single-object reply: the fixed smoke, or a single
                # output.json retained through the original endpoint.
                return dict(
                    manifest=manifest,
                    output=strict_json(self.custody.read_retained(binding, LEGACY_OUTPUT_NAME)),
                )
            # Chunked import: the manifest lists per-file sha256 and bytes; the
            # importer pulls each file with import_chunk and verifies it.
            return dict(manifest=manifest, chunk_bytes=OUTPUT_CHUNK_BYTES)
        elif action == "import_chunk":
            if (
                not row["provider_id"]
                or self.guardian.status(row["operation"])["status"] != "stopped"
            ):
                raise ValueError("writers_not_stopped")
            binding = CustodyBinding(**json.loads(row["binding"]))
            # Sealing is idempotent; it closes writers before any byte is served.
            self.custody.seal_operation(binding)
            if type(body["name"]) is not str or type(body["seq"]) is not int:
                raise ValueError("invalid_admin_request")
            return self.custody.read_chunk(binding, body["name"], body["seq"])
        elif action == "reconcile":
            return self._reconcile(row, body["attestation"])
        elif action not in {"reserve", "bind"}:
            raise ValueError("unsupported_operation")
        finished = self.clock()
        if (
            self.loop_at is None
            or type(accepted) not in {int, float}
            or not math.isfinite(accepted)
            or accepted > finished
            or not 0 <= accepted - self.loop_at <= ADMIN_LOOP_FRESH_SECONDS
        ):
            raise ValueError("guardian_loop_unready")
        if finished - accepted > ADMIN_ACCEPT_WINDOW_SECONDS:
            # Normally unreachable: the request alarm fires first.
            raise ValueError("admin_deadline_exceeded")
        if self.jwt.refreshed is None or not 0 <= self.clock() - self.jwt.refreshed <= 3600:
            raise ValueError("workload_verifier_unready")
        # Verify durable custody closure and readable manifest state, not config flags.
        if action in {"reserve", "ready", "bind", "heartbeat", "input_ticket"}:
            inventory = self.custody.db.execute(
                "SELECT closed FROM inventories WHERE binding=?", (row["binding"],)
            ).fetchone()
            if not inventory or inventory[0] or row["recovery"]:
                raise ValueError("custody_unready")
            if row["provider_id"]:
                state = self.guardian.status(row["operation"])
                if state["status"] != "armed" or state["allocation_uncertain"]:
                    raise ValueError("guardian_unready")
        if action == "input_ticket":
            self.inputs.install(
                install["operation"],
                install["ticket"],
                expected_digest=install["expected_digest"],
                max_bytes=install["max_bytes"],
                deadline=install["deadline"],
            )
        if action == "ready":
            # A create may follow this admission; a rejection report must come after it.
            admitted = self.clock()
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET last_ready_at=? WHERE reservation=?",
                    (admitted, reservation),
                )
                self._frame_note(reservation, "ready", {"create_admitted_at": admitted})
        return dict(reservation=reservation, state="ready", observed_at=self.loop_at)

    def loop(self):
        """One guardian pass; retries of each provider-read kind share one window in it."""
        self.retry_windows = {}
        try:
            self._loop_pass()
        finally:
            self.retry_windows = None

    def _loop_pass(self):
        now = self.clock()
        if self.jwt.refreshed is None or now - self.jwt.refreshed >= 1800:
            try:
                with alarm_budget(2):
                    self.jwt.refresh()
            except Exception:
                pass
        for (reservation,) in self.db.execute("SELECT reservation FROM reservations").fetchall():
            row = self._row(reservation)
            if self._lost_create_closed(row):
                if self.inputs:
                    self.inputs.drop(row["operation"])
                # Idempotent: an interrupted resolution never leaves writers open.
                self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
                resolved_at = json.loads(row["lost_create_resolution"])["resolved_at"]
                watching = now - resolved_at < LOST_CREATE_WATCH_SECONDS
                if watching and now >= self.next_absence.get(reservation, 0):
                    self.next_absence[reservation] = now + LOST_CREATE_WATCH_INTERVAL_SECONDS
                    self._watch_lost_create(row, now)
                continue
            self._account(row)
            if self.inputs and row["recovery"]:
                self.inputs.drop(row["operation"])
            if row["provider_id"]:
                try:
                    if self.guardian.status(row["operation"])["status"] in {
                        "stopped",
                        "reconciled",
                    }:
                        if self.inputs:
                            self.inputs.drop(row["operation"])
                        self.next_close_check.pop(reservation, None)
                        continue
                except ValueError:
                    pass
            expired = (
                now >= self._stop_deadline(row)
                or now - row["heartbeat"] >= 30
                or now < row["heartbeat"]
            )
            if now >= self.next_rate.get(reservation, 0):
                try:
                    self._budget(row, refresh=True)
                except Exception:
                    expired = True
                    with self.db:
                        self.db.execute(
                            "UPDATE reservations SET budget_fenced=1,rate_unknown=1 "
                            "WHERE reservation=?",
                            (reservation,),
                        )
                self.next_rate[reservation] = self.clock() + 5
            if expired and not row["recovery"]:
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET recovery=1 WHERE reservation=?", (reservation,)
                    )
                row["recovery"] = 1
                if self.inputs:
                    self.inputs.drop(row["operation"])
            if not row["provider_id"]:
                if row["recovery"]:
                    # Lost-create recovery is read by immutable reserved name only.
                    try:
                        self._provider(row)
                        row = self._row(reservation)
                        self.guardian.arm(self._assignment(row), stop_only=True)
                    except Exception:
                        self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
                        row = self._row(reservation)
                        margin = now >= row["deadline"] + LOST_CREATE_MARGIN_SECONDS
                        eligible = self._rejection_eligible_at(row)
                        rejected = eligible is not None and now >= eligible
                        if (
                            not row["provider_id"]
                            and (margin or rejected)
                            and now >= self.next_absence.get(reservation, 0)
                        ):
                            self.next_absence[reservation] = now + LOST_CREATE_READ_SECONDS
                            try:
                                # Past the margin the ordinary path applies even after
                                # a reported rejection; before it only the fast path.
                                self._resolve_lost_create(row, rejected=not margin)
                            except Exception:
                                pass  # stays reserved and blocking
                        continue
                else:
                    continue
            if row["recovery"]:
                self.guardian.arm(self._assignment(row), stop_only=True)
                self._cancel(row["operation"])
            else:
                self.guardian.arm(self._assignment(row))
            state = self.guardian.status(row["operation"])
            urgent = row["recovery"] and state["status"] == "armed"
            if now < self.next_poll.get(reservation, 0) and not urgent:
                continue
            try:
                self.guardian.tick(row["operation"])
                after = self.guardian.status(row["operation"])
                if after["status"] == "stopped":
                    self._account(self._row(reservation), force=True)
                elif after["status"] == "unknown" and (
                    after["allocation_uncertain"] or self._row(reservation)["allocation_uncertain"]
                ):
                    # The guardian cannot self-confirm after an allocation change, but
                    # the provider's exact read can end the spend: stop retrying the
                    # stop, freeze accrual, release the fence (custody stays unsealed).
                    # Throttled: the close check is one more exact provider read.
                    if now >= self.next_close_check.get(reservation, 0):
                        self.next_close_check[reservation] = now + CLOSE_CHECK_SECONDS
                        try:
                            self._close_uncertain_stopped(self._row(reservation))
                        except ValueError:
                            pass  # still running, unreadable or ambiguous: keep stopping
            except Exception:
                # No failed observation/custody write is a confirmed stop.
                pass
            # A pre-start read failure being retried across polls backs off (bounded).
            try:
                backoff = self.guardian.read_retry_backoff(row["operation"])
            except Exception:
                backoff = None
            self.next_poll[reservation] = self.clock() + max(5, backoff or 0)
        self.loop_at = self.clock()

    def _input(self, row, body):
        """Serve the held, verified snapshot to the identity-checked bound workload."""
        kind = body.get("request")
        if (
            self.inputs is None
            or not row["begun"]
            or row["recovery"]
            or row["budget_fenced"]
            or row["rate_unknown"]
            or kind not in {"manifest", "chunk"}
        ):
            # Only after begin-once (defense in depth: the ticket alone already scopes it).
            # The loop re-reads the published rate every five seconds and sets these
            # fences; unlike begin-once, input calls add no Salad rate read of their own.
            raise ValueError("workload_rejected")
        allowed = (
            {"binding", "request"}
            if kind == "manifest"
            else {"binding", "request", "seq", "offset", "length"}
        )
        if set(body) != allowed:
            raise ValueError("workload_rejected")
        if kind == "manifest":
            return self.inputs.manifest(row["operation"])
        return self.inputs.chunk(row["operation"], body["seq"], body["offset"], body["length"])

    def input_reply_failed(self, body, result):
        """The handler could not send an input reply: refund its served-byte charge."""
        if self.inputs is None or type(body) is not dict or type(result) is not dict:
            return
        binding = body.get("binding")
        operation = binding.get("operation") if type(binding) is dict else None
        if type(operation) is str and result.get("schema") == "tessera.input-range.v1":
            self.inputs.refund(operation, result.get("length"))

    def workload(self, path, body, token, accepted_at=None):
        """Bound-workload request; a refusal's closed code is recorded for diagnosis."""
        try:
            return self._workload(path, body, token, accepted_at)
        except Exception as error:
            self._record_workload_refusal(body, refusal_code(error))
            raise

    def _last_workload_refusal(self, reservation):
        last = self.db.execute(
            "SELECT detail FROM events WHERE reservation=? AND kind='workload_refused' "
            "ORDER BY sequence DESC LIMIT 1",
            (reservation,),
        ).fetchone()
        return last[0] if last else None

    def _record_workload_refusal(self, body, code):
        """Durable, deduplicated record of why a workload call was refused.

        ADVISORY: this runs before the call's token has bound, and the operation id comes
        from the request body, so a caller that knows an operation id can influence which
        code is recorded for it. It is a diagnostic hint, never evidence of what the
        container did and never an input to any stop, budget or custody decision.

        Only the closed refusal code (never the token, body or provider reply) is kept,
        at most once per code per reservation per WORKLOAD_REFUSAL_DEDUP_SECONDS, so a
        container that retries cannot flood the table. Failures here never mask the
        refusal itself.
        """
        try:
            binding = body.get("binding") if type(body) is dict else None
            operation = binding.get("operation") if type(binding) is dict else None
            if type(operation) is not str:
                return
            row = self._row(operation=operation)
            now = self.clock()
            last = self.db.execute(
                "SELECT at FROM events WHERE reservation=? AND kind='workload_refused' "
                "AND detail=? ORDER BY sequence DESC LIMIT 1",
                (row["reservation"], code),
            ).fetchone()
            if last and 0 <= now - last[0] < WORKLOAD_REFUSAL_DEDUP_SECONDS:
                return
            with self.db:
                self.db.execute(
                    "INSERT INTO events(reservation,operation,at,kind,detail) "
                    "VALUES(?,?,?,'workload_refused',?)",
                    (row["reservation"], operation, now, code),
                )
        except Exception:
            self.diagnostic_write_failures += 1

    def _workload(self, path, body, token, accepted_at=None):
        """Bound-workload request. ``accepted_at`` is the service clock when the
        listener accepted the connection, before the TLS handshake and body read.

        Loop freshness is judged at acceptance: the loop and request handling share
        one thread, so a slow body upload delays the next loop pass but cannot make
        a stale loop look fresh. Deadlines and heartbeats are still judged now.
        """
        request = body["binding"]
        if type(request) is not dict:
            raise ValueError("workload_rejected")
        now = self.clock()
        accepted = now if accepted_at is None else accepted_at
        row = self._row(operation=request.get("operation"))
        proposal = RemoteProposal.model_validate_json(row["proposal"])
        expected = self._request_for(proposal)
        if (
            encoded(request) != encoded(expected)
            or row["recovery"]
            or now >= self._stop_deadline(row)
            or not row["provider_id"]
            or self.loop_at is None
            or type(accepted) not in {int, float}
            or accepted > now
            or accepted - self.loop_at > 2
        ):
            raise ValueError("workload_rejected")
        status = self.guardian.status(row["operation"])
        # Once every output is retained the guardian is stopping; only an exact
        # replay of an already stored chunk may still be acknowledged (read-only).
        # A crash between guardian.complete and the output flag leaves the guardian
        # in this state with output=0; the replay then repairs the flag.
        acknowledging = (
            path == OUTPUT_CHUNK_PATH
            and status["status"] == "unknown"
            and status["reason"] == "output_retained"
        )
        if status["allocation_uncertain"] or not (status["status"] == "armed" or acknowledging):
            raise ValueError("workload_rejected")
        if path in {OUTPUT_PLAN_PATH, OUTPUT_CHUNK_PATH}:
            self._charge_output_request(row, proposal, token)
        if path == INPUT_PATH:
            self._charge_input_request(row, proposal, token)
        # A container may call right after start, before the next loop tick binds
        # its allocation; this fresh read binds a running instance first.
        observation, group, allocation, machine = self._provider(row)
        row = self._row(row["reservation"])
        instances = observation.get("instances") or []

        def verify(machine_id):
            self.jwt.verify(
                token,
                organization_id=self.organization_ids[proposal.selection.salad.organization],
                organization_name=proposal.selection.salad.organization,
                project_name=proposal.selection.salad.project,
                group_id=group,
                group_name=proposal.selection.salad.group,
                machine_id=machine_id,
            )

        if not row["machine"] and not row["allocation_uncertain"] and len(instances) == 1:
            # The container can run, and call, before Salad's API reports its
            # instance as running. A Salad-signed token from the single instance's
            # exact machine proves it runs there, so that instance may be bound;
            # a token from any other machine is refused. Only a group that is
            # deploying or running, with an instance that is not stopping, binds.
            instance = instances[0]
            if observation.get("state") not in {"deploying", "running"} or instance.get(
                "state"
            ) not in PRE_START | {"running"}:
                raise ValueError("workload_rejected")
            candidate = normalized_uuid(instance["machine_id"])
            verify(candidate)
            if self.clock() >= self._stop_deadline(row) or self.clock() - row["heartbeat"] >= 30:
                raise ValueError("workload_rejected")
            bound = normalized_uuid(instance["instance_id"]) + ":" + candidate
            with self.db:
                changed = self.db.execute(
                    "UPDATE reservations SET allocation=?,machine=? "
                    "WHERE reservation=? AND allocation IS NULL AND machine IS NULL "
                    "AND allocation_uncertain=0 AND recovery=0",
                    (bound, candidate, row["reservation"]),
                ).rowcount
            if changed != 1:
                raise ValueError("workload_rejected")
            row = self._row(row["reservation"])
            allocation, machine = bound, candidate
        elif [i.get("state") for i in instances] != ["running"]:
            # Salad can keep the group "deploying" while its instance already
            # runs; judge the bound instance itself.
            raise ValueError("workload_rejected")
        else:
            verify(machine)
        if (
            not row["machine"]
            or row["allocation_uncertain"]
            or allocation != row["allocation"]
            or machine != row["machine"]
        ):
            raise ValueError("workload_rejected")
        if self.clock() >= self._stop_deadline(row) or self.clock() - row["heartbeat"] >= 30:
            raise ValueError("workload_rejected")
        if path == "/v1/workload/input":
            return self._input(row, body)
        if path == "/v1/workload/begin-once":
            self._budget(row, refresh=True)
            if set(body) != {"binding"}:
                raise ValueError("workload_rejected")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                changed = self.db.execute(
                    "UPDATE reservations SET begun=1,"
                    "first_running_at=COALESCE(first_running_at,last_unseen_at,?),"
                    "seen_running_at=COALESCE(seen_running_at,?) "
                    "WHERE operation=? AND begun=0 AND recovery=0",
                    (
                        self.clock() - UNSEEN_RUN_GAP_SECONDS,
                        self.clock(),
                        row["operation"],
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("operation_consumed")
            return dict(
                schema="tessera.begin-once.v1",
                granted=True,
                operation=request["operation"],
                epoch=request["epoch"],
            )
        if path in {OUTPUT_PLAN_PATH, OUTPUT_CHUNK_PATH}:
            return self._chunked_output(path, row, proposal, request, body)
        if (
            path != "/v1/workload/output"
            or set(body) != {"binding", "sha256", "output"}
            or not row["begun"]
        ):
            raise ValueError("workload_rejected")
        work = proposal.selection.salad.execution.workload
        if work is not None and not work.legacy_single_output():
            # Multi-file or large outputs only travel through chunked custody.
            raise ValueError("workload_rejected")
        data = encoded(body["output"])
        digest = hashlib.sha256(data).hexdigest()
        if digest != body["sha256"] or len(data) > expected["max_output_bytes"]:
            raise ValueError("output_rejected")
        binding = CustodyBinding(**json.loads(row["binding"]))
        # Durable ticket reservation before byte insertion. A lost response or
        # crash never overwrites/reissues this filename or clears operation begun.
        ticket = self.custody.issue(
            binding,
            name=LEGACY_OUTPUT_NAME,
            checksum=digest,
            size=len(data),
            expires=min(self._stop_deadline(row), self.clock() + 60),
        )
        self.custody.upload(ticket, binding, data)
        self._outputs_complete(row)
        return dict(
            schema="tessera.retained.v1",
            retained=True,
            sha256=digest,
            operation=request["operation"],
            epoch=request["epoch"],
        )

    def _outputs_complete(self, row):
        """Every approved output is durable: stop now, before the exited one-shot
        container can be replaced by a second, unapproved allocation.

        Idempotent and ordered for crash safety: the durable stop intent comes
        first, then the output flag. An interruption after the stop intent still
        stops the job; one before it leaves a state a retried chunk re-completes.
        """
        self.guardian.complete(row["operation"])
        with self.db:
            self.db.execute(
                "UPDATE reservations SET output=1 WHERE reservation=?", (row["reservation"],)
            )
        if self.inputs:
            self.inputs.drop(row["operation"])
        self.next_poll[row["reservation"]] = 0  # tick on the very next loop

    def _charge_output_request(self, row, proposal, token):
        """Charge one authenticated chunked-output request before any provider read.

        Only a token signed for this reservation's bound group and machine, with
        the exact binding already matched, is charged, so an anonymous caller
        cannot burn a legitimate operation's budget. Refused requests still count.
        """
        if not row["begun"] or not row["machine"] or row["allocation_uncertain"]:
            raise ValueError("workload_rejected")
        salad = proposal.selection.salad
        self.jwt.verify(
            token,
            organization_id=self.organization_ids[salad.organization],
            organization_name=salad.organization,
            project_name=salad.project,
            group_id=row["provider_id"],
            group_name=salad.group,
            machine_id=row["machine"],
        )
        declared = self._declared_outputs(proposal) or []
        budget = 2 * sum(output_chunk_count(o["max_bytes"]) for o in declared)
        with self.db:
            charged = self.db.execute(
                "UPDATE reservations SET output_requests=output_requests+1 "
                "WHERE reservation=? AND output_requests<?",
                (row["reservation"], budget + OUTPUT_REQUEST_SLACK),
            ).rowcount
        if charged != 1:
            raise ValueError("output_request_budget_exhausted")

    def _charge_input_request(self, row, proposal, token):
        """Charge one authenticated staged-input request before any provider read.

        Same identity rule as ``_charge_output_request``: only a token for this
        reservation's bound group and machine, with the binding already matched,
        after begin-once. Refused requests (bad ranges, failed fetches) still count,
        so a workload cannot loop reads into the shared provider key's rate limits.
        """
        if not row["begun"] or not row["machine"] or row["allocation_uncertain"]:
            raise ValueError("workload_rejected")
        salad = proposal.selection.salad
        self.jwt.verify(
            token,
            organization_id=self.organization_ids[salad.organization],
            organization_name=salad.organization,
            project_name=salad.project,
            group_id=row["provider_id"],
            group_name=salad.group,
            machine_id=row["machine"],
        )
        planned = self.inputs.planned_requests(row["operation"]) if self.inputs else 1
        with self.db:
            charged = self.db.execute(
                "UPDATE reservations SET input_requests=input_requests+1 "
                "WHERE reservation=? AND input_requests<?",
                (row["reservation"], 2 * planned + INPUT_REQUEST_SLACK),
            ).rowcount
        if charged != 1:
            raise ValueError("input_request_budget_exhausted")

    def _chunked_output(self, path, row, proposal, request, body):
        """Declared multi-file output custody (ADR 0036 phase 3), workload_v1 only.

        The caller already verified the IMDS identity, binding and deadline. A chunk
        is idempotent per (name, seq); custody enforces the declared names, per-file
        and total caps, chunk geometry, per-chunk and whole-file sha256 and a bounded
        chunk-call budget. Stop on retained output fires only once every declared
        output is complete; a partial upload never extends the stop deadline.
        """
        declared = self._declared_outputs(proposal)
        if (
            declared is None
            or not row["begun"]
            or row["budget_fenced"]
            or row["rate_unknown"]
            or (row["output"] and path != OUTPUT_CHUNK_PATH)
        ):
            raise ValueError("workload_rejected")
        if path == OUTPUT_PLAN_PATH:
            if set(body) != {"binding"}:
                raise ValueError("workload_rejected")
            return dict(
                schema="tessera.output-plan.v1",
                operation=request["operation"],
                epoch=request["epoch"],
                chunk_bytes=OUTPUT_CHUNK_BYTES,
                outputs=declared,
            )
        fields = {"binding", "name", "sha256", "bytes", "seq", "chunk_sha256", "content_base64"}
        content = body.get("content_base64")
        if (
            set(body) != fields
            or type(content) is not str
            or len(content) > (OUTPUT_CHUNK_BYTES + 2) // 3 * 4
        ):
            raise ValueError("workload_rejected")
        try:
            data = base64.b64decode(content.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError):
            raise ValueError("workload_rejected") from None
        if len(data) > OUTPUT_CHUNK_BYTES:
            raise ValueError("workload_rejected")
        binding = CustodyBinding(**json.loads(row["binding"]))
        chunk = dict(
            name=body["name"],
            checksum=body["sha256"],
            size=body["bytes"],
            seq=body["seq"],
            chunk_sha256=body["chunk_sha256"],
            data=data,
        )
        guardian = self.guardian.status(row["operation"])
        if row["output"] or (
            guardian["status"] != "armed" and guardian["reason"] == "output_retained"
        ):
            # Lost final response (or a crash before the output flag): acknowledge an
            # exact replay of a stored chunk. Read-only; nothing reopens and the stop
            # is not delayed. Completion below re-runs idempotently and sets the flag.
            result = self.custody.chunk_ack(binding, **chunk)
        else:
            result = self.custody.put_chunk(binding, expires=self._stop_deadline(row), **chunk)
        if result["outputs_complete"]:
            # Durable stop intent first, then close writers; both idempotent, so a
            # retried chunk repairs an interruption between them.
            self._outputs_complete(row)
            self.custody.close_writers(binding)
        return dict(
            schema="tessera.output-chunk.v1",
            operation=request["operation"],
            epoch=request["epoch"],
            name=body["name"],
            seq=body["seq"],
            stored=True,
            **result,
        )


class SafetyHTTPServer(HTTPServer):
    """Bounded single-thread listener; handlers cannot race the guardian owner."""

    def __init__(self, address, service, admin_token, *, context):
        if len(admin_token) < 32:
            raise ValueError("admin_credential_unavailable")
        self.service, self.admin_token = service, admin_token
        self.accepted_at = None
        self.accepted_monotonic = None
        super().__init__(address, SafetyHandler)
        self.timeout = 0.2
        self.socket = context.wrap_socket(
            self.socket, server_side=True, do_handshake_on_connect=False
        )

    def get_request(self):
        request, address = super().get_request()
        self.accepted_monotonic = time.monotonic()
        # Loop freshness is judged at acceptance, before the handshake and body.
        self.accepted_at = self.service.clock()
        request.settimeout(2)
        try:
            request.do_handshake()
        except Exception:
            request.close()
            raise
        return request, address

    def finish_request(self, request, address):
        # Socket idle timeouts alone permit endless trickling. This process owns
        # its main thread and signals; cap the entire parse/auth/dispatch budget.
        # The clock started at accept: only the headers phase is armed here, and a
        # request extends it (extend_deadline) once its pre-auth check has passed.
        elapsed = time.monotonic() - (self.accepted_monotonic or time.monotonic())
        with alarm_budget(max(0.05, HEADER_READ_SECONDS - elapsed)):
            super().finish_request(request, address)

    def extend_deadline(self, total_seconds):
        """Re-arm the request alarm to ``total_seconds`` after accept (never beyond)."""
        if self.accepted_monotonic is None or signal.getitimer(signal.ITIMER_REAL)[0] == 0:
            return
        left = total_seconds - (time.monotonic() - self.accepted_monotonic)
        signal.setitimer(signal.ITIMER_REAL, max(0.05, left))

    def handle_error(self, request, address):
        pass  # Never emit HTTP/provider exception tracebacks.


class SafetyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No URL, token, body, claims or raw exception logging.

    def do_POST(self):
        status = 400
        try:
            if (
                self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type") != "application/json"
                or len(self.headers.get_all("Content-Length", [])) != 1
                or len(self.headers.get_all("Authorization", [])) != 1
            ):
                raise ValueError("invalid_request")
            length = int(self.headers["Content-Length"])
            cap = OUTPUT_CHUNK_BODY_BYTES if self.path == OUTPUT_CHUNK_PATH else REQUEST_BODY_BYTES
            if not 1 <= length <= cap:
                raise ValueError("invalid_request")
            auth = self.headers["Authorization"]
            if not auth.startswith("Bearer ") or len(auth) > 2048:
                raise ValueError("invalid_auth")
            if self.path != "/v1/admin" and self.path not in WORKLOAD_PATHS:
                raise ValueError("invalid_request")
            # Cheap pre-auth, before any body byte is read or the request deadline
            # is extended: the admin token, or a Salad JWT that parses, is unexpired
            # and verifies against the cached JWKS (no network, no tenant scope; the
            # full scope check still runs after the body).
            status = 401
            if self.path == "/v1/admin":
                if not hmac.compare_digest(auth[7:].encode(), self.server.admin_token):
                    raise ValueError("invalid_auth")
            else:
                precheck = getattr(getattr(self.server.service, "jwt", None), "precheck", None)
                if precheck is not None:
                    precheck(auth[7:])
            status = 400
            extend = getattr(self.server, "extend_deadline", None)
            if extend is not None:
                extend(
                    CHUNK_REQUEST_SECONDS
                    if self.path in {OUTPUT_CHUNK_PATH, INPUT_PATH}
                    else REQUEST_SECONDS
                )
            body = strict_json(self._read_body(length))
            if type(body) is not dict:
                raise ValueError("invalid_request")
            if self.path == "/v1/admin":
                status = 401
                if not hmac.compare_digest(auth[7:].encode(), self.server.admin_token):
                    raise ValueError("invalid_auth")
                # No acceptance stamp is never the lenient direct-call default: NaN
                # fails the loop-freshness check (guardian_loop_unready) while still
                # letting cancel/status/import, which need no fresh loop, proceed.
                accepted = getattr(self.server, "accepted_at", None)
                result = self.server.service.admin(
                    body, accepted_at=float("nan") if accepted is None else accepted
                )
            elif self.path in WORKLOAD_PATHS:
                status = 401
                result = self.server.service.workload(
                    self.path, body, auth[7:], accepted_at=self.server.accepted_at
                )
            else:
                raise ValueError("invalid_request")
            try:
                self._reply(200, result)
            except Exception:
                if self.path == INPUT_PATH:
                    # Cut off on the wire (timeout, reset or the request alarm): the
                    # workload retries the range, so the bytes never count as served.
                    self.server.service.input_reply_failed(body, result)
                # Nothing more is written to a connection that failed mid-reply.
                self.close_connection = True
                return
        except Exception as error:
            log_unclosed_refusal(error, "https")
            self._reply(status, {"error": "request_rejected"})

    def _read_body(self, length):
        """Read exactly ``length`` bytes under one wall-clock deadline.

        A per-recv socket timeout lets a client trickle bytes and stall the shared
        guardian loop; this bounds the whole body to ``body_read_seconds(length)``,
        i.e. the documented 1.5 Mbit/s floor rate plus a one second grace.
        """
        deadline = time.monotonic() + body_read_seconds(length)
        chunks, remaining = [], length
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0:
                self.close_connection = True
                raise ValueError("invalid_request")
            self.connection.settimeout(min(left, 2))
            piece = self.rfile.read1(min(remaining, 65536))
            if not piece:
                raise ValueError("invalid_request")
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)

    def _reply(self, status, value):
        raw = encoded(value)
        try:
            # _read_body leaves a short per-recv timeout behind; sending needs its own.
            self.connection.settimeout(2)
        except (AttributeError, OSError):
            pass
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()


class SafetyHTTPSClient:
    def __init__(self, host, port, *, token_file, ca_file, certificate_sha256):
        self.host, self.port, self.token_file = host, port, token_file
        self.context = ssl.create_default_context(cafile=ca_file)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.pin = certificate_sha256

    def exchange(self, body, cap=131072):
        token = private_read(self.token_file, 256).strip()
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=10, context=self.context
        )
        try:
            connection.connect()
            if (
                hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                != self.pin
            ):
                raise ValueError("service_identity_mismatch")
            connection.request(
                "POST",
                "/v1/admin",
                encoded(body),
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token.decode("ascii"),
                },
            )
            response = connection.getresponse()
            raw = response.read(cap + 1)
            if response.status != 200 or len(raw) > cap:
                raise ValueError("service_unavailable")
            return strict_json(raw)
        except Exception:
            raise ValueError("service_unavailable") from None
        finally:
            connection.close()


def serve(service, server, *, shutdown=lambda: False, unix_listener=None, proxy_uid=None):
    while not shutdown():
        service.loop()
        if unix_listener is not None:
            try:
                connection, _ = unix_listener.accept()
            except TimeoutError:
                pass
            else:
                with connection:
                    try:
                        # Loop freshness is judged here, before the frame read and work.
                        accepted_at = service.clock()
                        _, uid, _ = struct.unpack(
                            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        )
                        if uid != proxy_uid:
                            raise ValueError("proxy_identity_rejected")
                        raw = read_admin_frame(connection)
                        # Same whole-request budget as the HTTPS admin channel.
                        with alarm_budget(UNIX_ADMIN_BUDGET_SECONDS):
                            reply = service.admin(strict_json(raw), accepted_at=accepted_at)
                    except Exception as error:
                        # Trusted local peer only: a closed code, never free text.
                        log_unclosed_refusal(error, "admin")
                        reply = {"error": "request_rejected", "code": refusal_code(error)}
                    try:
                        connection.sendall(encoded(reply) + b"\n")
                    except OSError:
                        # A departed client must never stop the guardian loop.
                        pass
        server.handle_request()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    if not args.run:
        print('{"state":"disabled","effects":0}')
        return 0
    if not args.config:
        return 2
    service = None
    try:
        cfg = strict_json(private_read(args.config, 16384))
        resolver = PrivateFileSecretResolver(cfg["credential_directory"])
        jwt = SaladWorkloadJWT()
        with alarm_budget(2):
            jwt.refresh()  # Explicit run only; fixed public JWKS, never provider effect.
        service = SaladSafetyService(
            cfg["state_directory"],
            organization_ids=cfg["organization_ids"],
            resolver=resolver,
            reference=cfg["credential_reference"],
            version=cfg["credential_version"],
            destination=cfg["destination"],
            verifier=jwt,
            total_cap_cents=cfg.get("total_cap_cents"),
            control_origin=cfg.get("control_origin"),
            max_open_billable_frames=cfg.get("max_open_billable_frames", 1),
            max_prestart_rebinds=cfg.get("max_prestart_rebinds", 3),
            prestart_read_deadline_seconds=cfg.get(
                "prestart_read_deadline_seconds", DEFAULT_PRESTART_READ_DEADLINE_SECONDS
            ),
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        private_read(cfg["tls_key"], 16384)
        context.load_cert_chain(cfg["tls_certificate"], cfg["tls_key"])
        token = private_read(cfg["admin_token_file"], 256).strip()
        server = SafetyHTTPServer(
            (cfg["listen_host"], cfg["listen_port"]), service, token, context=context
        )
        listener = None
        if cfg.get("unix_socket"):
            if not hasattr(socket, "SO_PEERCRED"):
                raise ValueError("linux_peer_credentials_required")
            socket_path = Path(cfg["unix_socket"])
            info = socket_path.parent.stat()
            if (
                info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or socket_path.exists()
            ):
                raise ValueError("unsafe_socket")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            listener.listen(8)
            listener.settimeout(0.01)
        serve(service, server, unix_listener=listener, proxy_uid=cfg.get("proxy_uid"))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('{"state":"unavailable","reason":"service_prerequisite_or_runtime_failure"}')
        return 2
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
