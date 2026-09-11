from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tessera_control.migrations import MIGRATIONS
from tessera_control.snapshots import create_snapshot_schema
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state


def _owner(state_dir: Path) -> dict[str, object]:
    token = (state_dir / "owner.header").read_text().strip().split(": ", 1)[1]
    return Store(state_dir).authenticate(token.removeprefix("Bearer "))


def _v4_state(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
    state = tmp_path / "state"
    initialize_state(state)
    store = Store(state)
    owner = _owner(state)
    _, project = store.create_project(
        owner, "project", canonical_digest({"name": "Preserved"}), {"name": "Preserved"}
    )
    _, task = store.create_task(
        owner,
        project["id"],
        "task",
        canonical_digest({"title": "Keep", "purpose": "preserve", "budget": 0}),
        {
            "title": "Keep",
            "purpose": "preserve",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE execution_result_checkpoints")
        connection.execute("DROP TABLE execution_grant_skills")
        connection.execute("DROP TABLE skill_releases")
        connection.execute("DROP TABLE skill_snapshots")
        connection.execute("DROP TABLE artifact_dispositions")
        connection.execute("DROP TABLE artifact_manifests")
        connection.execute("DROP TABLE artifact_uploads")
        connection.execute("DROP TABLE artifacts")
        connection.execute("DROP TABLE execution_observations")
        connection.execute("DROP TABLE execution_leases")
        connection.execute("DROP TABLE execution_grants")
        connection.execute("DROP TABLE execution_task_epochs")
        connection.execute("DROP TABLE execution_project_limits")
        connection.execute("DROP TABLE snapshot_releases")
        connection.execute("DROP TABLE snapshot_files")
        connection.execute("DROP TABLE source_snapshots")
        connection.execute("UPDATE schema_meta SET schema_version = 4")
        connection.commit()
    return state, project, task


def _tables(state: Path) -> set[str]:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_v4_to_v5_requires_explicit_migration_and_preserves_records(tmp_path: Path) -> None:
    state, project, task = _v4_state(tmp_path)
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 32
    assert {"source_snapshots", "snapshot_files"}.issubset(_tables(state))
    store = Store(state)
    owner = _owner(state)
    assert store.get_project(owner, project["id"]) == project
    assert store.get_task(owner, task["id"])["id"] == task["id"]
    assert migrate_state(state) == 32


def test_v4_to_v5_rolls_back_snapshot_schema_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, _ = _v4_state(tmp_path)

    def fail_after_snapshot_schema(connection: sqlite3.Connection) -> None:
        create_snapshot_schema(connection)
        raise RuntimeError("injected snapshot migration failure")

    monkeypatch.setitem(MIGRATIONS, 4, fail_after_snapshot_schema)
    with pytest.raises(RuntimeError, match="injected snapshot"):
        migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 4
    assert not {"source_snapshots", "snapshot_files"}.intersection(_tables(state))
