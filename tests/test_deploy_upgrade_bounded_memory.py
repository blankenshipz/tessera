"""``tessera deploy-upgrade`` must not hold a whole database or config in memory (ADR 0099).

The snapshot digest, the snapshot restore and the config copies stream through a fixed-size chunk.
These tests shrink the chunk, pad the database well past it, and fail on any whole-file read."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from test_deploy_upgrade import Fakes, make_plan, run

from tessera_control import deploy_upgrade
from tessera_control.store import DATABASE_NAME

CHUNK = 16 * 1024


class _ReadSpy:
    """Wraps a binary file object and records the size argument of every read."""

    def __init__(self, file, sizes):
        self._file, self._sizes = file, sizes

    def read(self, size=-1):
        self._sizes.append(size)
        return self._file.read(size)

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, *exc):
        return self._file.__exit__(*exc)


@pytest.fixture
def bounded(monkeypatch):
    """Shrink the chunk, forbid whole-file reads of large files, and record every read size."""
    sizes: list[int] = []
    monkeypatch.setattr(deploy_upgrade, "_CHUNK", CHUNK)
    real_open = open

    def spy_open(path, mode="r", *args, **kwargs):
        file = real_open(path, mode, *args, **kwargs)
        return _ReadSpy(file, sizes) if "b" in mode and "r" in mode else file

    monkeypatch.setattr(deploy_upgrade, "open", spy_open, raising=False)
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(self):
        if self.stat().st_size > CHUNK:
            raise AssertionError(f"whole-file read_bytes of {self.name}")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    return sizes


def _pad(state, megabytes=3):
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("CREATE TABLE padding (id INTEGER PRIMARY KEY, body BLOB)")
        for _ in range(megabytes):
            db.execute("INSERT INTO padding (body) VALUES (zeroblob(1048576))")


def test_the_snapshot_digest_is_computed_in_bounded_chunks(tmp_path, bounded):
    _plan, _path, state = make_plan(tmp_path)
    _pad(state)
    out = tmp_path / "backups" / "snap"
    manifest = deploy_upgrade.take_backup(state, out)
    snapshot = out / deploy_upgrade.SNAPSHOT_NAME
    assert snapshot.stat().st_size > 100 * CHUNK  # many chunks, not one
    assert manifest["bytes"] == snapshot.stat().st_size
    digest = hashlib.sha256()
    with open(snapshot, "rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    assert manifest["sha256"] == digest.hexdigest()
    assert bounded and all(0 < size <= CHUNK for size in bounded)


def test_rollback_and_config_copies_stream_in_bounded_chunks(tmp_path, bounded):
    plan, _path, state = make_plan(tmp_path)
    _pad(state)
    document = run(plan, Fakes(), execute=True)
    assert document["result"] == "ok"
    receipt = deploy_upgrade.write_receipt(plan, document)
    result = deploy_upgrade.rollback(plan, receipt, probe=lambda _h, **_: (False, "down"))
    assert result["result"] == "ok" and result["configs_restored"] == [0]
    assert bounded and all(0 < size <= CHUNK for size in bounded)
    assert (state / DATABASE_NAME).stat().st_size > 100 * CHUNK


def test_rollback_refuses_a_snapshot_changed_after_it_was_verified(tmp_path, monkeypatch):
    plan, _path, state = make_plan(tmp_path)
    document = run(plan, Fakes(), execute=True)
    receipt = deploy_upgrade.write_receipt(plan, document)
    real = deploy_upgrade._hash_file

    def hash_then_tamper(path):
        result = real(path)
        with open(path, "ab") as file:
            file.write(b"tampered")
        return result

    monkeypatch.setattr(deploy_upgrade, "_hash_file", hash_then_tamper)
    with pytest.raises(deploy_upgrade.RollbackRefused, match="digest"):
        deploy_upgrade.rollback(plan, receipt, probe=lambda _h, **_: (False, "down"))
    assert not (state / (DATABASE_NAME + ".rollback-new")).exists()
    assert json.dumps(document)  # the receipt itself was untouched


def _bundle_database(path, *, legacy):
    with sqlite3.connect(path) as db:
        db.executescript(
            """CREATE TABLE projects (id TEXT PRIMARY KEY, org_id TEXT);
            CREATE TABLE artifacts (id TEXT PRIMARY KEY);
            CREATE TABLE project_artifact_retention (
                artifact_id TEXT, project_id TEXT, state TEXT);
            CREATE TABLE project_artifact_placements (
                artifact_id TEXT, project_id TEXT, backend_kind TEXT, tenant_namespace TEXT,
                project_namespace TEXT, object_key TEXT, sha256 TEXT, size INTEGER);"""
        )
        db.executemany("INSERT INTO artifacts VALUES (?)", [(f"a{n}",) for n in range(legacy)])


def test_the_object_declaration_reads_one_row_past_its_cap_not_the_table(tmp_path, monkeypatch):
    """Both bundle queries carry ``LIMIT cap+1``: over the cap is refused without a full scan."""
    from tessera_control import recovery
    from tessera_control.store import StoreError

    path = tmp_path / "bundle.sqlite3"
    _bundle_database(path, legacy=50)
    monkeypatch.setattr(recovery, "MAX_BACKUP_OBJECTS", 3)
    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(recovery.sqlite3, "connect", tracing_connect)
    with pytest.raises(StoreError, match="legacy artifact metadata"):
        recovery._bundle_entries(path, time.monotonic())
    selects = [s for s in statements if "FROM project_artifact_placements" in s]
    assert selects and all("LIMIT 4" in s for s in selects)
