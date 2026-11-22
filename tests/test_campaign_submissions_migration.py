"""Schema step for approved campaign submissions (ADR 0093): a populated database at the
previous version gains the same tables and triggers a fresh one has."""

from __future__ import annotations

import sqlite3

import pytest
from campaign_fixture import Env

from tessera_control.migrations import (
    CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    migrate_database,
)
from tessera_control.store import _connect, validate_database

TABLES = ("campaign_submission_approvals", "campaign_submission_decisions")


def _objects(path) -> dict[str, str]:
    marks = ",".join("?" for _ in TABLES)
    with sqlite3.connect(path) as db:
        return dict(
            db.execute(
                f"SELECT name, sql FROM sqlite_schema WHERE tbl_name IN ({marks})", TABLES
            ).fetchall()
        )


def test_previous_version_migrates_to_the_fresh_schema(tmp_path):
    assert LATEST_SCHEMA_VERSION >= CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION
    env = Env(tmp_path)
    env.submit()  # populated: a campaign row exists
    path = env.state / "tessera.sqlite3"
    fresh = _objects(path)
    assert {
        "campaign_submission_approvals_terms_fixed",
        "campaign_submission_decisions_no_update",
    } <= set(fresh)
    with sqlite3.connect(path) as db:
        for name, sql in fresh.items():
            if sql and sql.startswith("CREATE TRIGGER"):
                db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE campaign_submission_decisions")
        db.execute("DROP TABLE campaign_submission_approvals")
        db.execute(
            "UPDATE schema_meta SET schema_version=?", (CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION - 1,)
        )
    with pytest.raises(ValueError, match="migrate"):
        validate_database(path)
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION  # no-op
    validate_database(path)
    assert _objects(path) == fresh
    # The same triggers as fresh; their behaviour is covered in test_campaign_submissions.
