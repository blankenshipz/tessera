from __future__ import annotations

import stat
from pathlib import Path

from fastapi.testclient import TestClient
from test_credentials import owner_headers

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import initialize_state


def test_online_backup_restore_holds_writes_but_keeps_reads(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_state(source)
    client = TestClient(create_app(source))
    project = client.post(
        "/projects",
        headers=owner_headers(source) | {"Idempotency-Key": "project"},
        json={"name": "Preserved project"},
    )
    assert project.status_code == 201
    backup = tmp_path / "backup"
    manifest = backup_state(source, backup)
    assert set(entry.name for entry in backup.iterdir()) == {"manifest.json", "snapshot.sqlite3"}
    assert manifest["schema_version"] == 43
    restored = tmp_path / "restored"
    database, header = restore_state(backup, restored)
    assert database.name == "tessera.sqlite3" and header.stat().st_mode & 0o777 == 0o600
    restored_client = TestClient(create_app(restored))
    status = restored_client.get("/recovery", headers=owner_headers(restored))
    assert status.status_code == 200
    assert status.json()["state"] == "held_restore"
    assert status.json()["recovery_hold"] is True
    assert status.json()["original_branch_fenced"] is False
    assert status.json()["unknown_post_backup_activity"] is True
    assert restored_client.get("/projects", headers=owner_headers(restored)).json()["projects"] == [
        project.json()
    ]
    body = {"name": "must remain read-only"}
    headers = owner_headers(restored) | {"Idempotency-Key": "held"}
    assert restored_client.post("/projects", headers=headers, json=body).status_code == 403
    assert restored_client.post("/projects", headers=headers, json=body).status_code == 403
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700


def test_restore_rejects_tampered_backup_and_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_state(source)
    backup = tmp_path / "backup"
    backup_state(source, backup)
    (backup / "snapshot.sqlite3").write_bytes(b"tampered")
    target = tmp_path / "target"
    try:
        restore_state(backup, target)
    except Exception as error:
        assert "Backup" in str(error)
    else:
        raise AssertionError("tampered backup was restored")
    clean_backup = tmp_path / "clean-backup"
    backup_state(source, clean_backup)
    target.mkdir(exist_ok=True)
    try:
        restore_state(clean_backup, target)
    except Exception as error:
        assert "destination" in str(error)
    else:
        raise AssertionError("existing destination was restored")
