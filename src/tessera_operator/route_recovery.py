"""Isolated abandoned-route custody preparation; never retires or launches a route.

No CLI, registration, production paths, subprocesses or imports from live operators.
Observers are trusted integration dependencies, not caller attestation parameters.
Publication is intentionally unavailable until all launch paths share the lock.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


class RecoveryRefused(RuntimeError):
    """Allowlisted diagnostic only; never embed observer exceptions or input bytes."""


IDENTITY_KEYS = frozenset(
    {"namespace_uid", "deployment_uid", "pvc_uid", "pv_uid", "pod_uid", "image"}
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RecoveryRefused("private_directory_required")


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= 8192
        ):
            raise RecoveryRefused("private_regular_file_required")
        data = os.read(fd, 8193)
        if len(data) != info.st_size:
            raise RecoveryRefused("file_changed")
        return data
    finally:
        os.close(fd)


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_intent(raw: bytes, identity: dict) -> None:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RecoveryRefused("invalid_intent") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"pod_name", "pod_uid", "port"}
        or type(value["port"]) is not int
        or value["port"] != 18765
        or not isinstance(value["pod_name"], str)
        or not re.fullmatch(r"tessera-control-[a-z0-9-]{1,100}", value["pod_name"])
        or value["pod_uid"] != identity["pod_uid"]
    ):
        raise RecoveryRefused("exact_incomplete_intent_required")


def _observe(inspect_identity, inspect_processes, identity: dict) -> None:
    try:
        current = inspect_identity()
        observation = inspect_processes()
    except Exception:
        raise RecoveryRefused("inspection_unavailable") from None
    if current != identity:
        raise RecoveryRefused("admitted_identity_changed")
    # An empty list alone is not evidence of a complete/authorized enumeration.
    if (
        not isinstance(observation, dict)
        or observation.get("complete") is not True
        or observation != {"complete": True, "processes": [], "listeners": []}
    ):
        raise RecoveryRefused("process_or_listener_uncertain")


def prepare_custody(
    directory: Path,
    expected_intent_sha256: str,
    expected_identity: dict,
    *,
    inspect_identity,
    inspect_processes,
) -> dict:
    """Prepare immutable evidence only. Original route intent is never changed.

    The proposed future live adapter must supply canonical admitted identity and
    complete trusted OS observations. These observations are preflight, NOT a
    race-free certificate or permission to clear the intent. Repeated identical
    preparation verifies custody and re-observes prerequisites without rewriting.
    """
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected_intent_sha256)
        or set(expected_identity) != IDENTITY_KEYS
        or any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in expected_identity.values())
        or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", expected_identity["image"])
    ):
        raise RecoveryRefused("invalid_expected_identity")
    identity = dict(expected_identity)
    _private_directory(directory)
    lock = os.open(
        directory / "local-route-supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise RecoveryRefused("private_lock_required")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryRefused("conflicting_writer") from None
        if any(directory.glob(".route-recovery-preparing-*")):
            raise RecoveryRefused("uncertain_preparation_retained")
        route = directory / "local-persistent-route.json"
        raw = _read(route)
        if _digest(raw) != expected_intent_sha256:
            raise RecoveryRefused("stale_intent_digest")
        _validate_intent(raw, identity)
        _observe(inspect_identity, inspect_processes, identity)
        plan = {
            "version": 1,
            "state": "custody_only_integration_required",
            "intent_sha256": expected_intent_sha256,
            "identity": identity,
        }
        encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        destination = directory / ("route-recovery-" + expected_intent_sha256)
        if destination.exists() or destination.is_symlink():
            _private_directory(destination)
            if set(p.name for p in destination.iterdir()) != {"original.json", "plan.json"}:
                raise RecoveryRefused("custody_changed")
            if (
                _read(destination / "original.json") != raw
                or _read(destination / "plan.json") != encoded
            ):
                raise RecoveryRefused("custody_changed")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".route-recovery-preparing-", dir=directory))
            # On failure keep all partial evidence. Never auto-clean or replay it.
            _write(staging / "original.json", raw)
            _write(staging / "plan.json", encoded)
            _sync(staging)
            _observe(inspect_identity, inspect_processes, identity)
            if _read(route) != raw:
                raise RecoveryRefused("intent_changed_during_preparation")
            os.rename(staging, destination)
            _sync(directory)
        if _read(route) != raw:
            raise RecoveryRefused("intent_changed_during_preparation")
        return plan
    finally:
        os.close(lock)


def publish_reconciliation(*args, **kwargs):
    """Deliberately unavailable: a caller boolean cannot establish writer fencing."""
    raise RecoveryRefused("shared_launch_lock_and_transition_integration_required")
