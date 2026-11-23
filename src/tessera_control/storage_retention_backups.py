"""Backup bundle selection and deletion for the storage retention policy.

Backups are the `tessera backup` bundle (manifest.json) and the deploy-upgrade command's
pre-migration snapshot (snapshot-manifest.json, an older schema that `restore` refuses; its
config copies in a sibling `<name>-configs` directory are listed and removed with it).

Only bundles that `tessera restore` would accept (its own validation, including object
digests and the declared layout) count: an unrestorable bundle is skipped and reported, so it
never uses up the keep-last floor nor protects itself at the cost of a restorable one. Only
complete, verified `tessera backup` bundles that are direct, non-symlink
children of the backup root are ever listed as candidates. Deletion re-verifies the
bundle immediately before removing it and never follows symlinks.
"""

from __future__ import annotations

import errno
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
# The deploy-upgrade command's pre-migration snapshot (an older schema `tessera backup` refuses):
# snapshot.sqlite3 plus this manifest, restored by putting the database back, not by `restore`.
SNAPSHOT_MANIFEST_NAME = "snapshot-manifest.json"
SNAPSHOT_KIND = "pre-migration-snapshot"
COMPANION_SUFFIX = "-configs"  # config copies the deploy-upgrade command keeps beside a backup
_SNAPSHOT_STAMP = re.compile(r"^backup-(\d{8}T\d{6}Z)$")
_MAX_MANIFEST_BYTES = 1 << 20
# Sentinel "path" of the one skipped entry reported when the backup root itself is unreadable.
ROOT_UNREADABLE_PATH = "."
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


def _snapshot_manifest(directory: Path) -> dict[str, Any]:
    """The pre-migration snapshot format, normalized to the fields a bundle manifest has.

    It records no id or time, so the id is the digest's first 32 hex digits and the time is the
    stamp in the directory name (``backup-YYYYmmddTHHMMSSZ``), else the manifest file's mtime."""
    encoded = _read_regular(directory / SNAPSHOT_MANIFEST_NAME, _MAX_MANIFEST_BYTES)
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise BackupRetentionError("manifest too large")
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or raw.get("kind") != SNAPSHOT_KIND:
        raise BackupRetentionError("snapshot manifest is not a pre-migration snapshot")
    digest, size, schema = raw.get("sha256"), raw.get("bytes"), raw.get("schema_version")
    if (
        not isinstance(digest, str)
        or not _HEX64.fullmatch(digest)
        or type(size) is not int
        or size < 1
        or type(schema) is not int
        or schema < 1
    ):
        raise BackupRetentionError("snapshot manifest fields are invalid")
    match = _SNAPSHOT_STAMP.fullmatch(directory.name)
    if match:
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    else:
        created = datetime.fromtimestamp((directory / SNAPSHOT_MANIFEST_NAME).lstat().st_mtime, UTC)
    return {
        "format": "pre_migration_snapshot",
        "backup_id": digest[:32],
        "sha256": digest,
        "size": size,
        "schema_version": schema,
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }


def _manifest(directory: Path) -> dict[str, Any]:
    try:
        return _bundle_manifest(directory)
    except FileNotFoundError:
        if (directory / SNAPSHOT_MANIFEST_NAME).exists():
            return _snapshot_manifest(directory)
        raise


def _bundle_manifest(directory: Path) -> dict[str, Any]:
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

    if manifest.get("format") == "pre_migration_snapshot":
        _verify_snapshot(directory, manifest)
        return
    try:
        checked = recovery.verify_restorable(directory)
    except (recovery.StoreError, sqlite3.Error, OSError, ValueError, KeyError) as error:
        raise BackupRetentionError(f"restore would reject it: {_why(error)}") from error
    if checked["backup_id"] != manifest["backup_id"] or checked["sha256"] != manifest["sha256"]:
        raise BackupRetentionError("manifest changed during verification")


def _verify_snapshot(directory: Path, manifest: dict[str, Any]) -> None:
    """A pre-migration snapshot is restorable when it is exactly the two recorded files, the
    database matches its recorded size and digest, and SQLite reads it (read-only, in place)
    at the schema version the manifest names."""
    with os.scandir(directory) as entries:
        names = {entry.name for entry in entries}
    if names != {SNAPSHOT_NAME, SNAPSHOT_MANIFEST_NAME}:
        raise BackupRetentionError("snapshot directory holds unexpected files")
    size, digest = _hash_regular(directory / SNAPSHOT_NAME)
    if size != manifest["size"] or digest != manifest["sha256"]:
        raise BackupRetentionError("snapshot does not match its manifest")
    try:
        connection = sqlite3.connect(
            (directory / SNAPSHOT_NAME).absolute().as_uri() + "?mode=ro", uri=True, timeout=1
        )
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupRetentionError("snapshot database failed its integrity check")
            rows = connection.execute("SELECT schema_version FROM schema_meta").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupRetentionError(
            f"snapshot database unreadable: {type(error).__name__}"
        ) from error
    if rows != [(manifest["schema_version"],)]:
        raise BackupRetentionError("snapshot schema version does not match its manifest")


def _companion(root: Path, name: str) -> Path | None:
    """The config-copies directory beside a backup, when it is a plain direct child."""
    path = root / (name + COMPANION_SUFFIX)
    try:
        info = path.lstat()
    except OSError:
        return None
    return path if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) else None


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
        return [], [
            {
                "path": ROOT_UNREADABLE_PATH,
                "reason": f"backup root unreadable: {type(error).__name__}",
            }
        ]
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
        if name.endswith(COMPANION_SUFFIX) and (root / name[: -len(COMPANION_SUFFIX)]).is_dir():
            continue  # config copies kept beside a backup: listed and removed with it
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
            companion = _companion(root, name)
            if companion is not None:
                size += _tree_bytes(companion)
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
    """A reason safe to receipt: our own text, else the error type (and the errno name for an OS
    error, so a full disk reads ENOSPC rather than a bare OSError); never a path or message."""
    if isinstance(error, BackupRetentionError):
        return str(error)
    if isinstance(error, OSError) and error.errno in errno.errorcode:
        return f"{type(error).__name__} {errno.errorcode[error.errno]}"
    return type(error).__name__


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
    companion = _companion(root, name)
    shutil.rmtree(directory)  # does not follow symlinks
    if companion is not None:
        shutil.rmtree(companion)  # the config copies kept beside this backup go with it
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
