from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.cli import main
from tessera_control.store import initialize_state


def run_cli(monkeypatch, *arguments: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["tessera", *arguments])
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = main()
    return result, stdout.getvalue(), stderr.getvalue()


def test_backup_and_restore_cli_preserve_output_and_create_held_owner(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    initialize_state(source)
    with sqlite3.connect(source / "tessera.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    backup = tmp_path / "backup"

    status, stdout, stderr = run_cli(
        monkeypatch, "backup", "--state-dir", str(source), "--output", str(backup)
    )
    assert status == 0
    assert stderr == ""
    backup_id = stdout.strip()
    assert len(backup_id) == 32
    assert backup_id not in (source / "owner.header").read_text()

    restored = tmp_path / "restored"
    status, stdout, stderr = run_cli(
        monkeypatch, "restore", "--backup", str(backup), "--state-dir", str(restored)
    )
    assert status == 0
    assert stderr == ""
    database_path, header_path = stdout.strip().splitlines()
    assert Path(database_path) == restored / "tessera.sqlite3"
    assert Path(header_path) == restored / "owner.header"
    token = Path(header_path).read_text().strip().split("Bearer ", 1)[1]
    assert token not in stdout
    assert token not in (source / "owner.header").read_text()
    client = TestClient(create_app(restored))
    response = client.get("/recovery", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["state"] == "held_restore"


def test_recovery_cli_expected_failures_are_concise_and_non_destructive(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    initialize_state(source)
    backup = tmp_path / "backup"
    status, _, stderr = run_cli(
        monkeypatch, "backup", "--state-dir", str(source), "--output", str(backup)
    )
    assert status == 0 and stderr == ""

    (backup / "manifest.json").write_text("not json")
    malformed_target = tmp_path / "malformed-target"
    status, stdout, stderr = run_cli(
        monkeypatch, "restore", "--backup", str(backup), "--state-dir", str(malformed_target)
    )
    assert status == 1
    assert stdout == ""
    assert stderr.startswith("restore failed: ")
    assert "Traceback" not in stderr
    assert not malformed_target.exists()

    clean_backup = tmp_path / "clean-backup"
    status, _, _ = run_cli(
        monkeypatch, "backup", "--state-dir", str(source), "--output", str(clean_backup)
    )
    assert status == 0
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("preserve")
    status, stdout, stderr = run_cli(
        monkeypatch, "restore", "--backup", str(clean_backup), "--state-dir", str(existing)
    )
    assert status == 1
    assert stdout == ""
    assert stderr.startswith("restore failed: ")
    assert "Traceback" not in stderr
    assert marker.read_text() == "preserve"

    status, stdout, stderr = run_cli(
        monkeypatch, "backup", "--state-dir", str(source), "--output", str(clean_backup)
    )
    assert status == 1
    assert stdout == ""
    assert stderr.startswith("backup failed: ")
    assert "Traceback" not in stderr
