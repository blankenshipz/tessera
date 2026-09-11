from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_finding_reaffirmation import _fixture, _run

from tessera_control.migrations import MIGRATIONS, migrate_v11_to_v12, migrate_v12_to_v13
from tessera_control.recovery import backup_state, restore_state
from tessera_control.research import ResearchStore
from tessera_control.store import Store, migrate_state

_TABLES = ("research_findings", "research_finding_sources", "research_finding_variants")


def _legacy(tmp_path: Path, version: int = 11):
    store, owner, finding, _, _ = _fixture(tmp_path)
    state = store.database_path.parent
    before = store.export_study(owner, finding["study_id"])
    with sqlite3.connect(store.database_path) as connection:
        # Build the actual previous CHECK constraint, rather than only changing
        # the version marker of a current database.
        schema = """CREATE TABLE research_findings (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            statement TEXT NOT NULL, status TEXT NOT NULL CHECK
            (status IN ('candidate','accepted','needs_review')),
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            accepted_by_principal_id TEXT REFERENCES principals(id))"""
        if version == 12:
            schema = schema.replace("'needs_review'))", "'needs_review','retracted'))")
        connection.execute("DROP TABLE execution_result_checkpoints")
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
            columns = ",".join(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
            connection.execute(f"INSERT INTO {table} SELECT {columns} FROM old_{table}")
        connection.execute("UPDATE schema_meta SET schema_version=?", (version,))
    return state, owner, finding, before


def test_v12_preserves_finding_history_and_supports_held_restore(tmp_path: Path) -> None:
    state, owner, finding, before = _legacy(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_findings SET status='retracted'")
    with pytest.raises(ValueError, match="explicit"):
        Store(state)
    assert migrate_state(state) == 32
    assert migrate_state(state) == 32
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


@pytest.mark.parametrize("fail", [False, True])
def test_v13_migration_preserves_v12_records_and_rolls_back(
    tmp_path: Path, monkeypatch, fail: bool
) -> None:
    state, owner, finding, before = _legacy(tmp_path, version=12)
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        snapshot = list(connection.iterdump())
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_findings SET status='superseded'")
    if fail:

        def injected(connection):
            migrate_v12_to_v13(connection)
            raise RuntimeError("injected version migration failure")

        monkeypatch.setitem(MIGRATIONS, 12, injected)
        with pytest.raises(RuntimeError, match="injected"):
            migrate_state(state)
        with sqlite3.connect(database) as connection:
            assert list(connection.iterdump()) == snapshot
        return
    assert migrate_state(state) == 32
    store = ResearchStore(state)
    assert store.export_study(owner, finding["study_id"]) == before
    result = _run(
        store,
        owner,
        store.revise_finding,
        finding["id"],
        body={
            "expected_revision": 3,
            "kind": "narrow",
            "reason": "Synthetic narrowed interpretation",
            "statement": "A narrower synthetic statement",
            "source_ids": finding["source_ids"],
            "variant_ids": finding["variant_ids"],
        },
    )
    assert result["original"]["status"] == "superseded"
    assert result["replacement"]["supersedes_finding_id"] == finding["id"]
    backup_state(state, tmp_path / "backup13")
    restore_state(tmp_path / "backup13", tmp_path / "restored13")
    restored = ResearchStore(tmp_path / "restored13")
    token = (tmp_path / "restored13" / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    assert restored.export_study(
        restored.authenticate(token), finding["study_id"]
    ) == store.export_study(owner, finding["study_id"])
