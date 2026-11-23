from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from tessera_control.recovery import (
    BACKUP_DATABASE,
    BACKUP_MANIFEST,
    backup_state,
    verify_restorable,
)
from tessera_control.store import initialize_state


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_in_place_verification_never_writes_wal_files_into_a_backup(tmp_path: Path) -> None:
    state = tmp_path / "source"
    initialize_state(state)
    backup = tmp_path / "backup"
    backup_state(state, backup)
    database = backup / BACKUP_DATABASE
    # A legacy bundle whose database header says WAL, with no -wal/-shm beside it.
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    assert database.read_bytes()[18:20] == b"\x02\x02"
    # Re-seal the manifest to the modified database so only the sqlite open is under test.
    manifest_path = backup / BACKUP_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    data = database.read_bytes()
    manifest["size"] = len(data)
    manifest["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    before = _tree(backup)

    verify_restorable(backup)

    assert _tree(backup) == before
    assert not list(backup.glob("*-wal")) and not list(backup.glob("*-shm"))
