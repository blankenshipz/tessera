"""Online backup and held history-recovery primitives for consistent local control-plane backups."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from .migrations import LATEST_SCHEMA_VERSION
from .store import DATABASE_NAME, OWNER_HEADER_NAME, StoreError, _connect, _token_hash, utc_now

BACKUP_MANIFEST = "manifest.json"
BACKUP_DATABASE = "snapshot.sqlite3"
RESTORE_MARKER = ".restore-complete"
MAX_BACKUP_BYTES = 1 << 30
MAX_BACKUP_SECONDS = 30
MAX_MANIFEST_BYTES = 16 * 1024
BACKUP_FORMAT_VERSION = 1


def create_recovery_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS control_recovery (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        state TEXT NOT NULL CHECK (state IN ('active', 'held_restore')),
        incarnation TEXT NOT NULL,
        restore_id TEXT,
        source_backup_digest TEXT,
        event_watermark INTEGER,
        restored_at TEXT
        )"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO control_recovery
        (singleton, state, incarnation, restore_id, source_backup_digest,
         event_watermark, restored_at)
        VALUES (1, 'active', ?, NULL, NULL, NULL, NULL)""",
        (uuid.uuid4().hex,),
    )


@contextmanager
def _open_regular(path: Path, limit: int):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise StoreError("Recovery input must be a bounded regular file with one link.")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            yield file
    finally:
        os.close(descriptor)


def _sha256(path: Path, start: float) -> str:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path, MAX_BACKUP_BYTES) as file:
        while chunk := file.read(1024 * 1024):
            _deadline(start)
            size += len(chunk)
            if size > MAX_BACKUP_BYTES:
                raise StoreError("Backup exceeds its size limit.")
            digest.update(chunk)
    return digest.hexdigest()


def _deadline(start: float) -> None:
    if time.monotonic() - start > MAX_BACKUP_SECONDS:
        raise StoreError("Recovery operation exceeded its time limit.")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise OSError("Could not completely write recovery file.")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _table_shapes(connection: sqlite3.Connection) -> dict:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema LIMIT 257"
    ).fetchall()
    if len(rows) > 256 or any(row[0] not in {"table", "index"} for row in rows):
        raise StoreError("Backup has unsupported executable schema.")
    result = {}
    for kind, name, _table, sql in rows:
        if kind != "table":
            continue
        if sql is None or not sql.upper().startswith("CREATE TABLE "):
            raise StoreError("Backup has unsupported virtual or executable schema.")
        quoted = '"' + name.replace('"', '""') + '"'
        columns = connection.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
        if any(column[6] != 0 for column in columns):
            raise StoreError("Backup has unsupported generated columns.")
        # Column order and nullable legacy ALTER TABLE additions may differ after
        # supported migrations. Names, types, primary keys and ordinary storage
        # must match the trusted bootstrap; CHECK expressions confer no authority.
        foreign_keys = sorted(
            tuple(row[1:]) for row in connection.execute(f"PRAGMA foreign_key_list({quoted})")
        )
        indexes = []
        for index in connection.execute(f"PRAGMA index_list({quoted})"):
            index_name = '"' + index[1].replace('"', '""') + '"'
            details = connection.execute(f"PRAGMA index_xinfo({index_name})").fetchall()
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (index[1],)
            ).fetchone()[0]
            indexes.append(
                (
                    index[1],
                    index[2],
                    index[3],
                    index[4],
                    index_sql,
                    tuple(tuple(d[2:]) for d in details),
                )
            )
        result[name] = (
            sorted((c[1], c[2].upper(), c[5]) for c in columns),
            foreign_keys,
            sorted(indexes),
        )
    return result


@lru_cache(maxsize=1)
def _expected_tables() -> dict:
    from .store import _create_schema

    connection = sqlite3.connect(":memory:")
    try:
        _create_schema(connection)
        return _table_shapes(connection)
    finally:
        connection.close()


def _defensive(connection: sqlite3.Connection, start: float) -> None:
    connection.execute("PRAGMA trusted_schema=OFF")
    if hasattr(connection, "enable_load_extension"):
        connection.enable_load_extension(False)
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 128 * 1024)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
    connection.set_progress_handler(
        lambda: int(time.monotonic() - start > MAX_BACKUP_SECONDS), 1000
    )


def _validate_snapshot(path: Path, start: float | None = None) -> tuple[int, int]:
    start = time.monotonic() if start is None else start
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        _defensive(connection, start)
        _deadline(start)
        actual, expected = _table_shapes(connection), _expected_tables()
        if actual.keys() != expected.keys():
            raise StoreError("Backup schema does not match supported control tables.")
        for name, (columns, foreign_keys, indexes) in actual.items():
            expected_columns, expected_keys, expected_indexes = expected[name]
            # Early supported databases omitted some nonunique indexes and
            # foreign keys. Missing those cannot introduce cascading writes;
            # additional definitions or changed actions are never accepted.
            if (
                columns != expected_columns
                or any(key not in expected_keys for key in foreign_keys)
                or any(index not in expected_indexes for index in indexes)
                or any(index[1] and index not in indexes for index in expected_indexes)
            ):
                raise StoreError("Backup schema does not match supported control tables.")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise StoreError("Backup database integrity validation failed.")
        schema = connection.execute("SELECT schema_version FROM schema_meta").fetchall()
        recovery = connection.execute("SELECT singleton,state FROM control_recovery").fetchall()
        if (
            schema != [(LATEST_SCHEMA_VERSION,)]
            or len(recovery) != 1
            or recovery[0] not in {(1, "active"), (1, "held_restore")}
        ):
            raise StoreError("Backup database schema or recovery state is unsupported.")
        watermark = connection.execute("SELECT COALESCE(MAX(cursor), 0) FROM events").fetchone()[0]
        _deadline(start)
        return schema[0][0], watermark
    except sqlite3.Error as error:
        _deadline(start)
        raise StoreError("Backup database validation failed.") from error
    finally:
        connection.close()


def backup_state(state_dir: Path, output: Path) -> dict[str, Any]:
    """Copy one SQLite-consistent snapshot; source state remains active."""
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise StoreError("Source state directory is unsafe.")
    with _open_regular(state_dir / DATABASE_NAME, MAX_BACKUP_BYTES):
        pass
    if (
        output.exists()
        or output.is_symlink()
        or output.parent.is_symlink()
        or not output.parent.is_dir()
    ):
        raise StoreError("Backup output must be a new directory under a regular parent.")
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    snapshot = output / BACKUP_DATABASE
    start = time.monotonic()
    source = sqlite3.connect(
        (state_dir / DATABASE_NAME).absolute().as_uri() + "?mode=ro", uri=True, timeout=1
    )
    destination = _connect(snapshot)
    try:
        _defensive(source, start)
        source.execute("BEGIN")
        source.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if source.execute("PRAGMA page_count").fetchone()[0] * page_size > MAX_BACKUP_BYTES:
            raise StoreError("Backup exceeds its size limit.")

        def progress(_status: int, _remaining: int, total_pages: int) -> None:
            _deadline(start)
            if total_pages * page_size > MAX_BACKUP_BYTES:
                raise StoreError("Backup exceeds its size limit.")

        source.backup(destination, pages=128, progress=progress, sleep=0.001)
        # A WAL source copies its journal-mode header too. Normalize only the
        # private destination so validation cannot leave sidecars in the bundle.
        if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise StoreError("Backup could not produce a standalone snapshot.")
    finally:
        destination.close()
        source.close()
    os.chmod(snapshot, 0o600)
    if snapshot.stat().st_size > MAX_BACKUP_BYTES:
        raise StoreError("Backup exceeds its size limit.")
    schema, watermark = _validate_snapshot(snapshot, start)
    with snapshot.open("rb") as file:
        os.fsync(file.fileno())
    manifest = {
        "backup_id": uuid.uuid4().hex,
        "created_at": utc_now(),
        "event_watermark": watermark,
        "format_version": BACKUP_FORMAT_VERSION,
        "schema_version": schema,
        "sha256": _sha256(snapshot, start),
        "size": snapshot.stat().st_size,
    }
    _fsync_directory(output)
    _write_exclusive(
        output / BACKUP_MANIFEST,
        (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )
    _fsync_directory(output)
    _fsync_directory(output.parent)
    _deadline(start)
    return manifest


def _read_manifest(backup: Path) -> dict[str, Any]:
    if backup.is_symlink() or not backup.is_dir():
        raise StoreError("Backup directory is not safe.")
    with os.scandir(backup) as entries:
        names = set()
        for entry in entries:
            names.add(entry.name)
            if len(names) > 2:
                raise StoreError("Backup contains unsupported files.")
    if names != {BACKUP_DATABASE, BACKUP_MANIFEST}:
        raise StoreError("Backup directory is incomplete.")
    try:
        with _open_regular(backup / BACKUP_MANIFEST, MAX_MANIFEST_BYTES) as file:
            encoded = file.read(MAX_MANIFEST_BYTES + 1)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise StoreError("Backup manifest exceeds recovery limits.")
        manifest = json.loads(encoded)
    except (OSError, ValueError) as error:
        raise StoreError("Backup manifest is invalid.") from error
    required = {
        "backup_id",
        "created_at",
        "event_watermark",
        "format_version",
        "schema_version",
        "sha256",
        "size",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise StoreError("Backup manifest is invalid.")
    for name in ("event_watermark", "format_version", "schema_version", "size"):
        if type(manifest[name]) is not int or manifest[name] < 0:
            raise StoreError("Backup manifest numbers must be nonnegative integers.")
    if (
        manifest["format_version"] != BACKUP_FORMAT_VERSION
        or manifest["schema_version"] != LATEST_SCHEMA_VERSION
        or not 1 <= manifest["size"] <= MAX_BACKUP_BYTES
        or not isinstance(manifest["backup_id"], str)
        or len(manifest["backup_id"]) != 32
        or not isinstance(manifest["created_at"], str)
        or not 1 <= len(manifest["created_at"]) <= 64
        or not isinstance(manifest["sha256"], str)
        or len(manifest["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in manifest["sha256"] + manifest["backup_id"])
    ):
        raise StoreError("Backup checksum or metadata is invalid.")
    # SQLite validation is intentionally deferred until bytes have been copied
    # and hash-checked in a private stage. Never query a changing supplied DB.
    return manifest


def restore_state(backup: Path, state_dir: Path) -> tuple[Path, Path]:
    """Create a new held restore with all prior authority revoked."""
    start = time.monotonic()
    manifest = _read_manifest(backup)
    _deadline(start)
    if (
        state_dir.exists()
        or state_dir.is_symlink()
        or state_dir.parent.is_symlink()
        or not state_dir.parent.is_dir()
    ):
        raise StoreError("Restore destination must be a new directory under a regular parent.")
    state_dir.mkdir(mode=0o700)
    os.chmod(state_dir, 0o700)
    staged = state_dir / "restored.sqlite3"
    copied = 0
    digest = hashlib.sha256()
    with (
        _open_regular(backup / BACKUP_DATABASE, manifest["size"]) as source,
        staged.open("xb") as destination,
    ):
        os.fchmod(destination.fileno(), 0o600)
        while chunk := source.read(1024 * 1024):
            _deadline(start)
            copied += len(chunk)
            if copied > MAX_BACKUP_BYTES:
                raise StoreError("Backup exceeds recovery limits.")
            digest.update(chunk)
            destination.write(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    os.chmod(staged, 0o600)
    if copied != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
        raise StoreError("Backup changed while it was being restored.")
    schema, watermark = _validate_snapshot(staged, start)
    if schema != manifest["schema_version"] or watermark != manifest["event_watermark"]:
        raise StoreError("Restored backup metadata does not match its manifest.")
    token = secrets.token_urlsafe(32)
    now = utc_now()
    connection = _connect(staged)
    try:
        _defensive(connection, start)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE principals SET revoked_at=COALESCE(revoked_at, ?)", (now,))
        connection.execute("UPDATE credentials SET revoked_at=COALESCE(revoked_at, ?)", (now,))
        principal_id, credential_id = (
            "principal_" + uuid.uuid4().hex,
            "credential_" + uuid.uuid4().hex,
        )
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES (?, 'org_local', 'human', ?, NULL, ?)""",
            (principal_id, _token_hash(token), now),
        )
        connection.execute(
            """INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id,
            expires_at, revoked_at, created_at, created_by_principal_id)
            VALUES (?, ?, 'local_owner', 'control', NULL, NULL, NULL, NULL, ?, ?)""",
            (credential_id, principal_id, now, principal_id),
        )
        connection.execute(
            """UPDATE control_recovery SET state='held_restore', incarnation=?, restore_id=?,
            source_backup_digest=?, event_watermark=?, restored_at=? WHERE singleton=1""",
            (
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                manifest["sha256"],
                manifest["event_watermark"],
                now,
            ),
        )
        held = connection.execute("SELECT state FROM control_recovery").fetchall()
        live_principals = connection.execute(
            "SELECT id FROM principals WHERE revoked_at IS NULL"
        ).fetchall()
        live_credentials = connection.execute(
            "SELECT id,principal_id,kind,audience,parent_id,project_id "
            "FROM credentials WHERE revoked_at IS NULL"
        ).fetchall()
        if (
            [tuple(row) for row in held] != [("held_restore",)]
            or [tuple(row) for row in live_principals] != [(principal_id,)]
            or [tuple(row) for row in live_credentials]
            != [(credential_id, principal_id, "local_owner", "control", None, None)]
        ):
            raise StoreError("Restore fencing invariants failed.")
        _deadline(start)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    with staged.open("rb") as file:
        os.fsync(file.fileno())
    _write_exclusive(state_dir / OWNER_HEADER_NAME, f"Authorization: Bearer {token}\n".encode())
    database = state_dir / DATABASE_NAME
    try:
        os.link(staged, database)
    except FileExistsError as error:
        raise StoreError("Restore database publication raced with another writer.") from error
    staged.unlink()
    _fsync_directory(state_dir)
    _write_exclusive(state_dir / RESTORE_MARKER, b"held restore complete\n")
    _fsync_directory(state_dir)
    _fsync_directory(state_dir.parent)
    _deadline(start)
    return database, state_dir / OWNER_HEADER_NAME
