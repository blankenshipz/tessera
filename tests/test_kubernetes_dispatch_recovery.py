"""Historical v43 dispatch ledger recovery at current55; synthetic local resources."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import assert_empty_runtime_v55
from test_kubernetes_dispatch import _dispatch, _owner_headers
from test_kubernetes_execution import _admitted_package, _cluster, _old_v35_state

from tessera_control.api import create_app
from tessera_control.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
from tessera_control.recovery import (
    BACKUP_DATABASE,
    BACKUP_MANIFEST,
    DATABASE_NAME,
    RESTORE_MARKER,
    backup_state,
    restore_state,
)
from tessera_control.store import StoreError, initialize_state, migrate_state
from tessera_worker.kubernetes_contracts import cluster_identity_digest

_LEDGER_TABLES = (
    "external_operations",
    "execution_runtime_packages",
    "kubernetes_dispatches",
)


def _ledger_rows(state: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(state / DATABASE_NAME) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in _LEDGER_TABLES
        }


def _refresh_manifest(backup: Path) -> None:
    snapshot = backup / BACKUP_DATABASE
    manifest_path = backup / BACKUP_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        size=snapshot.stat().st_size,
        sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    )
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("migrated", (False, True), ids=("fresh-v43", "migrated-v35"))
def test_dispatch_ledger_backup_held_restore_preserves_data_and_remains_repeatable(
    tmp_path: Path, migrated: bool
) -> None:
    response, _worker, _package = _admitted_package(tmp_path, migrate_from_v35=migrated)
    state = tmp_path / "state"
    assert LATEST_SCHEMA_VERSION == 55
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION

    with sqlite3.connect(state / DATABASE_NAME) as connection:
        assert_empty_runtime_v55(connection)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' "
            "AND name='kubernetes_dispatches_by_worker'"
        ).fetchone() == ("kubernetes_dispatches_by_worker",)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' "
            "AND name='kubernetes_dispatches_fence_cancel'"
        ).fetchone() == ("kubernetes_dispatches_fence_cancel",)

    client = TestClient(create_app(state))
    dispatch = _dispatch(client, state, response, cluster_identity_digest(_cluster()))
    revoked = client.post(
        f"/execution-grants/{response['package']['grant_id']}/revoke",
        json={"expected_revision": 1},
        headers=_owner_headers(state) | {"Idempotency-Key": "cancel-dispatch-ledger"},
    )
    assert revoked.status_code == 200
    with sqlite3.connect(state / DATABASE_NAME) as connection:
        assert connection.execute(
            "SELECT state FROM kubernetes_dispatches WHERE id=?", (dispatch["id"],)
        ).fetchone() == ("cancel_requested",)
    before = _ledger_rows(state)
    assert len(before["execution_runtime_packages"]) == len(before["kubernetes_dispatches"]) == 1

    backup = tmp_path / "backup"
    assert backup_state(state, backup)["schema_version"] == LATEST_SCHEMA_VERSION
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    assert _ledger_rows(restored) == before
    with sqlite3.connect(restored / DATABASE_NAME) as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 55
        assert_empty_runtime_v55(connection)

    held = TestClient(create_app(restored))
    assert held.get("/health/ready").status_code == 503
    assert (
        held.post(
            f"/experiment-admitted-attempts/{response['admitted_attempt_id']}/dispatch",
            json={
                "expected_revision": 1,
                "target_digest": cluster_identity_digest(_cluster()),
                "profile_version": _cluster().profile,
            },
            headers=_owner_headers(restored) | {"Idempotency-Key": "held-dispatch"},
        ).status_code
        == 403
    )
    assert (
        held.post(
            f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
            json={
                "target_digest": cluster_identity_digest(_cluster()),
                "profile_version": _cluster().profile,
            },
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "revoked-worker"},
        ).status_code
        == 401
    )

    repeated_backup = tmp_path / "repeated-backup"
    held_database_bytes = (restored / DATABASE_NAME).read_bytes()
    backup_state(restored, repeated_backup)
    # Backing up a held restore copies its immutable audit history; it must not
    # append backup-health state to the held source as a side effect.
    assert (restored / DATABASE_NAME).read_bytes() == held_database_bytes
    repeatedly_restored = tmp_path / "repeatedly-restored"
    restore_state(repeated_backup, repeatedly_restored)
    assert _ledger_rows(repeatedly_restored) == before
    with sqlite3.connect(repeatedly_restored / DATABASE_NAME) as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 55
        assert_empty_runtime_v55(connection)


def test_v35_fixture_failed_upgrade_rolls_back_schema_and_prior_authority(tmp_path, monkeypatch):
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    assert (
        client.post(
            "/projects",
            json={"name": "Prior retained authority history"},
            headers=_owner_headers(state) | {"Idempotency-Key": "retained-project"},
        ).status_code
        == 201
    )
    before = _old_v35_state(state)
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    actual_v55 = MIGRATIONS[54]

    def fail_after_additive_tables(connection):
        actual_v55(connection)
        raise sqlite3.OperationalError("synthetic post-v55 migration failure")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 54, fail_after_additive_tables)
        with pytest.raises(sqlite3.OperationalError, match="post-v55"):
            migrate_state(state)
    with sqlite3.connect(state / DATABASE_NAME) as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 35
        for table in (
            "runtime_policy_profiles",
            "kubernetes_dispatches",
            "execution_runtime_packages",
        ):
            assert (
                connection.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (table,)).fetchone()
                is None
            )
        assert all(
            connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows
            for table, rows in before.items()
        )
    assert migrate_state(state) == 55
    assert migrate_state(state) == 55
    with sqlite3.connect(state / DATABASE_NAME) as connection:
        assert_empty_runtime_v55(connection)
        assert all(
            connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows
            for table, rows in before.items()
        )


def test_held_restore_rejects_an_altered_dispatch_cancellation_trigger(tmp_path: Path) -> None:
    response, _worker, _package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    client = TestClient(create_app(state))
    _dispatch(client, state, response, cluster_identity_digest(_cluster()))
    backup = tmp_path / "backup"
    backup_state(state, backup)
    with sqlite3.connect(backup / BACKUP_DATABASE) as connection:
        connection.execute("DROP TRIGGER kubernetes_dispatches_fence_cancel")
        connection.execute(
            """CREATE TRIGGER kubernetes_dispatches_fence_cancel
            AFTER UPDATE OF desired_state ON external_operations
            WHEN NEW.desired_state='cancel_requested'
            BEGIN
              UPDATE kubernetes_dispatches SET state='claimed',updated_at=NEW.updated_at
              WHERE operation_id=NEW.operation_id;
            END"""
        )
    _refresh_manifest(backup)

    restored = tmp_path / "restored"
    with pytest.raises(StoreError, match="schema|tables"):
        restore_state(backup, restored)
    assert not (restored / DATABASE_NAME).exists()
    assert not (restored / RESTORE_MARKER).exists()
