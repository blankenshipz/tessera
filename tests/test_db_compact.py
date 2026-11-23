from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from tessera_control import db_compact, maintenance_lock
from tessera_control.cli import main
from tessera_control.recovery import backup_state, restore_state, verify_restorable
from tessera_control.storage_retention_backups import list_backups
from tessera_control.store import StoreError, initialize_state

DATABASE = "tessera.sqlite3"


def run_cli(monkeypatch, *arguments: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["tessera", *arguments])
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = main()
    return result, stdout.getvalue(), stderr.getvalue()


def bloated_state(tmp_path: Path, *, wal: bool = False, rows: int = 1500) -> Path:
    """A real state directory whose database has a long freelist (rows inserted then deleted)."""
    state = tmp_path / "state"
    initialize_state(state)
    connection = sqlite3.connect(state / DATABASE, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if wal:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        principal = connection.execute("SELECT id FROM principals").fetchone()[0]
        connection.execute(
            "INSERT INTO projects (id, org_id, name, created_at) "
            "VALUES ('project_bulk', 'org_local', 'Bulk', '2026-01-01T00:00:00Z')"
        )
        connection.execute("BEGIN")
        for index in range(rows):
            connection.execute(
                """INSERT INTO events (org_id, project_id, task_id, event_type,
                resource_revision, actor_principal_id, created_at, payload_json)
                VALUES ('org_local', 'project_bulk', NULL, 'bulk.filler', ?, ?,
                '2026-01-01T00:00:00Z', ?)""",
                (index, principal, json.dumps({"pad": "x" * 2000})),
            )
        connection.execute("COMMIT")
        connection.execute("DELETE FROM events WHERE resource_revision < ?", (rows - 5,))
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 500
    finally:
        connection.close()
    return state


def with_policy(tmp_path: Path, state: Path) -> Path:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    (state / "storage-retention.json").write_text(json.dumps({"backup_root": str(root)}))
    return root


def tree(path: Path) -> dict[str, bytes]:
    """Every file under path with its bytes, except the advisory lock file."""
    found = {}
    for directory, _dirs, files in os.walk(path):
        for name in files:
            if name == maintenance_lock.MAINTENANCE_LOCK_NAME:
                continue
            full = Path(directory) / name
            found[str(full.relative_to(path))] = full.read_bytes()
    return found


def counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
    finally:
        connection.close()


def test_compacts_registers_backup_and_restores_previous_content(
    tmp_path: Path, monkeypatch
) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    database = state / DATABASE
    before_bytes = database.stat().st_size
    before_counts = counts(database)

    status, stdout, stderr = run_cli(
        monkeypatch, "db", "compact", "--state-dir", str(state), "--min-free-bytes", "0"
    )
    assert status == 0, stderr
    assert stderr == ""
    receipt = json.loads(stdout)
    assert receipt["status"] == "compacted"
    assert receipt["integrity"] == "ok"
    assert receipt["bytes_before"] == before_bytes
    assert receipt["bytes_after"] == database.stat().st_size < before_bytes
    assert receipt["bytes_reclaimed"] == before_bytes - receipt["bytes_after"]
    assert receipt["freelist_pages_before"] > 500
    assert receipt["page_count_after"] < receipt["page_count_before"]
    assert receipt["tables"] == before_counts == counts(database)
    assert receipt["required_free_bytes"] > 0 and receipt["free_disk_bytes"] > 0
    assert receipt["duration_seconds"] >= 0
    assert receipt["started_at"] <= receipt["finished_at"]

    # The receipt file is the printed receipt, private, in a private directory.
    receipts = list((state / "db-compact").iterdir())
    assert [path.name for path in receipts] == [
        path.name for path in receipts if path.name.endswith(f"-{receipt['run_id']}.json")
    ]
    assert json.loads(receipts[0].read_text()) == receipt
    assert stat.S_IMODE(receipts[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "db-compact").stat().st_mode) == 0o700

    # The new file is private, clean, and leaves nothing behind.
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
    finally:
        connection.close()
    leftovers = [
        name
        for name in os.listdir(state)
        if name.startswith(db_compact.TEMP_PREFIX) or name.startswith(db_compact.PREVIOUS_PREFIX)
    ]
    assert leftovers == []

    # The previous database is a restorable bundle the retention policy lists.
    backup = receipt["backup"]
    assert backup["registered_with_retention"] is True
    assert "note" not in backup
    bundle = root / backup["path"]
    manifest = verify_restorable(bundle)
    assert manifest["backup_id"] == backup["backup_id"]
    assert manifest["sha256"] == backup["sha256"] and manifest["size"] == backup["size"]
    listed, skipped = list_backups(root)
    assert skipped == []
    assert [(c.path, c.ref) for c in listed] == [(backup["path"], backup["backup_id"])]

    restored = tmp_path / "restored"
    status, _stdout, stderr = run_cli(
        monkeypatch, "restore", "--backup", str(bundle), "--state-dir", str(restored)
    )
    assert status == 0, stderr
    restored_counts = counts(restored / DATABASE)
    assert restored_counts["events"] == before_counts["events"]
    assert restored_counts["projects"] == before_counts["projects"]

    # No secret or absolute local path is written to the receipt.
    text = receipts[0].read_text()
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    assert token not in text
    assert str(state) not in text and str(root) not in text


def test_refuses_while_the_maintenance_lock_is_held(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    before = tree(state)
    with maintenance_lock.shared(state):
        with pytest.raises(maintenance_lock.MaintenanceBusy, match="stop it first"):
            db_compact.compact_state(state, min_free_bytes=0)
    assert tree(state) == before
    assert list(root.iterdir()) == []


def test_refuses_when_another_connection_holds_the_database(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    before = tree(state)
    holder = sqlite3.connect(state / DATABASE, isolation_level=None)
    try:
        holder.execute("BEGIN")
        holder.execute("SELECT count(*) FROM events").fetchone()
        with pytest.raises(StoreError, match="stop all services"):
            db_compact.compact_state(state, min_free_bytes=0)
    finally:
        holder.close()
    assert tree(state) == before
    assert list(root.iterdir()) == []


def test_refuses_below_the_free_disk_floor_before_creating_anything(
    tmp_path: Path, monkeypatch
) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    before = tree(state)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(10**12, 10**12 - 4096, 4096))
    with pytest.raises(StoreError, match=r"bytes required, 4096 free"):
        db_compact.compact_state(state)
    assert tree(state) == before
    assert list(root.iterdir()) == []

    # The default floor is 8 GiB even for a tiny database.
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: usage(10**13, 0, db_compact.DEFAULT_MIN_FREE_BYTES - 1)
    )
    with pytest.raises(StoreError, match=str(db_compact.DEFAULT_MIN_FREE_BYTES - 1)):
        db_compact.compact_state(state)
    assert tree(state) == before and list(root.iterdir()) == []


def test_refuses_without_a_backup_root(tmp_path: Path, monkeypatch) -> None:
    state = bloated_state(tmp_path)
    before = tree(state)
    status, stdout, stderr = run_cli(
        monkeypatch, "db", "compact", "--state-dir", str(state), "--min-free-bytes", "0"
    )
    assert status == 1 and stdout == ""
    assert stderr.startswith("db compact failed: ") and "--backup-root" in stderr
    assert "Traceback" not in stderr
    assert tree(state) == before
    assert not (state / maintenance_lock.MAINTENANCE_LOCK_NAME).exists()


def test_backup_root_outside_the_policy_is_reported_as_unregistered(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    receipt = db_compact.compact_state(state, backup_root=elsewhere, min_free_bytes=0)
    assert receipt["backup"]["registered_with_retention"] is False
    assert "retention will not" in receipt["backup"]["note"]
    verify_restorable(elsewhere / receipt["backup"]["path"])


def test_refuses_held_restore_and_symlinked_database(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    backup_state(state, tmp_path / "bundle")
    held = tmp_path / "held"
    restore_state(tmp_path / "bundle", held)
    root = tmp_path / "root"
    root.mkdir()
    before = tree(held)
    with pytest.raises(StoreError, match="held restore"):
        db_compact.compact_state(held, backup_root=root, min_free_bytes=0)
    assert tree(held) == before and list(root.iterdir()) == []

    linked = tmp_path / "linked"
    linked.mkdir(mode=0o700)
    (linked / DATABASE).symlink_to(state / DATABASE)
    with pytest.raises(StoreError, match="not a regular file"):
        db_compact.compact_state(linked, backup_root=root, min_free_bytes=0)
    assert list(root.iterdir()) == []

    (state / ".authority-moved").write_text("moved\n")
    with pytest.raises(StoreError, match="authority was moved"):
        db_compact.compact_state(state, backup_root=root, min_free_bytes=0)


def test_parity_mismatch_aborts_and_leaves_the_original_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    original = (state / DATABASE).read_bytes()
    real = db_compact._fingerprint

    def tampered(connection: sqlite3.Connection):
        result = real(connection)
        if db_compact.TEMP_PREFIX in connection.execute("PRAGMA database_list").fetchone()[2]:
            result["tables"]["events"] += 1
        return result

    monkeypatch.setattr(db_compact, "_fingerprint", tampered)
    with pytest.raises(StoreError, match="does not match the source"):
        db_compact.compact_state(state, min_free_bytes=0)
    assert (state / DATABASE).read_bytes() == original
    assert not any(name.startswith(db_compact.TEMP_PREFIX) for name in os.listdir(state))
    assert not (state / db_compact.RECEIPT_DIR).exists()
    assert list(root.iterdir()) == []


def test_post_swap_failure_puts_the_previous_file_back(tmp_path: Path, monkeypatch) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    original = (state / DATABASE).read_bytes()

    def broken(_database, _expected):
        raise StoreError("Simulated post-swap failure.")

    monkeypatch.setattr(db_compact, "_post_verify", broken)
    with pytest.raises(StoreError, match="previous database was put back"):
        db_compact.compact_state(state, min_free_bytes=0)
    assert (state / DATABASE).read_bytes() == original
    assert (state / DATABASE).stat().st_nlink == 1
    assert not any(
        name.startswith((db_compact.TEMP_PREFIX, db_compact.PREVIOUS_PREFIX))
        for name in os.listdir(state)
    )
    assert not (state / db_compact.RECEIPT_DIR).exists()
    # The verified pre-compaction backup is kept.
    [bundle] = list(root.iterdir())
    verify_restorable(bundle)


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    before = tree(state)
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
    assert plan["freelist_pages_before"] > 500
    assert 0 < plan["estimated_bytes_reclaimed"] < plan["bytes_before"]
    assert plan["backup_registered_with_retention"] is True
    assert tree(state) == before
    assert list(root.iterdir()) == []


def test_wal_source_keeps_wal_mode_without_stale_sidecars(tmp_path: Path) -> None:
    state = bloated_state(tmp_path, wal=True)
    with_policy(tmp_path, state)
    database = state / DATABASE
    before_counts = counts(database)
    receipt = db_compact.compact_state(state, min_free_bytes=0)
    assert receipt["journal_mode"] == "wal"
    assert receipt["bytes_reclaimed"] > 0
    for suffix in ("-wal", "-shm", "-journal"):
        assert not os.path.lexists(f"{database}{suffix}")
    header = database.read_bytes()[:20]
    assert header[18:20] == b"\x02\x02"  # WAL read/write format versions
    assert counts(database) == before_counts


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    state = bloated_state(tmp_path)
    root = with_policy(tmp_path, state)
    first = db_compact.compact_state(state, min_free_bytes=0)
    second = db_compact.compact_state(state, min_free_bytes=0)
    assert second["status"] == "compacted"
    assert second["freelist_pages_before"] == 0
    assert second["tables"] == first["tables"]
    assert second["bytes_after"] == first["bytes_after"]
    listed, skipped = list_backups(root)
    assert skipped == [] and len(listed) == 2
    assert len(list((state / db_compact.RECEIPT_DIR).iterdir())) == 2


def test_usage_errors_exit_2(monkeypatch) -> None:
    with pytest.raises(SystemExit) as raised:
        run_cli(monkeypatch, "db", "compact", "--state-dir", "x", "--min-free-bytes", "-1")
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        run_cli(monkeypatch, "db", "compact")
    assert raised.value.code == 2
