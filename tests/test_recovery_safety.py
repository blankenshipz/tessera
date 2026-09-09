"""Adversarial supplied-backup and publication fixtures; no real state is touched."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from test_plans import _legacy_state
from test_recovery_boundaries import _backup

import tessera_control.recovery as recovery
from tessera_control.api import create_app
from tessera_control.store import StoreError, migrate_state


def _refresh_manifest(backup: Path) -> None:
    path = backup / recovery.BACKUP_DATABASE
    manifest_path = backup / recovery.BACKUP_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest.update(size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "schema",
    [
        """CREATE TRIGGER undo_fence AFTER UPDATE OF state ON control_recovery
        BEGIN UPDATE control_recovery SET state='active';
        UPDATE principals SET revoked_at=NULL; UPDATE credentials SET revoked_at=NULL; END""",
        "CREATE VIEW unexpected AS SELECT * FROM principals",
        "CREATE TABLE unexpected (data TEXT)",
        "CREATE INDEX unexpected ON projects(name)",
        "ALTER TABLE projects ADD COLUMN unexpected TEXT REFERENCES control_recovery(state)",
    ],
)
def test_restore_rejects_untrusted_schema_before_authority_updates(tmp_path: Path, schema: str):
    _source, backup = _backup(tmp_path)
    with sqlite3.connect(backup / recovery.BACKUP_DATABASE) as connection:
        connection.execute(schema)
    _refresh_manifest(backup)
    restored = tmp_path / "restored"
    with pytest.raises(StoreError, match="schema|tables"):
        recovery.restore_state(backup, restored)
    assert not (restored / recovery.DATABASE_NAME).exists()
    assert not (restored / recovery.RESTORE_MARKER).exists()


def test_supported_legacy_migration_can_be_backed_up_and_restored(tmp_path: Path):
    source = _legacy_state(tmp_path)
    assert migrate_state(source) == 23
    backup = tmp_path / "backup"
    recovery.backup_state(source, backup)
    restored = tmp_path / "restored"
    recovery.restore_state(backup, restored)
    create_app(restored)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_changed_backup_node_is_rejected_without_blocking(tmp_path: Path, kind: str):
    _source, backup = _backup(tmp_path)
    snapshot = backup / recovery.BACKUP_DATABASE
    held = tmp_path / "held.sqlite3"
    snapshot.rename(held)
    if kind == "symlink":
        snapshot.symlink_to(held)
    elif kind == "hardlink":
        os.link(held, snapshot)
    else:
        os.mkfifo(snapshot)
    with pytest.raises((StoreError, OSError)):
        recovery.restore_state(backup, tmp_path / "restored")


def test_backup_progress_counts_pages_in_bytes(tmp_path: Path, monkeypatch):
    source, _backup_path = _backup(tmp_path)
    real_connect = sqlite3.connect
    source_uri = (source / recovery.DATABASE_NAME).absolute().as_uri() + "?mode=ro"

    class GrowingSnapshot:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def backup(self, _destination, *, progress, **_kwargs):
            # Correct calculation is 40,000 pages * 4096 bytes, not 1 * 40,000.
            progress(sqlite3.SQLITE_OK, 1, 40_000)

    def connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        return GrowingSnapshot(connection) if path == source_uri else connection

    monkeypatch.setattr(recovery.sqlite3, "connect", connect)
    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 128 * 1024 * 1024)
    with pytest.raises(StoreError, match="size limit"):
        recovery.backup_state(source, tmp_path / "oversized")
    assert not (tmp_path / "oversized" / recovery.BACKUP_MANIFEST).exists()


def test_hash_limit_and_deadline_are_checked_while_reading(tmp_path: Path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"bounded bytes")
    with pytest.raises(StoreError, match="time limit"):
        recovery._sha256(path, time.monotonic() - recovery.MAX_BACKUP_SECONDS - 1)
    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 1)
    with pytest.raises(StoreError, match="bounded"):
        recovery._sha256(path, time.monotonic())


def test_exclusive_writes_handle_short_writes(tmp_path: Path, monkeypatch):
    write = os.write
    monkeypatch.setattr(recovery.os, "write", lambda fd, data: write(fd, data[:3]))
    path = tmp_path / "private"
    recovery._write_exclusive(path, b"complete header and marker")
    assert path.read_bytes() == b"complete header and marker"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fail_name", [recovery.OWNER_HEADER_NAME, recovery.RESTORE_MARKER])
def test_publication_fault_never_exposes_an_accepted_state(tmp_path: Path, monkeypatch, fail_name):
    _source, backup = _backup(tmp_path)
    original = recovery._write_exclusive

    def fail(path, data, mode=0o600):
        if path.name == fail_name:
            raise OSError("publication fault")
        return original(path, data, mode)

    monkeypatch.setattr(recovery, "_write_exclusive", fail)
    restored = tmp_path / "restored"
    with pytest.raises(OSError, match="publication fault"):
        recovery.restore_state(backup, restored)
    with pytest.raises(ValueError):
        create_app(restored)


def test_recovery_publication_flushes_parent_directory(tmp_path: Path, monkeypatch):
    source, _original = _backup(tmp_path)
    calls = []
    original = recovery._fsync_directory

    def observe(path):
        calls.append(path)
        original(path)

    monkeypatch.setattr(recovery, "_fsync_directory", observe)
    backup = tmp_path / "second-backup"
    recovery.backup_state(source, backup)
    assert calls[-1] == tmp_path
    calls.clear()
    recovery.restore_state(backup, tmp_path / "restored")
    assert calls[-1] == tmp_path


def test_restore_rejects_changed_foreign_key_actions(tmp_path: Path):
    source, backup = _backup(tmp_path)
    database = backup / recovery.BACKUP_DATABASE
    with sqlite3.connect(database) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='projects'"
        ).fetchone()[0]
        # Isolated supplied-file fixture changes no columns, only FK behavior.
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql=? WHERE name='projects'",
            (
                original.replace(
                    "REFERENCES organizations(id)", "REFERENCES organizations(id) ON UPDATE CASCADE"
                ),
            ),
        )
    _refresh_manifest(backup)
    restored = tmp_path / "restored"
    with pytest.raises(StoreError, match="schema"):
        recovery.restore_state(backup, restored)
    assert not (restored / recovery.DATABASE_NAME).exists()
    assert not (restored / recovery.RESTORE_MARKER).exists()
    with sqlite3.connect(source / recovery.DATABASE_NAME) as connection:
        assert (
            connection.execute("SELECT sql FROM sqlite_schema WHERE name='projects'").fetchone()[0]
            == original
        )
