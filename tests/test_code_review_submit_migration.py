"""Schema 81 adds code_review_sources with no-update and no-delete triggers (ADR 0108); a v80
state migrates additively and idempotently, and its existing code reviews have no source."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_code_review_jobs import CRPolicy, cr_job, seed_subject
from test_review_jobs import _enroll

from tessera_control.api import create_app
from tessera_control.migrations import (
    CODE_REVIEW_SCHEMA_VERSION,
    CODE_REVIEW_SUBMIT_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
)
from tessera_control.store import initialize_state, migrate_state

OBJECTS = (
    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name LIKE '%review%' ORDER BY name"
)


def _owner(state):
    return {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}


def _downgrade_to_v80(db):
    for action in ("update", "delete"):
        db.execute(f"DROP TRIGGER code_review_sources_no_{action}")
    db.execute("DROP TABLE code_review_sources")
    db.execute("UPDATE schema_meta SET schema_version=80")


def test_v80_migrates_to_81_preserving_code_reviews(tmp_path):
    assert CODE_REVIEW_SCHEMA_VERSION == 75
    assert CODE_REVIEW_SUBMIT_SCHEMA_VERSION == 81 <= LATEST_SCHEMA_VERSION
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    state = tmp_path / "old"
    initialize_state(state)
    owner = _owner(state)
    client = TestClient(create_app(state))
    client.app.state.review_policy = CRPolicy()
    project = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "p"}, json={"name": "R"}
    ).json()["id"]
    submitter = _enroll(client, owner, project, ["review:submit", "review:read"])
    db_path = state / "tessera.sqlite3"
    seed_subject(db_path, project)
    r = client.post(
        f"/projects/{project}/review-jobs",
        headers={**submitter, "Idempotency-Key": "cr"},
        json=cr_job(),
    )
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    with sqlite3.connect(db_path) as db:
        _downgrade_to_v80(db)
        jobs_before = db.execute("SELECT * FROM review_jobs").fetchall()
        side_before = db.execute("SELECT * FROM code_review_jobs").fetchall()
        events_before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(db_path) as db, sqlite3.connect(fresh / "tessera.sqlite3") as ref:
        assert db.execute(OBJECTS).fetchall() == ref.execute(OBJECTS).fetchall()
        assert db.execute("SELECT * FROM review_jobs").fetchall() == jobs_before
        assert db.execute("SELECT * FROM code_review_jobs").fetchall() == side_before
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
        assert db.execute("SELECT COUNT(*) FROM code_review_sources").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        columns = {r[1] for r in db.execute("PRAGMA table_info(code_review_sources)")}
        assert columns == {
            "job_id",
            "org_id",
            "project_id",
            "repo",
            "branch",
            "head_sha",
            "base_branch",
            "base_sha",
            "focused_tests_json",
            "round",
            "previous_job_id",
            "recheck_findings_json",
            "created_at",
        }
        org = db.execute("SELECT org_id FROM review_jobs WHERE id=?", (jid,)).fetchone()[0]
        row = (jid, org, project, "demo", "b", "1" * 40, "main", "2" * 40, "[]", 4, None, "[]", "t")
        with pytest.raises(sqlite3.IntegrityError):  # round is 1..3
            db.execute("INSERT INTO code_review_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        db.execute(
            "INSERT INTO code_review_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*row[:9], 1, *row[10:]),
        )
        for statement in (
            "UPDATE code_review_sources SET round=2",
            "DELETE FROM code_review_sources",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)
        db.rollback()
    # The pre-existing code review reads with no source.
    client = TestClient(create_app(state))
    got = client.get(f"/review-jobs/{jid}", headers=submitter).json()
    assert got["code_review"]["source"] is None and got["code_review"]["review_receipt"] is None
