from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_credentials import create_project, owner_headers

import tessera_control.recovery as recovery
from tessera_control.api import create_app
from tessera_control.migrations import MIGRATIONS
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import StoreError, _connect, initialize_state, migrate_state


def _backup(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "source"
    initialize_state(state)
    backup = tmp_path / "backup"
    backup_state(state, backup)
    return state, backup


@pytest.mark.parametrize(
    "change",
    (
        {"event_watermark": True},
        {"size": True},
        {"event_watermark": 99},
        {"extra": "rejected"},
    ),
)
def test_restore_rejects_strict_manifest_metadata(
    tmp_path: Path, change: dict[str, object]
) -> None:
    _state, backup = _backup(tmp_path)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) | change
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(StoreError):
        restore_state(backup, tmp_path / "restored")


def test_restore_fails_closed_without_completion_marker_and_never_replaces(tmp_path: Path) -> None:
    _state, backup = _backup(tmp_path)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    (restored / ".restore-complete").unlink()
    with pytest.raises(ValueError, match="completely published"):
        create_app(restored)
    with pytest.raises(StoreError, match="destination"):
        restore_state(backup, restored)


def test_backup_is_a_consistent_point_while_source_continues(tmp_path: Path) -> None:
    state, backup = _backup(tmp_path)
    source = TestClient(create_app(state))
    after = create_project(source, state, "After backup", "after-backup")
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    restored_client = TestClient(create_app(restored))
    assert (
        after
        not in restored_client.get("/projects", headers=owner_headers(restored)).json()["projects"]
    )
    status = restored_client.get("/recovery", headers=owner_headers(restored)).json()
    assert status["unknown_post_backup_activity"] is True
    # The fresh restore owner is authenticated, but no mutation or replay is admitted.
    body = {"name": "held"}
    headers = owner_headers(restored) | {"Idempotency-Key": "held-replay"}
    assert (
        source.post(
            "/projects",
            headers=owner_headers(state) | {"Idempotency-Key": "source-live"},
            json=body,
        ).status_code
        == 201
    )
    assert restored_client.post("/projects", headers=headers, json=body).status_code == 403
    assert restored_client.post("/projects", headers=headers, json=body).status_code == 403


def test_restore_detects_backup_replacement_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, backup = _backup(tmp_path)
    original = recovery._read_manifest

    def validate_then_replace(path: Path):
        manifest = original(path)
        (path / "snapshot.sqlite3").write_bytes(b"changed")
        return manifest

    monkeypatch.setattr(recovery, "_read_manifest", validate_then_replace)
    with pytest.raises(StoreError):
        restore_state(backup, tmp_path / "restored")


def test_v10_to_v11_migration_rolls_back_transactionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE control_recovery")
        connection.execute("UPDATE schema_meta SET schema_version=10")

    def fail(connection: sqlite3.Connection) -> None:
        from tessera_control.recovery import create_recovery_schema

        create_recovery_schema(connection)
        raise RuntimeError("recovery migration fault")

    monkeypatch.setitem(MIGRATIONS, 10, fail)
    with pytest.raises(RuntimeError, match="recovery migration"):
        migrate_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 10
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='control_recovery'"
            ).fetchone()
            is None
        )
