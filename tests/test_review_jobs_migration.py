"""Schema 73 adds the review-job tables additively and idempotently (ADR 0084); a v72 state
migrates through it to the latest version."""

import sqlite3

from tessera_control.migrations import LATEST_SCHEMA_VERSION, REVIEW_SCHEMA_VERSION
from tessera_control.store import initialize_state, migrate_state

TABLES = (
    "review_jobs",
    "review_job_approvals",
    "review_findings",
    "review_finding_decisions",
    "enrolled_people",
)


def test_v72_migrates_to_73_and_fresh_state_matches(tmp_path):
    assert REVIEW_SCHEMA_VERSION == 73 and LATEST_SCHEMA_VERSION >= REVIEW_SCHEMA_VERSION
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    state = tmp_path / "old"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for table in reversed(TABLES):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE schema_meta SET schema_version=72")
        before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with (
        sqlite3.connect(state / "tessera.sqlite3") as db,
        sqlite3.connect(fresh / "tessera.sqlite3") as ref,
    ):
        query = (
            "SELECT name FROM sqlite_master WHERE name LIKE 'review_%' "
            "OR name='enrolled_people' ORDER BY name"
        )
        assert db.execute(query).fetchall() == ref.execute(query).fetchall()
        assert {r[0] for r in db.execute(query)} >= set(TABLES)
        for table in TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table, column in (
            ("review_finding_decisions", "separation_basis"),
            ("review_jobs", "findings_separation_basis"),
            ("enrolled_people", "renewed_at"),
        ):
            for conn in (db, ref):
                assert column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert (
            db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
