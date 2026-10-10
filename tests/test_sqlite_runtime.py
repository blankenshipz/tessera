"""Runtime admission stays separate from untrusted backup schema validation."""

import sqlite3
import sys
from pathlib import Path

import pytest

from tessera_control import cli, recovery, store


@pytest.fixture(autouse=True)
def clear_runtime_cache():
    store._check_sqlite_runtime.cache_clear()
    yield
    store._check_sqlite_runtime.cache_clear()


def runtime_factory(flags, *, unavailable=False):
    real_connect = sqlite3.connect
    calls = []

    class Connection:
        def __init__(self):
            self.connection = real_connect(":memory:")

        def execute(self, sql):
            if "pragma_function_list" in sql:
                if unavailable:
                    raise sqlite3.OperationalError("no such table: pragma_function_list")
                if flags is None:
                    return self.connection.execute("SELECT 0 WHERE 0")
                return self.connection.execute("SELECT ?", (flags,))
            return self.connection.execute(sql)

        def close(self):
            self.connection.close()

    def connect(path, *args, **kwargs):
        calls.append(path)
        assert path == ":memory:", "unsupported runtime must never open control state"
        return Connection()

    return connect, calls


@pytest.mark.parametrize("flags", [None, 0, 0x800, 0x200000])
def test_missing_json_or_required_flag_refuses_new_state(tmp_path, monkeypatch, flags):
    connect, calls = runtime_factory(flags)
    monkeypatch.setattr(store.sqlite3, "connect", connect)
    state = tmp_path / "state"
    with pytest.raises(store.StoreError, match="Unsupported SQLite runtime.*innocuous"):
        store.initialize_state(state)
    assert not state.exists()
    assert calls == [":memory:"]


def test_missing_function_introspection_fails_closed(tmp_path, monkeypatch):
    connect, _calls = runtime_factory(None, unavailable=True)
    monkeypatch.setattr(store.sqlite3, "connect", connect)
    database = tmp_path / "pending.sqlite3"
    with pytest.raises(store.StoreError, match="Python version alone is insufficient"):
        store._connect(database)
    assert not database.exists()


@pytest.mark.parametrize("entry", ["backup", "private_backup", "restore", "migrate", "connect"])
def test_incompatible_runtime_cannot_change_existing_state(tmp_path, monkeypatch, entry):
    state = tmp_path / "state"
    store.initialize_state(state)
    backup = tmp_path / "backup"
    recovery.backup_state(state, backup)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    health_calls = []
    monkeypatch.setattr(recovery, "_record_backup_health", lambda *a, **k: health_calls.append(a))
    connect, calls = runtime_factory(0x800)
    monkeypatch.setattr(store.sqlite3, "connect", connect)
    operations = {
        "backup": lambda: recovery.backup_state(state, tmp_path / "new-backup"),
        "private_backup": lambda: recovery._backup_state(state, tmp_path / "new-backup"),
        "restore": lambda: recovery.restore_state(backup, tmp_path / "restored"),
        "migrate": lambda: store.migrate_state(state),
        "connect": lambda: store._connect(state / store.DATABASE_NAME),
    }
    with pytest.raises(store.StoreError, match="Unsupported SQLite runtime"):
        operations[entry]()
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert not (tmp_path / "new-backup").exists()
    assert not (tmp_path / "restored").exists()
    assert health_calls == []
    assert calls == [":memory:"]


def test_success_is_cached_only_for_same_runtime_factory(monkeypatch):
    real_connect = sqlite3.connect
    calls = []

    def supported(path, *args, **kwargs):
        calls.append(path)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(store.sqlite3, "connect", supported)
    store.require_sqlite_runtime()
    store.require_sqlite_runtime()
    assert calls == [":memory:"]
    incompatible, _ = runtime_factory(0x800)
    monkeypatch.setattr(store.sqlite3, "connect", incompatible)
    with pytest.raises(store.StoreError, match="Unsupported SQLite runtime"):
        store.require_sqlite_runtime()


def test_failed_capability_check_is_not_cached(monkeypatch):
    incompatible, calls = runtime_factory(0x800)
    monkeypatch.setattr(store.sqlite3, "connect", incompatible)
    for _ in range(2):
        with pytest.raises(store.StoreError):
            store.require_sqlite_runtime()
    assert calls == [":memory:", ":memory:"]


@pytest.mark.parametrize("command", ["init", "migrate", "backup", "restore", "serve"])
def test_cli_unsupported_runtime_is_concise_and_non_destructive(
    tmp_path: Path, monkeypatch, capsys, command
):
    state = tmp_path / "state"
    if command == "migrate":
        store.initialize_state(state)
    connect, _ = runtime_factory(None)
    monkeypatch.setattr(store.sqlite3, "connect", connect)
    arguments = ["tessera", command, "--state-dir", str(state)]
    if command == "backup":
        arguments += ["--output", str(tmp_path / "backup")]
    elif command == "restore":
        arguments += ["--backup", str(tmp_path / "backup")]
    elif command == "serve":
        arguments += ["--port", "8765"]
    monkeypatch.setattr(sys, "argv", arguments)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith(f"{command} failed: Unsupported SQLite runtime")
    assert "Traceback" not in output.err
    if command != "migrate":
        assert not state.exists()


def test_supported_runtime_keeps_json_uniqueness_and_defensive_read(tmp_path):
    state = tmp_path / "state"
    database, _ = store.initialize_state(state)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        recovery._defensive(connection, recovery.time.monotonic())
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] > 0
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='generated_output_source_identity'"
        ).fetchone()[0]
        assert "CREATE UNIQUE INDEX" in sql
        assert "json_extract" in sql
    finally:
        connection.close()


def test_control_image_pins_and_tools_stay_separate_from_worker():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    base = (
        "python:3.11-slim-trixie@sha256:"
        "e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"
    )
    assert dockerfile.count("FROM " + base) == 2
    remote = (root / "deploy/remote-agent-platform/docker/Dockerfile").read_text()
    assert remote.startswith("FROM " + base + "\n")
    lab, dev = dockerfile.split(" AS dev\n", 1)
    assert "apt-get install" not in lab and "apt-get install" not in remote
    assert "gcc libc6-dev patch" in dev
    assert "!.dockerignore" in (root / ".dockerignore").read_text().splitlines()
