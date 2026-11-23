"""Advisory lock that proves the long-running services are (not) running against a state directory.

Every long-running process that keeps the control database open (``tessera serve`` and the Salad
campaign executor, ``tessera-salad-executor``) holds a SHARED lock on
``<state-dir>/.maintenance.lock`` for its whole life; an operator command that must have the
database to itself (``tessera db compact``) takes the EXCLUSIVE lock without blocking and refuses
if anything holds it. The lock is advisory and local: it protects against a forgotten running
service, not against a hostile process, and a process that opens the database without taking it
is not covered.
"""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from .store import StoreError

MAINTENANCE_LOCK_NAME = ".maintenance.lock"

_SERVICE_REFUSAL = (
    "The control service (or another maintenance run) is using this state directory; stop it first."
)
_MAINTENANCE_REFUSAL = "A maintenance run holds this state directory; retry when it finishes."

# Process-local bookkeeping. flock locks belong to open file descriptions, so two descriptors in
# one process conflict with each other exactly as two processes would; these records let the
# process recognise its own holdings.
_registry_guard = threading.Lock()
_exclusive_here: set[Path] = set()
_shared_for_process: dict[Path, int] = {}


class MaintenanceBusy(StoreError):
    """The state directory is in use (a service holds it) or a maintenance run is active."""


def _key(state_dir: Path | str) -> Path:
    return Path(state_dir).resolve()


def _open(state_dir: Path | str) -> int:
    path = Path(state_dir) / MAINTENANCE_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(path, flags, 0o600)


@contextmanager
def _locked(state_dir: Path | str, mode: int, refusal: str) -> Iterator[None]:
    descriptor = _open(state_dir)
    try:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceBusy(refusal) from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive(state_dir: Path | str) -> Iterator[None]:
    key = _key(state_dir)
    with _locked(state_dir, fcntl.LOCK_EX, _SERVICE_REFUSAL):
        with _registry_guard:
            _exclusive_here.add(key)
        try:
            yield
        finally:
            with _registry_guard:
                _exclusive_here.discard(key)


def exclusive(state_dir: Path | str) -> AbstractContextManager[None]:
    """Hold the database to yourself, or raise ``MaintenanceBusy`` at once."""
    return _exclusive(state_dir)


def shared(state_dir: Path | str) -> AbstractContextManager[None]:
    """What a running service holds; refused while a maintenance run holds the exclusive lock."""
    return _locked(state_dir, fcntl.LOCK_SH, _MAINTENANCE_REFUSAL)


def probe_exclusive(state_dir: Path | str) -> None:
    """Raise ``MaintenanceBusy`` if any process holds the lock; create and write nothing.

    A missing lock file means nobody holds it. If this process itself holds the exclusive lock
    for the same state directory, nobody else can hold any lock on it, so the probe passes.
    Other ``OSError`` (for example a symlink in place of the lock file) propagates.
    """
    with _registry_guard:
        if _key(state_dir) in _exclusive_here:
            return
    path = Path(state_dir) / MAINTENANCE_LOCK_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceBusy(_SERVICE_REFUSAL) from None
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def hold_shared_for_process(state_dir: Path | str) -> None:
    """Take the service's SHARED lock once and keep it until the process exits.

    Idempotent: one descriptor per resolved state directory, however often it is called. A no-op
    while this process holds the exclusive lock for the same directory, so a maintenance run never
    blocks itself. Raises ``MaintenanceBusy`` while another holder has the exclusive lock.
    """
    key = _key(state_dir)
    with _registry_guard:
        if key in _shared_for_process or key in _exclusive_here:
            return
        descriptor = _open(state_dir)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise MaintenanceBusy(_MAINTENANCE_REFUSAL) from None
        except BaseException:
            os.close(descriptor)
            raise
        _shared_for_process[key] = descriptor


def _release_process_locks() -> None:
    """Drop every lock taken by ``hold_shared_for_process`` (tests only)."""
    with _registry_guard:
        while _shared_for_process:
            _, descriptor = _shared_for_process.popitem()
            os.close(descriptor)
