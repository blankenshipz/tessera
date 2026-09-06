from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_finding_reaffirmation import _fixture, _run

from tessera_control.migrations import MIGRATIONS, migrate_v11_to_v12
from tessera_control.recovery import backup_state, restore_state
from tessera_control.research import ResearchStore
from tessera_control.store import Store, migrate_state

_TABLES = ("research_findings", "research_finding_sources", "research_finding_variants")


def _legacy(tmp_path: Path):
    store, owner, finding, _, _ = _fixture(tmp_path)
    state = store.database_path.parent
    before = store.export_study(owner, finding["study_id"])
    with sqlite3.connect(store.database_path) as connection:
        # Build the actual previous CHECK constraint, rather than only changing
        # the version marker of a current database.
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='research_findings'"
        ).fetchone()[0]
        schema = schema.replace(", 'retracted'", "").replace(",'retracted'", "")
        for table in _TABLES:
            connection.execute(f"CREATE TEMP TABLE old_{table} AS SELECT * FROM {table}")
        for table in reversed(_TABLES):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(schema)
        connection.execute(
            "CREATE INDEX research_findings_study ON research_findings(study_id, created_at, id)"
        )
        for suffix, target, column in (
            ("sources", "research_sources", "source_id"),
            ("variants", "research_variants", "variant_id"),
        ):
            connection.execute(
                f"CREATE TABLE research_finding_{suffix} ("
                "finding_id TEXT NOT NULL REFERENCES research_findings(id),"
                f"{column} TEXT NOT NULL REFERENCES {target}(id),"
                f"PRIMARY KEY(finding_id, {column}))"
            )
        for table in _TABLES:
            connection.execute(f"INSERT INTO {table} SELECT * FROM old_{table}")
        connection.execute("UPDATE schema_meta SET schema_version=11")
    return state, owner, finding, before


def test_v12_preserves_finding_history_and_supports_held_restore(tmp_path: Path) -> None:
    state, owner, finding, before = _legacy(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_findings SET status='retracted'")
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 12
    assert migrate_state(state) == 12
    store = ResearchStore(state)
    assert store.export_study(owner, finding["study_id"]) == before
    retracted = _run(
        store,
        owner,
        store.retract_finding,
        finding["id"],
        body={"expected_revision": 3, "reason": "Synthetic migration review"},
    )
    assert retracted["status"] == "retracted"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backup_state(state, tmp_path / "backup")
    restore_state(tmp_path / "backup", tmp_path / "restored")
    restored = ResearchStore(tmp_path / "restored")
    token = (tmp_path / "restored" / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    assert restored.export_study(restored.authenticate(token), finding["study_id"]) == (
        store.export_study(owner, finding["study_id"])
    )


def test_v12_migration_failure_rolls_back_tables_and_version(tmp_path: Path, monkeypatch) -> None:
    state, _, _, _ = _legacy(tmp_path)
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        before = list(connection.iterdump())

    def fail(connection):
        migrate_v11_to_v12(connection)
        raise RuntimeError("injected migration publication failure")

    monkeypatch.setitem(MIGRATIONS, 11, fail)
    with pytest.raises(RuntimeError, match="injected"):
        migrate_state(state)
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
