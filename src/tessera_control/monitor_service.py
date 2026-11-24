"""Opt-in execution-monitor receiver listener inside ``tessera-control serve`` (ADR 0127).

Nothing here runs unless the operator passes ``--monitor-enrollments FILE``. The file
is the only authority for a monitor's ``continue`` until control-side assignment
issuance exists: each challenge re-reads and re-validates it, and the answer is
``continue`` only while the entry is unchanged and active, the sealed assignment is
byte-identical and unexpired, and the assignment's worker credential is still live
in the control store. Anything else, including any error, answers ``revoke``.

The listener is TLS 1.3 with a required client certificate chained to the operator's
client CA and then pinned by SHA-256 to exactly one enrolled monitor before any
frame is read. Monitor keys are owner-private files, never inline values. Artifact
evidence stays refused (``MonitorReceiver``'s default custody callback) until a
custody integration exists. ``status()`` keeps reporting ``activation=blocked``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import selectors
import socket
import ssl
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from tessera_monitor.channel import Channel
from tessera_monitor.cli import private_read
from tessera_monitor.journal import MonitorJournal
from tessera_monitor.protocol import (
    Assignment,
    Denied,
    Digest,
    Envelope,
    Frozen,
    Name,
    encode,
    open_envelope,
)
from tessera_monitor.receiver import receive_one

from .monitor_evidence import MonitorReceiver

SCHEMA = "tessera.monitor-enrollments.v1"
MAX_ENROLLMENT_BYTES = 64 * 1024
MAX_ASSIGNMENT_BYTES = 64 * 1024
MAX_TLS_KEY_BYTES = 64 * 1024
MAX_MONITORS = 64
MAX_CONNECTIONS = 8
HANDSHAKE_SECONDS = 5.0
FRAME_SECONDS = 5.0
CONNECTION_SECONDS = 600.0
JOURNAL_DIRECTORY = "execution-monitors"

logger = logging.getLogger(__name__)


class EnrollmentRefused(ValueError):
    """Sanitized: names an entry and a reason, never a key, path or file content."""


class MonitorServiceError(RuntimeError):
    """Sanitized listener start failure."""


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


class ListenerSection(Frozen):
    bind_ip: str = Field(max_length=64)
    port: int = Field(ge=1, le=65535)
    server_cert: str = Field(min_length=2, max_length=1024)
    server_key: str = Field(min_length=2, max_length=1024)
    client_ca: str = Field(min_length=2, max_length=1024)


class EnrollmentEntry(Frozen):
    monitor: Name
    tenant: Name
    project: Name
    assignment: str = Field(min_length=2, max_length=1024)
    authority_key: str = Field(min_length=2, max_length=1024)
    channel_key: str = Field(min_length=2, max_length=1024)
    evidence_key: str = Field(min_length=2, max_length=1024)
    client_cert_sha256: Digest
    worker_credential_id: Name
    active: bool


class _Document(Frozen):
    schema_: Literal["tessera.monitor-enrollments.v1"] = Field(alias="schema")
    listener: dict[str, Any]
    monitors: list[Any] = Field(min_length=1, max_length=MAX_MONITORS)


@dataclass(frozen=True)
class Enrolled:
    entry: EnrollmentEntry
    document: Envelope
    assignment: Assignment
    keys: dict[str, bytes]


@dataclass(frozen=True)
class Enrollments:
    listener: ListenerSection
    monitors: dict[str, Enrolled]

    def by_pin(self) -> dict[str, Enrolled]:
        return {e.entry.client_cert_sha256: e for e in self.monitors.values()}


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("relative path")
    return path


def _read_enrollment_file(path: Path) -> bytes:
    # Same operator-file custody as --campaign-providers: absolute, no symlink in any
    # component, owner-only 0600, single link, bounded.
    from .salad_live_check import _private_bytes

    return _private_bytes(path, MAX_ENROLLMENT_BYTES)


def _listener(section: Any) -> ListenerSection:
    try:
        listener = ListenerSection.model_validate_json(json.dumps(section))
        ipaddress.ip_address(listener.bind_ip)  # explicit numeric IP, never a resolver
        for value in (listener.server_cert, listener.client_ca):
            _absolute(value)
    except Exception:
        raise EnrollmentRefused(
            "monitor enrollments refused: the listener needs an explicit numeric bind_ip, "
            "a port and absolute server_cert, server_key and client_ca paths"
        ) from None
    try:
        private_read(_absolute(listener.server_key), MAX_TLS_KEY_BYTES)
    except Exception:
        raise EnrollmentRefused(
            "monitor enrollments refused: the listener server key must be an owner-private "
            "regular file"
        ) from None
    return listener


def _entry(index: int, item: Any) -> Enrolled:
    try:
        entry = EnrollmentEntry.model_validate_json(json.dumps(item))
    except Exception:
        raise EnrollmentRefused(
            f"monitor enrollments refused: entry #{index + 1} has missing, unknown or invalid "
            "fields"
        ) from None
    name = f"monitor enrollment {entry.monitor!r} refused"
    try:
        keys = {
            field: private_read(_absolute(getattr(entry, field)), 32)
            for field in ("authority_key", "channel_key", "evidence_key")
        }
    except Exception:
        raise EnrollmentRefused(
            f"{name}: its keys must be absolute owner-private 32-byte files"
        ) from None
    if any(len(key) != 32 for key in keys.values()) or len(set(keys.values())) != 3:
        raise EnrollmentRefused(f"{name}: its keys must be three distinct 32-byte keys")
    try:
        document = Envelope.model_validate_json(
            private_read(_absolute(entry.assignment), MAX_ASSIGNMENT_BYTES)
        )
        assignment = open_envelope(document, Assignment, keys["authority_key"], b"assignment")
    except Exception:
        raise EnrollmentRefused(
            f"{name}: its assignment is not an owner-private sealed assignment that opens "
            "under its authority key"
        ) from None
    if (assignment.tenant, assignment.project, assignment.monitor) != (
        entry.tenant,
        entry.project,
        entry.monitor,
    ):
        raise EnrollmentRefused(
            f"{name}: its assignment's tenant, project or monitor differs from the entry"
        )
    return Enrolled(entry=entry, document=document, assignment=assignment, keys=keys)


def load_enrollments(path: Path | str) -> Enrollments:
    """Read and strictly validate the whole enrollment file, or refuse it."""
    from .salad_workload_jwt import strict_json

    try:
        path = _absolute(str(path))
        raw = _read_enrollment_file(path)
        top = strict_json(raw)
        if not isinstance(top, dict) or set(top) != {"schema", "listener", "monitors"}:
            raise ValueError
        document = _Document.model_validate_json(raw)
    except Exception:
        raise EnrollmentRefused(
            "monitor enrollments must be an owner-only (0600) absolute non-symlink JSON file "
            f"holding a {SCHEMA} document with a listener and at least one monitor"
        ) from None
    listener = _listener(document.listener)
    monitors: dict[str, Enrolled] = {}
    pins: set[str] = set()
    authority: set[bytes] = set()
    session: set[bytes] = set()
    for index, item in enumerate(document.monitors):
        enrolled = _entry(index, item)
        entry = enrolled.entry
        name = f"monitor enrollment {entry.monitor!r} refused"
        if entry.monitor in monitors:
            raise EnrollmentRefused(f"{name}: the monitor id is enrolled twice")
        if entry.client_cert_sha256 in pins:
            raise EnrollmentRefused(f"{name}: its client certificate pin is already enrolled")
        # One issuer may sign many assignments; channel/evidence keys are per monitor.
        own = {enrolled.keys["channel_key"], enrolled.keys["evidence_key"]}
        if own & session or own & authority or enrolled.keys["authority_key"] in session:
            raise EnrollmentRefused(
                f"{name}: its channel or evidence key is shared with another key"
            )
        monitors[entry.monitor] = enrolled
        pins.add(entry.client_cert_sha256)
        authority.add(enrolled.keys["authority_key"])
        session |= own
    return Enrollments(listener=listener, monitors=monitors)


def worker_credential_live(database_path: Path, entry: EnrollmentEntry) -> bool:
    """The entry's worker credential exists in its tenant/project, unrevoked and unexpired."""
    from .store import Store, _connect

    if not Path(database_path).is_file():
        return False  # never let the lookup create an empty database
    connection = _connect(database_path)
    try:
        row = connection.execute(
            """SELECT p.id, p.org_id, p.role, c.id AS credential_id, c.kind, c.audience,
            c.project_id, c.expires_at, c.revoked_at, c.parent_id
            FROM principals p JOIN credentials c ON c.principal_id = p.id
            WHERE c.id = ? AND p.org_id = ? AND p.revoked_at IS NULL""",
            (entry.worker_credential_id, entry.tenant),
        ).fetchone()
        if row is None:
            return False
        # Walks the parent chain: revocation, expiry, lifetime and structure.
        principal = Store._credential_principal(connection, row, "worker")
        return (
            principal["credential_id"] == entry.worker_credential_id
            and principal["kind"] == "worker"
            and principal["org_id"] == entry.tenant
            and principal["project_id"] == entry.project
        )
    finally:
        connection.close()


def make_authorizer(path: Path, database_path: Path, enrolled: Enrolled, *, now=wall_ms):
    """Server-side current-authority check for one receiver; any doubt is ``False``."""
    expected_entry = enrolled.entry.model_copy(update={"active": True})
    expected_document = encode(enrolled.document)

    def authorized(assignment) -> bool:
        try:
            current = load_enrollments(path).monitors.get(enrolled.entry.monitor)
            if current is None or current.entry.active is not True:
                return False
            if (
                current.entry != expected_entry
                or encode(current.document) != expected_document
                or current.keys != enrolled.keys
                or current.assignment != enrolled.assignment
                or assignment != enrolled.assignment
            ):
                return False
            if not now() < assignment.expires_ms:
                return False
            return worker_credential_live(database_path, current.entry) is True
        except Exception:
            return False

    return authorized


def _private_directory(path: Path) -> Path:
    if path.absolute() != path.resolve():
        raise MonitorServiceError("monitor journal ancestors must not be symlinks")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise MonitorServiceError("monitor journal directory must be owner-private")
    return path


def server_context(listener: ListenerSection) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=listener.client_ca)
    # Never prompt for an encrypted key in the service process.
    context.load_cert_chain(listener.server_cert, listener.server_key, password="")
    return context


class _Slot:
    def __init__(self, enrolled: Enrolled, receiver: MonitorReceiver, journal, authorized):
        self.enrolled, self.receiver, self.journal = enrolled, receiver, journal
        self.authorized = authorized
        self.busy = threading.Lock()


class MonitorService:
    """One ``MonitorReceiver`` and journal per enrolled monitor behind a pinned listener."""

    def __init__(self, enrollments_path: Path, state_dir: Path, *, now=wall_ms):
        self.path = Path(enrollments_path)
        self.state_dir = Path(state_dir)
        self.now = now
        self._slots: dict[str, _Slot] = {}
        self._pins: dict[str, _Slot] = {}
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._active: set[socket.socket] = set()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._started = False

    @property
    def address(self) -> tuple[str, int]:
        if self._listener is None:
            raise MonitorServiceError("monitor listener is not running")
        return self._listener.getsockname()[:2]

    def status(self) -> dict[str, dict[str, Any]]:
        return {name: slot.receiver.status() for name, slot in self._slots.items()}

    def start(self) -> None:
        if self._started:
            raise MonitorServiceError("monitor listener already started")
        self._started = True
        try:
            self._start()
        except (EnrollmentRefused, MonitorServiceError):
            self._close_all()
            raise
        except Exception:
            self._close_all()
            raise MonitorServiceError("execution monitor listener unavailable") from None

    def _start(self) -> None:
        from .store import Store

        enrollments = load_enrollments(self.path)
        database_path = Store(self.state_dir).database_path  # validates the control state
        root = _private_directory(self.state_dir / JOURNAL_DIRECTORY)
        for name, enrolled in enrollments.monitors.items():
            try:
                journal = MonitorJournal(root / name)
            except Exception:
                raise MonitorServiceError(
                    f"execution monitor {name!r}: its journal is unavailable"
                ) from None
            try:
                receiver = MonitorReceiver(
                    enrolled.document,
                    journal=journal,
                    tenant=enrolled.entry.tenant,
                    project=enrolled.entry.project,
                    monitor=enrolled.entry.monitor,
                    now=self.now,
                    # custody: the default refusal. Artifact evidence stays refused until
                    # a control-side custody integration exists.
                    **enrolled.keys,
                )
            except Exception:
                journal.close()
                raise MonitorServiceError(
                    f"execution monitor {name!r}: its journal holds a different assignment "
                    "or its receiver refused the enrollment"
                ) from None
            slot = _Slot(
                enrolled,
                receiver,
                journal,
                make_authorizer(self.path, database_path, enrolled, now=self.now),
            )
            self._slots[name] = slot
            self._pins[enrolled.entry.client_cert_sha256] = slot
        context = server_context(enrollments.listener)
        ip = ipaddress.ip_address(enrollments.listener.bind_ip)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((str(ip), enrollments.listener.port))
            listener.listen(MAX_CONNECTIONS)
            listener.settimeout(0.25)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            args=(listener, context),
            name="execution-monitor-listener",
            daemon=True,
        )
        self._accept_thread.start()

    def _accept_loop(self, listener: socket.socket, context: ssl.SSLContext) -> None:
        while not self._stopping.is_set():
            try:
                raw, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stopping.is_set():
                    logger.warning("execution monitor listener stopped: accept_failed")
                return
            if not self._connections.acquire(blocking=False):
                logger.warning("execution monitor connection refused: connection_limit")
                raw.close()
                continue
            thread = threading.Thread(
                target=self._connection,
                args=(raw, context),
                name="execution-monitor-connection",
                daemon=True,
            )
            with self._lock:
                self._threads.add(thread)
                self._active.add(raw)
            thread.start()

    def _track(self, old: socket.socket, new: socket.socket) -> None:
        with self._lock:
            self._active.discard(old)
            self._active.add(new)
        if self._stopping.is_set():
            _shutdown(new)

    def _handshake(self, raw: socket.socket, context: ssl.SSLContext) -> ssl.SSLSocket:
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        raw.setblocking(False)
        tls = context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
        self._track(raw, tls)
        with selectors.DefaultSelector() as selector:
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    events = selectors.EVENT_READ
                except ssl.SSLWantWriteError:
                    events = selectors.EVENT_WRITE
                left = deadline - time.monotonic()
                if left <= 0 or self._stopping.is_set():
                    raise Denied("handshake deadline")
                selector.register(tls, events)
                ready = selector.select(min(left, 0.25))
                selector.unregister(tls)
                if not ready and time.monotonic() >= deadline:
                    raise Denied("handshake deadline")
        if tls.version() != "TLSv1.3":
            raise Denied("TLS version")
        tls.setblocking(True)
        return tls

    def _connection(self, raw: socket.socket, context: ssl.SSLContext) -> None:
        sock: socket.socket = raw
        code = "closed"
        try:
            sock = self._handshake(raw, context)
            certificate = sock.getpeercert(binary_form=True)
            slot = self._pins.get(hashlib.sha256(certificate).hexdigest()) if certificate else None
            if slot is None:
                code = "unpinned_client"
                return
            if not slot.busy.acquire(blocking=False):
                code = "monitor_already_connected"
                return
            try:
                channel = Channel(sock, timeout=FRAME_SECONDS)
                ends = time.monotonic() + CONNECTION_SECONDS
                while not self._stopping.is_set() and time.monotonic() < ends:
                    receive_one(channel, slot.receiver, authorized=slot.authorized)
                code = "connection_lifetime"
            finally:
                slot.busy.release()
        except Denied as error:
            # Denied text is finite protocol vocabulary, never request content.
            code = str(error).replace(" ", "_")
        except ssl.SSLError:
            code = "tls_refused"
        except Exception as error:
            code = type(error).__name__
        finally:
            with self._lock:
                self._active.discard(raw)
                self._active.discard(sock)
                self._threads.discard(threading.current_thread())
            try:
                sock.close()
            except OSError:
                pass
            self._connections.release()
            if code not in ("channel_closed", "closed"):
                logger.warning("execution monitor connection closed: %s", code)

    def stop(self, timeout: float = 5.0) -> bool:
        """Close the listener and connections, then the journals. Idempotent."""
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout)
        with self._lock:
            active, threads = list(self._active), list(self._threads)
        for sock in active:
            _shutdown(sock)
        ends = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, ends - time.monotonic()))
        return self._close_all()

    def _close_all(self) -> bool:
        complete = True
        for slot in list(self._slots.values()):
            if slot.journal is None:
                continue
            if not slot.busy.acquire(blocking=False):
                # A connection thread did not exit: leave its journal to process exit
                # rather than closing it underneath a write.
                complete = False
                logger.warning("execution monitor shutdown incomplete: connection_busy")
                continue
            try:
                slot.journal.close()
                slot.journal = None
            finally:
                slot.busy.release()
        return complete


def _shutdown(sock: socket.socket) -> None:
    try:
        # Bypass SSLSocket.shutdown: unblock the reader without touching TLS state.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass
