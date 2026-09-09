from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.migrations import MIGRATIONS
from tessera_control.research import create_research_schema
from tessera_control.store import Store, initialize_state, migrate_state

RECORD_TABLES = (
    "research_studies",
    "research_variants",
    "research_sources",
    "research_findings",
    "research_finding_sources",
    "research_finding_variants",
    "research_corrections",
    "interaction_threads",
    "interaction_messages",
    "decision_requests",
)


def _owner_headers(state_dir: Path) -> dict[str, str]:
    name, value = (state_dir / "owner.header").read_text().strip().split(": ", 1)
    return {name: value}


def _v3_state(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    owner = _owner_headers(state)
    project_response = client.post(
        "/projects", headers=owner | {"Idempotency-Key": "project"}, json={"name": "Records"}
    )
    assert project_response.status_code == 201
    project = project_response.json()
    task_response = client.post(
        f"/projects/{project['id']}/tasks",
        headers=owner | {"Idempotency-Key": "task"},
        json={
            "title": "Preserve",
            "purpose": "Verify records migrations retain v3 state.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    assert task_response.status_code == 201
    task = task_response.json()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE execution_result_checkpoints")
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
        for table in reversed(RECORD_TABLES):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version = 3")
        connection.commit()
    return state, project, task


def _table_names(state_dir: Path) -> set[str]:
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_v3_to_v4_requires_explicit_migration_and_preserves_v3_records(tmp_path: Path) -> None:
    state, project, task = _v3_state(tmp_path)
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 18
    assert set(RECORD_TABLES).issubset(_table_names(state))
    client = TestClient(create_app(state))
    owner = _owner_headers(state)
    assert client.get(f"/projects/{project['id']}", headers=owner).json() == project
    assert client.get(f"/tasks/{task['id']}", headers=owner).json()["id"] == task["id"]
    assert migrate_state(state) == 18


def test_v3_to_v4_rolls_back_schema_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, _ = _v3_state(tmp_path)

    def fail_after_research_schema(connection: sqlite3.Connection) -> None:
        create_research_schema(connection)
        raise RuntimeError("injected records migration failure")

    monkeypatch.setitem(MIGRATIONS, 3, fail_after_research_schema)
    with pytest.raises(RuntimeError, match="injected records"):
        migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 3
    assert not set(RECORD_TABLES).intersection(_table_names(state))


def test_fresh_initialization_uses_v4_record_schema(tmp_path: Path) -> None:
    state = tmp_path / "fresh"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 18
    assert set(RECORD_TABLES).issubset(_table_names(state))
