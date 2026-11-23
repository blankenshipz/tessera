"""Offline compaction of the control database (``tessera db compact``).

The control database only grows: SQLite keeps deleted pages on its freelist. Compaction
rewrites it with ``VACUUM INTO`` next to the live file and swaps the copy in atomically,
under conditions that make the swap safe and reversible:

* the service must be stopped: the maintenance lock is taken exclusively (``tessera serve``
  holds it shared) and a separate connection proves nothing else has the database open;
* free space is checked before anything is written (the copy lives in the state directory,
  so the final rename is atomic; the backup needs room under the backup root);
* the copy must pass integrity and foreign-key checks, the supported-schema validation used
  by restore, and match the source table by table (row counts, ``schema_meta``,
  ``control_recovery``, the event watermark);
* the previous database is first written as a standard ``tessera backup`` bundle under the
  storage-retention policy's ``backup_root`` and must pass restore's own validation;
* a write lock on the source (``BEGIN IMMEDIATE``) is held from before the source is counted
  until the rename is durable, so no SQLite writer can commit between the copy and the swap;
  immediately before the rename the file's inode, link count, size, modification time, change
  counter and schema cookie must still be what they were when the lock was taken;
* after the rename the new file is checked again; if that fails, the previous file (kept as a
  hard link until then) is renamed back.

Every completed run leaves a private JSON receipt under ``<state-dir>/db-compact/``. It is a
file, not a control event: the database it would be recorded in is the one being replaced. If
writing it fails after the swap, the compaction still stands and the result says so
(``receipt_written: false``). ``--dry-run`` creates, locks for writing and changes nothing: it
probes the maintenance lock instead of taking it and reads the database read-only.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import maintenance_lock, recovery
from .migrations import LATEST_SCHEMA_VERSION
from .store import DATABASE_NAME, StoreError, _lstat_is_symlink, require_sqlite_runtime

DEFAULT_MIN_FREE_BYTES = 8 << 30  # free space the state filesystem must keep, at least
BACKUP_FREE_FLOOR_BYTES = 1 << 30  # free space left on the backup filesystem after the copy
RECEIPT_DIR = "db-compact"
BACKUP_PREFIX = "pre-compact-"
TEMP_PREFIX = f".{DATABASE_NAME}.compact-"
PREVIOUS_PREFIX = f".{DATABASE_NAME}.precompact-"
SIDECARS = ("-wal", "-shm", "-journal")


def _with_headroom(size: int) -> int:
    return size + size // 10


def _open(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """A connection that never waits: anything else holding the file is an error.

    ``readonly`` (the dry run) must not create, change or remove any file: the connection is
    ``mode=ro`` (a hot journal is an error instead of being rolled back), and a WAL database
    without sidecars, whose WAL is therefore empty, is read ``immutable`` so no ``-wal`` or
    ``-shm`` file is created.
    """
    require_sqlite_runtime()
    if readonly:
        query = "?mode=ro"
        if _header_journal(path) == "wal" and not any(
            os.path.lexists(f"{path}{suffix}") for suffix in SIDECARS
        ):
            query += "&immutable=1"
        connection = sqlite3.connect(
            path.absolute().as_uri() + query, uri=True, timeout=0, isolation_level=None
        )
    else:
        connection = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=0")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _read_header(path: Path) -> tuple[os.stat_result, bytes]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return os.fstat(descriptor), os.pread(descriptor, 100, 0)
    finally:
        os.close(descriptor)


def _header_journal(path: Path) -> str:
    """The journal mode the file header records (read and write versions 2 mean WAL)."""
    _info, header = _read_header(path)
    return "wal" if header[18:20] == b"\x02\x02" else "rollback"


def _file_identity(path: Path) -> dict[str, Any]:
    """What an in-place write would change even at the same size: inode, links, size,
    modification time, and the header's file change counter and schema cookie."""
    info, header = _read_header(path)
    return {
        "inode": info.st_ino,
        "links": info.st_nlink,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "change_counter": header[24:28],
        "schema_cookie": header[40:44],
    }


def _require_unchanged(path: Path, pinned: dict[str, Any], *, extra_links: int) -> None:
    """Abort the swap if the file changed since the write guard was taken (a writer that
    ignored the lock, or a raw write), or if a journal appeared next to it."""
    actual = _file_identity(path)
    expected = dict(pinned, links=pinned["links"] + extra_links)
    changed = [key for key in expected if expected[key] != actual[key]]
    changed += [f"{suffix} file" for suffix in SIDECARS if os.path.lexists(f"{path}{suffix}")]
    if changed:
        raise StoreError(
            "The control database changed on disk during compaction ("
            + ", ".join(changed)
            + "); nothing was swapped and the pre-compaction backup was removed."
        )


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fingerprint(connection: sqlite3.Connection) -> dict[str, Any]:
    """What must be identical before and after: every table's row count and the
    recovery-relevant singleton rows."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    tables = {
        name: connection.execute(f"SELECT count(*) FROM {_quoted(name)}").fetchone()[0]
        for name in names
    }
    return {
        "tables": tables,
        "schema_meta": [tuple(row) for row in connection.execute("SELECT * FROM schema_meta")],
        "control_recovery": [
            tuple(row) for row in connection.execute("SELECT * FROM control_recovery")
        ],
        "max_cursor": connection.execute("SELECT MAX(cursor) FROM events").fetchone()[0],
    }


def _differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    found = []
    for name in sorted(set(expected["tables"]) | set(actual["tables"])):
        if expected["tables"].get(name) != actual["tables"].get(name):
            found.append(f"table {name}")
    for key in ("schema_meta", "control_recovery", "max_cursor"):
        if expected[key] != actual[key]:
            found.append(key)
    return found


def _check_state(state_dir: Path, *, readonly: bool = False) -> tuple[Path, os.stat_result]:
    """The refusals of ``tessera migrate`` plus a held restore and an unreadable database."""
    if _lstat_is_symlink(state_dir) or not state_dir.is_dir():
        raise StoreError("State directory is not a safe directory.")
    if (state_dir / ".authority-moved").exists() or _lstat_is_symlink(
        state_dir / ".authority-moved"
    ):
        raise StoreError("Control-plane authority was moved from this state directory.")
    database = state_dir / DATABASE_NAME
    try:
        info = database.lstat()
    except OSError as error:
        raise StoreError("Control-plane database is not a regular file.") from error
    if not stat.S_ISREG(info.st_mode):
        raise StoreError("Control-plane database is not a regular file.")
    if info.st_nlink != 1:
        raise StoreError("Control-plane database must have exactly one link.")
    try:
        connection = _open(database, readonly=readonly)
        try:
            connection.execute("PRAGMA query_only=ON")
            schema = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton=1"
            ).fetchone()
            recovery_state = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.OperationalError as error:
        if "locked" in str(error) or "busy" in str(error):
            raise StoreError(
                "The control database is in use by another process; stop all services first."
            ) from error
        raise StoreError("Control-plane database is unreadable.") from error
    except sqlite3.Error as error:
        raise StoreError("Control-plane database is unreadable.") from error
    if schema is None or schema[0] > LATEST_SCHEMA_VERSION:
        raise StoreError("Control-plane database schema version is unsupported.")
    if schema[0] < LATEST_SCHEMA_VERSION:
        raise StoreError("Control-plane database requires an explicit `tessera migrate` first.")
    if recovery_state is None:
        raise StoreError("Control-plane database has no recovery state.")
    if recovery_state[0] == "held_restore":
        raise StoreError("A held restore is an audit copy and is never compacted.")
    return database, info


def _backup_root(state_dir: Path, backup_root: Path | None) -> tuple[Path, bool]:
    from .storage_retention import RetentionError, load_policy

    try:
        policy = load_policy(state_dir)
    except (RetentionError, OSError) as error:
        raise StoreError(f"Storage retention policy is unreadable: {error}") from error
    policy_root = Path(policy.backup_root) if policy is not None and policy.backup_root else None
    root = backup_root if backup_root is not None else policy_root
    if root is None:
        raise StoreError(
            "No backup root: the storage-retention policy sets no backup_root; "
            "pass --backup-root DIR for the pre-compaction backup."
        )
    if _lstat_is_symlink(root) or not root.is_dir():
        raise StoreError("Backup root must be an existing directory that is not a symlink.")
    registered = policy_root is not None and root.resolve() == policy_root.resolve()
    return root, registered


def _probe(database: Path, *, checkpoint: bool) -> str:
    """Prove no other connection holds the database; in WAL mode also fold the WAL back."""
    busy = StoreError(
        "The control database is open in another process; stop all services before compacting."
    )
    connection = _open(database)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError as error:
            raise busy from error
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        if journal == "wal" and checkpoint:
            # A TRUNCATE checkpoint also needs every reader gone (BEGIN EXCLUSIVE in WAL
            # mode only keeps writers out); it is busy if any other connection exists.
            try:
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.OperationalError as error:
                raise busy from error
            if result[0] != 0:
                raise busy
        return journal
    finally:
        connection.close()


def _probe_readonly(database: Path) -> str:
    """The dry run's probe: it may not lock the file for writing, so it only proves that no
    writer is committing right now (the real run still probes for every other connection)."""
    connection = _open(database, readonly=True)
    try:
        try:
            connection.execute("BEGIN")
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError as error:
            raise StoreError(
                "The control database is being written by another process; stop all services "
                "before compacting."
            ) from error
    finally:
        connection.close()
    return "wal" if _header_journal(database) == "wal" else "delete"


def _measure(database: Path, *, readonly: bool = False) -> dict[str, Any]:
    connection = _open(database, readonly=readonly)
    try:
        connection.execute("PRAGMA query_only=ON")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        fingerprint = _fingerprint(connection)
    finally:
        connection.close()
    sidecar_bytes = 0
    for suffix in SIDECARS:
        try:
            sidecar_bytes += os.lstat(f"{database}{suffix}").st_size
        except FileNotFoundError:
            pass
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist": freelist,
        "fingerprint": fingerprint,
        "db_size": database.stat().st_size,
        "sidecar_bytes": sidecar_bytes,
    }


def _space(state_dir: Path, root: Path, measured: dict[str, Any], min_free: int) -> dict[str, int]:
    """Refuse, before writing anything, unless both filesystems have room."""
    db_size = measured["db_size"]
    if db_size > recovery.MAX_CONFIGURABLE_BACKUP_BYTES:
        raise StoreError(
            f"Control database ({db_size} bytes) exceeds the largest backup "
            f"({recovery.MAX_CONFIGURABLE_BACKUP_BYTES} bytes); it cannot be compacted safely."
        )
    copy = _with_headroom(db_size + measured["sidecar_bytes"])
    state_required = max(min_free, copy)
    backup_required = _with_headroom(db_size) + BACKUP_FREE_FLOOR_BYTES
    if os.stat(state_dir).st_dev == os.stat(root).st_dev:
        # One filesystem holds both the compacted copy and the backup.
        state_required += _with_headroom(db_size)
    state_free = shutil.disk_usage(state_dir).free
    backup_free = shutil.disk_usage(root).free
    if state_free < state_required:
        raise StoreError(
            f"Not enough free disk on the state directory filesystem: {state_required} bytes "
            f"required, {state_free} free. Nothing was written."
        )
    if backup_free < backup_required:
        raise StoreError(
            f"Not enough free disk on the backup root filesystem: {backup_required} bytes "
            f"required, {backup_free} free. Nothing was written."
        )
    return {
        "free_disk_bytes": state_free,
        "required_free_bytes": state_required,
        "backup_free_disk_bytes": backup_free,
        "backup_required_free_bytes": backup_required,
    }


def _verify_copy(path: Path, expected: dict[str, Any]) -> None:
    """Restore's own snapshot validation plus exact parity with the source."""
    schema, watermark = recovery._validate_snapshot(path)
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StoreError("Compacted copy failed its integrity check.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoreError("Compacted copy failed its foreign key check.")
        actual = _fingerprint(connection)
    finally:
        connection.close()
    differences = _differences(expected, actual)
    if differences:
        raise StoreError(
            "Compacted copy does not match the source (" + ", ".join(differences[:10]) + ")."
        )
    if schema != LATEST_SCHEMA_VERSION or watermark != (expected["max_cursor"] or 0):
        raise StoreError("Compacted copy schema or event watermark does not match the source.")


def _post_verify(database: Path, expected: dict[str, Any]) -> int:
    """Check the swapped-in file; return its page count."""
    connection = _open(database)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StoreError("Compacted database failed its integrity check after the swap.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoreError("Compacted database failed its foreign key check after the swap.")
        differences = _differences(expected, _fingerprint(connection))
        if differences:
            raise StoreError("Compacted database does not match the source after the swap.")
        return connection.execute("PRAGMA page_count").fetchone()[0]
    finally:
        connection.close()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _clear_sidecars(database: Path) -> None:
    """Old sidecars must not be applied to the new file: allow only empty or absent ones."""
    for suffix in ("-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size != 0:
            raise StoreError(
                f"Control database has a non-empty {suffix} file; nothing was swapped."
            )
        path.unlink()
    shm = Path(f"{database}-shm")
    if shm.exists() or _lstat_is_symlink(shm):
        # Stale once the WAL is empty and no connection exists (proven by the probe).
        shm.unlink()


def _release(guard: sqlite3.Connection) -> None:
    try:
        if guard.in_transaction:
            guard.execute("ROLLBACK")
    finally:
        guard.close()


def _remove_quietly(path: Path) -> None:
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in SIDECARS)):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def compact_state(
    state_dir: Path,
    *,
    backup_root: Path | None = None,
    min_free_bytes: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Compact the stopped control database; return the receipt (or the dry-run plan)."""
    started = clock()
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    state_dir = Path(state_dir)
    min_free = DEFAULT_MIN_FREE_BYTES if min_free_bytes is None else min_free_bytes
    if type(min_free) is not int or min_free < 0:
        raise StoreError("--min-free-bytes must be a nonnegative integer.")
    _check_state(state_dir, readonly=dry_run)
    root, registered = _backup_root(state_dir, backup_root)
    if dry_run:
        # Write-free: probe the maintenance lock instead of taking it (taking it creates the
        # lock file), and read the database without creating, locking for write or changing
        # anything.
        maintenance_lock.probe_exclusive(state_dir)
        database, _info = _check_state(state_dir, readonly=True)
        journal = _probe_readonly(database)
        measured = _measure(database, readonly=True)
        space = _space(state_dir, root, measured, min_free)
        estimated = (measured["page_count"] - measured["freelist"]) * measured["page_size"]
        return {
            "status": "dry_run",
            "journal_mode": journal,
            "bytes_before": measured["db_size"],
            "sidecar_bytes_before": measured["sidecar_bytes"],
            "page_size": measured["page_size"],
            "page_count_before": measured["page_count"],
            "freelist_pages_before": measured["freelist"],
            "estimated_bytes_after": estimated,
            "estimated_bytes_reclaimed": max(0, measured["db_size"] - estimated),
            "tables": measured["fingerprint"]["tables"],
            "backup_registered_with_retention": registered,
            **space,
        }
    with maintenance_lock.exclusive(state_dir):
        database, _info = _check_state(state_dir)
        journal = _probe(database, checkpoint=True)
        measured = _measure(database)
        space = _space(state_dir, root, measured, min_free)
        receipt = _compact(state_dir, database, root, registered, journal, measured, moment)
    receipt.update(space)
    receipt["started_at"] = moment.isoformat()
    finished = datetime.now(UTC)
    receipt["finished_at"] = finished.isoformat()
    receipt["duration_seconds"] = round(clock() - started, 3)
    receipt["receipt_written"] = True
    _write_compaction_receipt(state_dir, moment, receipt)
    return receipt


def _write_compaction_receipt(state_dir: Path, moment: datetime, receipt: dict[str, Any]) -> None:
    """The swap has already happened: a failed receipt write is reported, never raised."""
    from .storage_retention import _write_receipt

    name = f"{moment.strftime('%Y%m%dT%H%M%S%f')}Z-{receipt['run_id']}.json"
    try:
        _write_receipt(state_dir / RECEIPT_DIR, name, receipt)
    except OSError as error:
        receipt["receipt_written"] = False
        # The class name only: an OSError message carries local paths.
        receipt["receipt_error"] = type(error).__name__
        try:
            (state_dir / RECEIPT_DIR / f".{name}.tmp").unlink()
        except OSError:
            pass


def _compact(
    state_dir: Path,
    database: Path,
    root: Path,
    registered: bool,
    journal: str,
    measured: dict[str, Any],
    moment: datetime,
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    mode = stat.S_IMODE(database.stat().st_mode)
    temp = state_dir / f"{TEMP_PREFIX}{secrets.token_hex(8)}"
    bundle = root / f"{BACKUP_PREFIX}{moment.strftime('%Y%m%dT%H%M%SZ')}-{run_id[:12]}"
    previous: Path | None = None
    swapped = False
    bundle_created = False
    token = recovery._BYTE_LIMIT.set(recovery.MAX_CONFIGURABLE_BACKUP_BYTES)
    guard: sqlite3.Connection | None = None
    try:
        # VACUUM INTO accepts an empty target; creating it here makes it new and private.
        os.close(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        guard = _open(database)
        # A write lock keeps every writer out from before the source is counted until after
        # the swap is durable: through the copy, the backup and its verification, the
        # sidecar clearing and the rename. Readers (the copy and the backup) are allowed.
        guard.execute("BEGIN IMMEDIATE")
        pinned = _file_identity(database)
        expected = _fingerprint(guard)
        if expected != measured["fingerprint"]:
            raise StoreError("The control database changed during compaction checks.")
        reader = _open(database)
        try:
            reader.execute("VACUUM INTO ?", (str(temp),))
        finally:
            reader.close()
        os.chmod(temp, 0o600)
        _verify_copy(temp, expected)
        if os.path.lexists(bundle):
            raise StoreError("Pre-compaction backup directory already exists.")
        bundle_created = True
        manifest = recovery._backup_state(state_dir, bundle)
        checked = recovery.verify_restorable(bundle)
        if (
            checked["backup_id"] != manifest["backup_id"]
            or checked["sha256"] != manifest["sha256"]
            or manifest["event_watermark"] != (expected["max_cursor"] or 0)
        ):
            raise StoreError("Pre-compaction backup does not match the source database.")
        if journal == "wal":
            connection = _open(temp)
            try:
                if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                    raise StoreError("Could not restore WAL journal mode on the compacted copy.")
            finally:
                connection.close()
        for suffix in SIDECARS:
            if os.path.lexists(f"{temp}{suffix}"):
                raise StoreError("Compacted copy left a journal file behind.")
        os.chmod(temp, mode)
        _fsync_file(temp)
        recovery._fsync_directory(state_dir)
        _clear_sidecars(database)
        previous = state_dir / f"{PREVIOUS_PREFIX}{secrets.token_hex(8)}"
        os.link(database, previous)
        # The guard only binds writers that honour SQLite locking; a write that bypassed it
        # (or a same-size in-place write) still changes the inode metadata or the header.
        _require_unchanged(database, pinned, extra_links=1)
        os.replace(temp, database)
        swapped = True
        recovery._fsync_directory(state_dir)
        # Released only now. In WAL mode the guard is the last connection to the old file:
        # closing it may remove "-wal"/"-shm" by name, which is harmless only because
        # nothing has opened the new file yet. It wrote nothing, so a failing rollback cannot
        # undo the swap; the connection is closed either way.
        try:
            _release(guard)
        except sqlite3.Error:
            pass
        guard = None
        try:
            page_count_after = _post_verify(database, expected)
        except Exception as error:
            os.replace(previous, database)
            previous = None
            recovery._fsync_directory(state_dir)
            raise StoreError(
                f"{error} The previous database was put back; the pre-compaction backup "
                f"{bundle.name} is kept."
            ) from error
        previous.unlink()
        previous = None
        recovery._fsync_directory(state_dir)
    except BaseException:
        if guard is not None:
            try:
                _release(guard)
            except sqlite3.Error:
                pass  # closed regardless; the original error is the one to report
            guard = None
        if not swapped:
            _remove_quietly(temp)
            if bundle_created and bundle.is_dir() and not bundle.is_symlink():
                shutil.rmtree(bundle, ignore_errors=True)
        if previous is not None and not swapped:
            # Before the swap the link is only a second name of the live file. After the
            # swap a remaining link means putting it back failed: it is the only copy of
            # the previous file outside the backup, so it stays.
            try:
                previous.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        recovery._BYTE_LIMIT.reset(token)
    bytes_after = database.stat().st_size
    return {
        "run_id": run_id,
        "status": "compacted",
        "journal_mode": journal,
        "bytes_before": measured["db_size"],
        "bytes_after": bytes_after,
        "bytes_reclaimed": measured["db_size"] - bytes_after,
        "page_size": measured["page_size"],
        "page_count_before": measured["page_count"],
        "page_count_after": page_count_after,
        "freelist_pages_before": measured["freelist"],
        "tables": expected["tables"],
        "integrity": "ok",
        "backup": {
            "path": bundle.name,
            "backup_id": manifest["backup_id"],
            "sha256": manifest["sha256"],
            "size": manifest["size"],
            "registered_with_retention": registered,
            **(
                {}
                if registered
                else {
                    "note": "the backup root is not the retention policy's backup_root; "
                    "storage retention will not list or prune this backup"
                }
            ),
        },
    }
