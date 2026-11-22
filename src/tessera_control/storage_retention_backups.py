"""Backup bundle selection and deletion for the storage retention policy.

Only bundles that `tessera restore` would accept (its own validation, including object
digests and the declared layout) count: an unrestorable bundle is skipped and reported, so it
never uses up the keep-last floor nor protects itself at the cost of a restorable one. Only
complete, verified `tessera backup` bundles that are direct, non-symlink
children of the backup root are ever listed as candidates. Deletion re-verifies the
bundle immediately before removing it and never follows symlinks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tessera_control.storage_retention_models import MIN_KEEP, Candidate, RetentionPolicy

MANIFEST_NAME = "manifest.json"
SNAPSHOT_NAME = "snapshot.sqlite3"
_MAX_MANIFEST_BYTES = 1 << 20
_HEX64 = re.compile(r"[0-9a-f]{64}")
_BACKUP_ID = re.compile(r"[0-9a-f]{32}")


class BackupRetentionError(Exception):
    """A backup bundle failed verification; nothing was deleted."""


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _read_regular(path: Path, limit: int | None = None) -> bytes:
    """Read a regular file without following a symlink at the final component."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise BackupRetentionError("not a regular file")
        return handle.read() if limit is None else handle.read(limit + 1)


def _hash_regular(path: Path) -> tuple[int, str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise BackupRetentionError("not a regular file")
        while chunk := handle.read(1 << 20):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _manifest(directory: Path) -> dict[str, Any]:
    encoded = _read_regular(directory / MANIFEST_NAME, _MAX_MANIFEST_BYTES)
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise BackupRetentionError("manifest too large")
    manifest = json.loads(encoded)
    if not isinstance(manifest, dict):
        raise BackupRetentionError("manifest is not an object")
    backup_id, digest, size = (
        manifest.get("backup_id"),
        manifest.get("sha256"),
        manifest.get("size"),
    )
    created = manifest.get("created_at")
    if (
        not isinstance(backup_id, str)
        or not _BACKUP_ID.fullmatch(backup_id)
        or not isinstance(digest, str)
        or not _HEX64.fullmatch(digest)
        or type(size) is not int
        or size < 1
        or not isinstance(created, str)
        or not 1 <= len(created) <= 64
    ):
        raise BackupRetentionError("manifest fields are invalid")
    _parse_time(created)
    return manifest


def _verify_restorable(directory: Path, manifest: dict[str, Any]) -> None:
    """Pass only what `tessera restore` would accept (its own validation, object digests too).

    Never cached: the content is hashed on every call, so a bundle that rotted (even keeping
    its size and modification time) can never be counted as restorable, and so can never
    cause the last restorable backup to be deleted."""
    from tessera_control import recovery

    try:
        checked = recovery.verify_restorable(directory)
    except (recovery.StoreError, sqlite3.Error, OSError, ValueError, KeyError) as error:
        raise BackupRetentionError(f"restore would reject it: {_why(error)}") from error
    if checked["backup_id"] != manifest["backup_id"] or checked["sha256"] != manifest["sha256"]:
        raise BackupRetentionError("manifest changed during verification")


def _tree_bytes(directory: Path) -> int:
    total = 0
    stack = [directory]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


def list_backups(
    root: Path, *, verify: bool = True
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """Return bundles (decision "keep") and skipped children with a reason.

    With ``verify`` (the default) every bundle is checked with restore's own validation,
    reading all its bytes; without it only the manifest is read (cheap, for ``plan_backups``,
    which verifies just the bundles whose verdict matters)."""
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError as error:
        return [], [{"path": ".", "reason": f"backup root unreadable: {type(error).__name__}"}]
    for name in names:
        child = root / name
        try:
            info = child.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            skipped.append({"path": name, "reason": "symlink"})
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        try:
            manifest = _manifest(child)
        except FileNotFoundError:
            skipped.append({"path": name, "reason": "no manifest"})
            continue
        except (OSError, ValueError, BackupRetentionError) as error:
            skipped.append({"path": name, "reason": f"manifest unreadable: {_why(error)}"})
            continue
        try:
            if verify:
                _verify_restorable(child, manifest)
            size = _tree_bytes(child)
        except (OSError, BackupRetentionError) as error:
            skipped.append({"path": name, "reason": f"incomplete or unverified: {_why(error)}"})
            continue
        candidates.append(
            Candidate(
                kind="backup",
                ref=manifest["backup_id"],
                path=name,
                size_bytes=size,
                sha256=manifest["sha256"],
                created_at=manifest["created_at"],
                decision="keep",
            )
        )
    return candidates, skipped


def _why(error: Exception) -> str:
    return str(error) if isinstance(error, BackupRetentionError) else type(error).__name__


def select_backups(
    entries: list[Candidate],
    policy: RetentionPolicy,
    *,
    now: datetime,
    free_bytes: Callable[[], int] | int,
) -> list[Candidate]:
    """Return the entries, newest first, with decision and reasons set."""
    ordered = sorted(
        (c for c in entries if c.kind == "backup"),
        key=lambda c: (_parse_time(c.created_at), c.ref),
        reverse=True,
    )
    floor = max(policy.backup_keep_last or 0, MIN_KEEP)
    decisions: list[tuple[str, list[str]]] = []
    for index, entry in enumerate(ordered):
        if index < floor:
            decisions.append(("keep", ["keep_last"] if policy.backup_keep_last else ["min_keep"]))
        elif policy.backup_max_age_days is not None and _parse_time(entry.created_at) < (
            now - timedelta(days=policy.backup_max_age_days)
        ):
            decisions.append(("delete", ["max_age"]))
        else:
            decisions.append(("keep", []))
    threshold = policy.disk_free_min_bytes
    if threshold is not None:
        free = free_bytes() if callable(free_bytes) else free_bytes
        if free < threshold:
            freed = sum(
                ordered[i].size_bytes for i, (d, _) in enumerate(decisions) if d == "delete"
            )
            for index in range(len(ordered) - 1, floor - 1, -1):
                if free + freed >= threshold:
                    break
                if decisions[index][0] == "keep":
                    decisions[index] = ("delete", ["disk_pressure"])
                    freed += ordered[index].size_bytes
    return [
        replace(entry, decision=decision, reasons=reasons)  # type: ignore[arg-type]
        for entry, (decision, reasons) in zip(ordered, decisions, strict=True)
    ]


def delete_backup(root: Path, candidate: Candidate) -> dict[str, Any]:
    """Re-verify the bundle, then remove it. Raises and deletes nothing on any mismatch."""
    if candidate.kind != "backup":
        raise BackupRetentionError("not a backup candidate")
    name = candidate.path
    if not name or name in {".", ".."} or "/" in name or "\\" in name or os.sep in name:
        raise BackupRetentionError("backup path must be a direct child name")
    directory = root / name
    try:
        info = directory.lstat()
    except OSError as error:
        raise BackupRetentionError("backup directory is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BackupRetentionError("backup is not a plain directory")
    if directory.resolve().parent != root.resolve():
        raise BackupRetentionError("backup is not a direct child of the backup root")
    try:
        manifest = _manifest(directory)
        if manifest["backup_id"] != candidate.ref or manifest["sha256"] != candidate.sha256:
            raise BackupRetentionError("manifest changed since planning")
        _verify_restorable(directory, manifest)
    except (OSError, ValueError) as error:
        raise BackupRetentionError(f"backup no longer verifies: {_why(error)}") from error
    shutil.rmtree(directory)  # does not follow symlinks
    return {
        "ref": candidate.ref,
        "path": name,
        "size_bytes": candidate.size_bytes,
        "sha256": candidate.sha256,
    }


def verify_candidate(root: Path, candidate: Candidate) -> None:
    """Re-hash one listed bundle with restore's validation; raises if restore would reject it
    or if it is no longer the bundle that was listed."""
    directory = root / candidate.path
    if candidate.path in {"", ".", ".."} or os.sep in candidate.path or directory.is_symlink():
        raise BackupRetentionError("backup is not a plain direct child")
    try:
        manifest = _manifest(directory)
        if manifest["backup_id"] != candidate.ref or manifest["sha256"] != candidate.sha256:
            raise BackupRetentionError("manifest changed since planning")
        _verify_restorable(directory, manifest)
    except (OSError, ValueError) as error:
        raise BackupRetentionError(f"backup no longer verifies: {_why(error)}") from error


def plan_backups(
    root: Path,
    policy: RetentionPolicy,
    *,
    now: datetime,
    free_bytes: Callable[[], int] | int,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """Plan with bounded hashing: only bundles whose verdict matters are read in full.

    Those are the newest restorable bundles that fill the keep floor (they are what must
    survive) and every bundle selected for deletion (an unrestorable one is reported, never
    deleted). A bundle kept only because it is young or inside the keep window is not hashed.
    """
    cheap, skipped = list_backups(root, verify=False)
    ordered = sorted(cheap, key=lambda c: (_parse_time(c.created_at), c.ref), reverse=True)
    floor = max(policy.backup_keep_last or 0, MIN_KEEP)
    verified: set[str] = set()  # by directory name: copies of one bundle share a backup id

    def check(entry: Candidate) -> bool:
        try:
            verify_candidate(root, entry)
        except (OSError, BackupRetentionError) as error:
            skipped.append(
                {"path": entry.path, "reason": f"restore would reject it: {_why(error)}"}
            )
            return False
        verified.add(entry.path)
        return True

    restorable: list[Candidate] = []
    for entry in ordered:  # newest first until the floor is filled with restorable bundles
        if len(restorable) >= floor:
            break
        if check(entry):
            restorable.append(entry)
    rest = ordered[ordered.index(restorable[-1]) + 1 :] if len(restorable) >= floor else []
    pool = restorable + rest
    while True:
        result = select_backups(pool, policy, now=now, free_bytes=free_bytes)
        unverified = [c for c in result if c.decision == "delete" and c.path not in verified]
        if not unverified:
            # Bundles kept without being read in full say so; the timer's sweep checks them.
            return [
                replace(c, reasons=["unverified"])
                if c.decision == "keep" and not c.reasons and c.path not in verified
                else c
                for c in result
            ], skipped
        for entry in unverified:
            if not check(entry):
                pool = [c for c in pool if c.path != entry.path]


def verify_all(root: Path) -> dict[str, Any]:
    """Fully verify every bundle (restore's validation): how many restore, and which do not.

    An unreadable backup root is reported as ``error`` (the sweep then fails), never as
    "nothing is wrong"."""
    entries, skipped = list_backups(root, verify=True)
    result: dict[str, Any] = {
        "restorable": len(entries),
        "unrestorable": [
            s
            for s in skipped
            if s["path"] != "."
            and any(w in s["reason"] for w in ("restore would reject", "incomplete", "manifest"))
        ],
        "skipped": len(skipped),
    }
    unreadable = [s for s in skipped if s["path"] == "."]
    if unreadable:
        result["error"] = unreadable[0]["reason"]
    return result


def reverify_kept(root: Path, plan: list[Candidate]) -> None:
    """Before any deletion: re-hash the bundles the plan relies on to survive (the keep floor).

    Closes the gap between planning and deleting; raises if ANY of them no longer verifies
    (stricter than "none still restores", and safe: nothing is deleted)."""
    floor = [c for c in plan if c.decision == "keep" and c.reasons in (["keep_last"], ["min_keep"])]
    if not floor:
        raise BackupRetentionError("no restorable backup is being kept")
    for entry in floor:
        verify_candidate(root, entry)
