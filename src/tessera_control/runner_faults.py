"""One table for what a store or filesystem error means to the campaign runner (ADR 0210).

Every runner site that has to decide whether an error is worth a replay asks ``classify``
instead of deciding in its own ``except``:

- ``contention``: a locked or busy database. Replayed; counts nothing anywhere.
- ``transient``: a fault a replay may get past, from a short explicit list only (``EAGAIN``,
  ``EINTR``, ``EBUSY``, ``ETIMEDOUT``, a timeout, a dropped or refused connection). Inside a
  step it is replayed with a persisted per-intent bound and a backoff, past which it is
  ``deterministic``; anywhere else in a campaign's pass it counts toward the breaker.
- ``io``: a brief I/O error (SQLite's "disk I/O error", ``EIO``). Replayed like a transient;
  a streak of ticks that each saw one becomes ``environment``. Never a campaign fault.
- ``environment``: the host cannot do work (disk full, quota, read-only filesystem, out of
  descriptors or memory, a database that cannot be opened, or a missing, unreadable or
  unmounted state directory). One host-level alarm, the runner pauses, and no campaign's
  breaker counts it; nothing needs re-approval.
- ``deterministic``: anything else, including an ``OSError`` whose errno is not listed or
  that has none (``EEXIST``, ``EXDEV``, ``EINVAL``, a ``URLError``). Inside a step it fails a
  forward step's intent (a reduce-only replay counts toward the breaker); elsewhere it counts
  toward the campaign's breaker. Only a missing blob for a recorded sha (``is_missing_blob``)
  fails an idle stage outright.

The host alarm is process-wide and in memory (a full disk cannot record it in the database);
``observability`` reports it on the operator health read while it stands.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

CONTENTION = "contention"
TRANSIENT = "transient"
IO = "io"
ENVIRONMENT = "environment"
DETERMINISTIC = "deterministic"
REPLAYABLE = frozenset({CONTENTION, TRANSIENT, IO, ENVIRONMENT})

# Replays of one pending intent's transient error before it is treated as deterministic.
TRANSIENT_REPLAY_LIMIT = 20
# Consecutive replays of one intent before its replays back off.
BACKOFF_AFTER_REPLAYS = 3
MAX_REPLAY_BACKOFF_SECONDS = 60.0
# Consecutive runner ticks that each saw a brief I/O error before it is the host's fault.
IO_STREAK_LIMIT = 5
# The runner's pause after a host fault: doubling from the first value to the cap.
HOST_PAUSE_SECONDS = 30.0
MAX_HOST_PAUSE_SECONDS = 300.0

# The persisted replay count of a pending intent, in its ``error_class`` column (no schema
# change): ``transient:<count>:<class>``. A done intent keeps it as history; a failed one
# overwrites it with the failure class.
REPLAY_PREFIX = "transient:"

_ENVIRONMENT_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("ENOSPC", "EDQUOT", "EROFS", "EMFILE", "ENFILE", "ENOMEM")
    )
    if code is not None
)
_TRANSIENT_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EAGAIN", "EWOULDBLOCK", "EINTR", "EBUSY", "ETIMEDOUT")
    )
    if code is not None
)
# A missing or unreadable path: deterministic for one file, the host's for the state root.
_ACCESS_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None) for name in ("ENOENT", "ENOTDIR", "EACCES", "EPERM", "ENODEV")
    )
    if code is not None
)
# A content-addressed blob is named by its sha-256 digest.
_SHA256_NAME = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_SQLITE_ENVIRONMENT = (
    "database or disk is full",
    "readonly database",
    "read-only",
    "unable to open database file",
    "out of memory",
)


class MissingBlob(FileNotFoundError):
    """A blob whose sha is recorded is gone from its store (for example removed by retention).
    The same on every pass, so it may fail an idle stage outright (``is_missing_blob``)."""

    def __init__(self, sha256: str, filename: str | None = None) -> None:
        super().__init__(errno.ENOENT, "recorded blob is missing", filename)
        self.sha256 = sha256


class HostFault(Exception):
    """The host cannot do work (``environment``): the runner pauses; no campaign is at fault."""

    def __init__(self, kind: str, error_class: str) -> None:
        super().__init__(f"{kind}: {error_class}")
        self.kind = kind[:40]
        self.error_class = error_class[:80]


def classify(error: BaseException, state_dir: os.PathLike[str] | str | None = None) -> str:
    """The class of one store or filesystem error (see the module docstring). With the
    runner's ``state_dir``, a missing or unreadable path that is the state root, an ancestor
    of it or one of its top-level entries, or any such error while the root itself is not
    usable, is the host's (``environment``), never one campaign's."""
    if isinstance(error, HostFault):
        return ENVIRONMENT
    if isinstance(error, sqlite3.OperationalError):
        text = str(error).lower()
        if "locked" in text or "busy" in text:
            return CONTENTION
        if "disk i/o" in text:
            return IO
        if any(marker in text for marker in _SQLITE_ENVIRONMENT):
            return ENVIRONMENT
        return DETERMINISTIC
    if isinstance(error, sqlite3.DatabaseError) and "disk is full" in str(error).lower():
        return ENVIRONMENT
    if isinstance(error, (TimeoutError, ConnectionError)):
        return TRANSIENT
    if isinstance(error, OSError):
        code = error.errno
        if code in _ENVIRONMENT_ERRNOS:
            return ENVIRONMENT
        if code == errno.EIO:
            return IO
        if code in _TRANSIENT_ERRNOS:
            return TRANSIENT
        if (
            code in _ACCESS_ERRNOS
            and state_dir is not None
            and not isinstance(error, MissingBlob)
            and state_root_fault(error, state_dir)
        ):
            return ENVIRONMENT
        # Any other errno, or none (``EEXIST``, ``EXDEV``, ``EINVAL``, a ``URLError``), is
        # the same on the next pass: never replayed as if it might clear.
        return DETERMINISTIC
    return DETERMINISTIC


def state_root_fault(error: OSError, state_dir: os.PathLike[str] | str) -> bool:
    """A missing or unreadable path is the host's: the state root is not a usable directory
    (missing, wrong owner, unmounted), or the error names the root, an ancestor of it or one of
    its top-level entries (a store directory or the database a deploy left out)."""
    root = os.path.abspath(os.fspath(state_dir))
    try:
        if not os.path.isdir(root) or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            return True
    except (OSError, ValueError):
        return True
    for name in (error.filename, error.filename2):
        if name is None or isinstance(name, int):
            continue
        try:
            path = os.path.abspath(os.fsdecode(name))
        except (TypeError, ValueError):
            continue
        if path == root or root.startswith(path.rstrip(os.sep) + os.sep):
            return True  # the root itself or an ancestor of it
        if os.path.dirname(path) == root:
            return True  # a top-level entry of the root
    return False


def is_missing_blob(error: BaseException) -> bool:
    """The one known file error that fails an idle stage outright: a recorded blob that is
    gone (``MissingBlob``, or ``ENOENT`` on a path named by a sha-256 digest)."""
    if isinstance(error, MissingBlob):
        return True
    if not isinstance(error, OSError) or error.errno != errno.ENOENT:
        return False
    name = error.filename
    if name is None or isinstance(name, int):
        return False
    try:
        base = os.path.basename(os.fsdecode(name))
    except (TypeError, ValueError):
        return False
    return _SHA256_NAME.search(base.lower()) is not None


def fault_kind(error: BaseException) -> str:
    """A short, payload-free name for an error: its errno name, else its class name."""
    if isinstance(error, HostFault):
        return error.kind
    if isinstance(error, OSError) and error.errno is not None:
        return errno.errorcode.get(error.errno, type(error).__name__)
    if isinstance(error, sqlite3.OperationalError):
        text = str(error).lower()
        if "disk i/o" in text:
            return "disk_io_error"
        if "full" in text:
            return "database_full"
        if "readonly" in text or "read-only" in text:
            return "database_readonly"
        if "unable to open" in text:
            return "database_unavailable"
    return type(error).__name__[:80]


def replay_count(error_class: str | None) -> int:
    """The persisted transient replay count of a pending intent (0 when none is recorded)."""
    if not error_class or not error_class.startswith(REPLAY_PREFIX):
        return 0
    try:
        return max(0, int(error_class[len(REPLAY_PREFIX) :].split(":", 1)[0]))
    except ValueError:
        return 0


def replay_marker(count: int, error_class: str) -> str:
    return f"{REPLAY_PREFIX}{count}:{error_class[:80]}"


def replay_backoff_seconds(count: int) -> float:
    """The wait before replaying an intent that has failed transiently ``count`` times."""
    if count < BACKOFF_AFTER_REPLAYS:
        return 0.0
    return min(2.0 ** (count - BACKOFF_AFTER_REPLAYS + 1), MAX_REPLAY_BACKOFF_SECONDS)


class _HostAlarm:
    """The process-wide host-fault alarm: raised once per spell, cleared by a clean tick."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, Any] | None = None

    def raise_alarm(self, kind: str, error_class: str, at: datetime) -> bool:
        """Record the alarm; True only when this starts a new spell (log it once)."""
        with self._lock:
            if self._active is not None:
                self._active["last_seen_at"] = at.isoformat()
                self._active["count"] += 1
                return False
            self._active = {
                "code": "runner_host_fault",
                "kind": kind[:40],
                "error_class": error_class[:80],
                "since": at.isoformat(),
                "last_seen_at": at.isoformat(),
                "count": 1,
            }
        log.error("runner host fault (%s, %s): work paused", kind[:40], error_class[:80])
        return True

    def clear(self) -> bool:
        with self._lock:
            if self._active is None:
                return False
            kind = self._active["kind"]
            self._active = None
        log.warning("runner host fault (%s) cleared: work resumed", kind)
        return True

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._active is None else dict(self._active)


HOST_ALARM = _HostAlarm()


__all__ = [
    "CONTENTION",
    "DETERMINISTIC",
    "ENVIRONMENT",
    "HOST_ALARM",
    "IO",
    "REPLAYABLE",
    "TRANSIENT",
    "HostFault",
    "MissingBlob",
    "classify",
    "fault_kind",
    "is_missing_blob",
    "state_root_fault",
]
