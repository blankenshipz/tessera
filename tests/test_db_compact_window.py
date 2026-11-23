"""The window between the copy and the swap: the write guard, the on-disk identity check,
a receipt that cannot be written after the swap, and a dry run that writes nothing."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_db_compact import DATABASE, bloated_state, counts, run_cli, with_policy

from tessera_control import db_compact, maintenance_lock, recovery, storage_retention
from tessera_control.store import StoreError


def try_insert(database: Path, project_id: str) -> str:
    """Commit one row from a second connection that waits at most 10 ms."""
    connection = sqlite3.connect(database, timeout=0.01, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=10")
        connection.execute(
            "INSERT INTO projects (id, org_id, name, created_at) "
            "VALUES (?, 'org_local', 'Late', '2026-01-01T00:00:00Z')",
            (project_id,),
        )
    except sqlite3.OperationalError as error:
        return str(error)
    finally:
        connection.close()
    return "committed"


def leftovers(state: Path) -> list[str]:
    return [
        name
        for name in os.listdir(state)
        if name.startswith((db_compact.TEMP_PREFIX, db_compact.PREVIOUS_PREFIX))
    ]


def snapshot(path: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every entry under path (the lock file included) with mode, mtime and content."""
    found = {}
    for directory, dirs, files in os.walk(path):
        for name in dirs + files:
            full = Path(directory) / name
            info = full.lstat()
            content = full.read_bytes() if full.is_file() else b""
            found[str(full.relative_to(path))] = (info.st_mode, info.st_mtime_ns, content)
    return found


def test_write_guard_is_held_through_backup_verification_and_the_rename(
    tmp_path: Path, monkeypatch
) -> None:
    state = bloated_state(tmp_path)
    with_policy(tmp_path, state)
    database = state / DATABASE
    before = counts(database)
    attempts: list[tuple[str, str]] = []

    real_verify = recovery.verify_restorable

    def verify_with_a_writer(bundle: Path):
        attempts.append(("verify_restorable", try_insert(database, "project_during_verify")))
        return real_verify(bundle)

    real_replace = os.replace

    def replace_with_a_writer(source, destination, *args, **kwargs):
        if Path(source).name.startswith(db_compact.TEMP_PREFIX) and Path(destination) == database:
            attempts.append(("os.replace", try_insert(database, "project_during_replace")))
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(recovery, "verify_restorable", verify_with_a_writer)
    monkeypatch.setattr(os, "replace", replace_with_a_writer)
    receipt = db_compact.compact_state(state, min_free_bytes=0)

    assert attempts == [
        ("verify_restorable", "database is locked"),
        ("os.replace", "database is locked"),
    ]
    assert receipt["status"] == "compacted"
    assert counts(database) == before
    assert leftovers(state) == []
    # Released after the swap: the service can write to the compacted file again.
    assert try_insert(database, "project_after") == "committed"


@pytest.mark.parametrize("keep_mtime", [True, False], ids=["header-only", "mtime-only"])
def test_same_size_write_that_bypasses_the_guard_aborts_the_swap(
    tmp_path: Path, monkeypatch, keep_mtime: bool
) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    database = state / DATABASE
    size = database.stat().st_size
    modified: dict[str, bytes] = {}
    real_link = os.link

    def link_then_write_behind_the_guard(source, destination, *args, **kwargs):
        real_link(source, destination, *args, **kwargs)
        if not Path(destination).name.startswith(db_compact.PREVIOUS_PREFIX):
            return
        info = database.stat()
        with open(database, "r+b") as handle:  # no SQLite locking at all
            if keep_mtime:
                header = bytearray(handle.read(100))
                counter = int.from_bytes(header[24:28], "big") + 1
                handle.seek(24)
                handle.write(counter.to_bytes(4, "big"))
            handle.seek(size - 1)
            last = handle.read(1)
            handle.seek(size - 1)
            handle.write(bytes([last[0] ^ 0xFF]))
        if keep_mtime:
            os.utime(database, ns=(info.st_atime_ns, info.st_mtime_ns))
        else:
            os.utime(database, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        modified["bytes"] = database.read_bytes()

    monkeypatch.setattr(os, "link", link_then_write_behind_the_guard)
    with pytest.raises(StoreError, match="changed on disk during compaction") as raised:
        db_compact.compact_state(state, min_free_bytes=0)
    detail = str(raised.value)
    assert ("change_counter" in detail) is keep_mtime
    assert ("mtime_ns" in detail) is not keep_mtime

    # Compaction left the file as the bypassing write made it, and cleaned up after itself.
    assert database.stat().st_size == size
    assert database.read_bytes() == modified["bytes"]
    assert database.stat().st_nlink == 1
    assert leftovers(state) == []
    assert list(root.iterdir()) == []
    assert not (state / db_compact.RECEIPT_DIR).exists()


def test_receipt_write_failure_after_the_swap_still_succeeds(tmp_path: Path, monkeypatch) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    database = state / DATABASE
    before_bytes = database.stat().st_size
    before = counts(database)

    def failing(directory: Path, _name: str, _receipt) -> None:
        raise PermissionError(13, "Permission denied", str(directory))

    monkeypatch.setattr(storage_retention, "_write_receipt", failing)
    status, stdout, stderr = run_cli(
        monkeypatch, "db", "compact", "--state-dir", str(state), "--min-free-bytes", "0"
    )
    assert status == 0
    assert stderr == "db compact: compaction completed; receipt not written\n"
    result = json.loads(stdout)
    assert result["status"] == "compacted"
    assert result["receipt_written"] is False
    assert result["receipt_error"] == "PermissionError"
    assert str(state) not in stdout and str(tmp_path) not in stdout
    assert result["bytes_after"] == database.stat().st_size < before_bytes
    assert counts(database) == before
    assert leftovers(state) == []
    assert [path.name for path in root.iterdir()] == [result["backup"]["path"]]
    assert not (state / db_compact.RECEIPT_DIR).exists() or not any(
        (state / db_compact.RECEIPT_DIR).iterdir()
    )


def test_written_receipt_says_so(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    with_policy(tmp_path, state)
    receipt = db_compact.compact_state(state, min_free_bytes=0)
    assert receipt["receipt_written"] is True and "receipt_error" not in receipt
    [written] = (state / db_compact.RECEIPT_DIR).iterdir()
    assert json.loads(written.read_text()) == receipt


@pytest.mark.parametrize("wal", [False, True], ids=["rollback", "wal"])
def test_dry_run_creates_nothing_not_even_the_lock_file(
    tmp_path: Path, monkeypatch, wal: bool
) -> None:
    state = bloated_state(tmp_path, wal=wal)
    root = with_policy(tmp_path, state)
    before_state, before_root = snapshot(state), snapshot(root)
    assert maintenance_lock.MAINTENANCE_LOCK_NAME not in before_state
    status, stdout, stderr = run_cli(
        monkeypatch,
        "db",
        "compact",
        "--state-dir",
        str(state),
        "--min-free-bytes",
        "0",
        "--dry-run",
    )
    assert status == 0, stderr
    plan = json.loads(stdout)
    assert plan["status"] == "dry_run"
    assert plan["journal_mode"] == ("wal" if wal else "delete")
    assert plan["freelist_pages_before"] > 500
    assert snapshot(state) == before_state
    assert snapshot(root) == before_root
    assert not (state / maintenance_lock.MAINTENANCE_LOCK_NAME).exists()


def test_dry_run_refuses_while_the_service_holds_the_lock(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    with maintenance_lock.shared(state):
        before = snapshot(state)
        with pytest.raises(maintenance_lock.MaintenanceBusy):
            db_compact.compact_state(state, min_free_bytes=0, dry_run=True)
        assert snapshot(state) == before
    assert list(root.iterdir()) == []
