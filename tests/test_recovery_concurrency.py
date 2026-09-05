from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from test_credentials import owner_headers

import tessera_control.recovery as recovery
from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import Store, _connect, canonical_digest, initialize_state, utc_now


class _BackupGate:
    def __init__(
        self, connection: sqlite3.Connection, ready: threading.Event, committed: threading.Event
    ):
        self._connection = connection
        self._ready = ready
        self._committed = committed

    def backup(self, destination, **kwargs):
        self._ready.set()
        assert self._committed.wait(5)
        return self._connection.backup(destination, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_backup_snapshot_is_consistent_while_source_writer_commits(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    initialize_state(source)
    owner = Store(source).authenticate(owner_headers(source)["Authorization"].split()[-1])
    with _connect(source / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA journal_mode=WAL")

    ready = threading.Event()
    committed = threading.Event()
    writer_errors: list[BaseException] = []

    def writer():
        try:
            assert ready.wait(5)
            connection = _connect(source / "tessera.sqlite3")
            try:
                connection.execute("BEGIN IMMEDIATE")
                project_id = "project_after_snapshot"
                connection.execute(
                    "INSERT INTO projects (id,org_id,name,created_at) VALUES (?,?,?,?)",
                    (project_id, owner["org_id"], "After snapshot", utc_now()),
                )
                connection.execute(
                    """INSERT INTO events
                    (org_id,project_id,task_id,event_type,resource_revision,
                     actor_principal_id,created_at,payload_json)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        owner["org_id"],
                        project_id,
                        None,
                        "project.created",
                        1,
                        owner["id"],
                        utc_now(),
                        "{}",
                    ),
                )
                connection.execute("COMMIT")
            finally:
                connection.close()
            committed.set()
        except BaseException as error:
            writer_errors.append(error)
            committed.set()

    original_connect = recovery.sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if args and isinstance(args[0], str) and "mode=ro" in args[0]:
            return _BackupGate(connection, ready, committed)
        return connection

    monkeypatch.setattr(recovery.sqlite3, "connect", connect)
    thread = threading.Thread(target=writer)
    thread.start()
    backup = tmp_path / "backup"
    backup_state(source, backup)
    thread.join(5)
    assert not thread.is_alive() and not writer_errors
    assert {path.name for path in backup.iterdir()} == {"snapshot.sqlite3", "manifest.json"}
    restore_state(backup, tmp_path / "restored")
    with _connect(backup / "snapshot.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT id FROM projects WHERE id='project_after_snapshot'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE project_id='project_after_snapshot'"
            ).fetchone()[0]
            == 0
        )
    with _connect(source / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT id FROM projects WHERE id='project_after_snapshot'"
            ).fetchone()
            is not None
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE project_id='project_after_snapshot'"
            ).fetchone()[0]
            == 1
        )


def test_restore_hold_precedes_cached_idempotency_replay(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    initialize_state(source)
    backup = tmp_path / "backup"
    backup_state(source, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    token = owner_headers(restored)["Authorization"].split()[-1]
    identity = Store(restored).authenticate(token)
    body = {"name": "must not be replayed"}
    digest = canonical_digest(body)
    with _connect(restored / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO idempotency
            (principal_id,org_id,idempotency_key,method,path,request_digest,response_status,response_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                identity["id"],
                identity["org_id"],
                "cached",
                "POST",
                "/projects",
                digest,
                201,
                '{"id":"cached-project","name":"cached"}',
                utc_now(),
            ),
        )
        before = connection.execute("SELECT count(*) FROM projects").fetchone()[0]

    called = False
    original_mutate = Store._mutate

    def mutate(*args, **kwargs):
        nonlocal called
        command = args[5]

        def spy(*command_args, **command_kwargs):
            nonlocal called
            called = True
            return command(*command_args, **command_kwargs)

        return original_mutate(*args[:5], spy, *args[6:], **kwargs)

    monkeypatch.setattr(Store, "_mutate", mutate)
    client = TestClient(create_app(restored))
    response = client.post(
        "/projects", headers=owner_headers(restored) | {"Idempotency-Key": "cached"}, json=body
    )
    assert response.status_code == 403
    assert response.json() != {"id": "cached-project", "name": "cached"}
    assert not called
    with _connect(restored / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == before
