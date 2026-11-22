"""Schema 75 widens review_jobs.kind for code reviews and adds code_review_jobs (with
attempt_receipts_json) and code_review_runners (ADR 0092)."""

import sqlite3

from fastapi.testclient import TestClient
from test_code_review_jobs import CRPolicy, cr_job, seed_subject
from test_review_jobs import (  # noqa: F401
    AUTO,
    D,
    Policy,
    _enroll,
    _seed,
    enrol_second_root,
    finding,
    ingested,
    job,
    resolve_body,
    world,
)

from tessera_control.api import create_app
from tessera_control.migrations import CODE_REVIEW_SCHEMA_VERSION, LATEST_SCHEMA_VERSION
from tessera_control.store import initialize_state, migrate_state

OBJECTS = (
    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name LIKE '%review%' ORDER BY name"
)


def _owner(state):
    return {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}


def _downgrade_to_v73(db):
    """Rebuild review_jobs with the ADR 0084 kind CHECK and drop the v75 tables."""
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")  # keep child FKs naming review_jobs
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='review_jobs'").fetchone()[0]
    assert ",'code_review'" in sql
    for action in ("update", "delete"):  # schema 75 (ADR 0092)
        db.execute(f"DROP TRIGGER code_review_sources_no_{action}")
    db.execute("DROP TABLE code_review_sources")
    db.execute("DROP TRIGGER code_review_jobs_no_delete")
    db.execute("DROP TABLE code_review_jobs")
    db.execute("DROP TABLE code_review_runners")
    db.execute("ALTER TABLE review_jobs RENAME TO review_jobs_old")
    db.execute(sql.replace(",'code_review'", ""))
    db.execute("INSERT INTO review_jobs SELECT * FROM review_jobs_old")
    db.execute("DROP TABLE review_jobs_old")
    db.execute("CREATE INDEX review_jobs_project ON review_jobs(org_id,project_id,sequence)")
    db.execute(
        "CREATE TRIGGER review_jobs_no_delete BEFORE DELETE ON review_jobs "
        "BEGIN SELECT RAISE(ABORT,'review jobs are permanent'); END"
    )
    db.execute("UPDATE schema_meta SET schema_version=73")


def test_v73_migrates_to_75_preserving_jobs_findings_and_triggers(tmp_path):
    assert CODE_REVIEW_SCHEMA_VERSION == 75 and LATEST_SCHEMA_VERSION >= CODE_REVIEW_SCHEMA_VERSION
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    state = tmp_path / "old"
    initialize_state(state)
    owner = _owner(state)
    client = TestClient(create_app(state))
    project = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "p"}, json={"name": "R"}
    ).json()["id"]
    submitter = _enroll(client, owner, project, ["review:submit", "review:read"])
    db_path = state / "tessera.sqlite3"
    reviewer_root = enrol_second_root(db_path, owner, client)
    reviewer = _enroll(client, reviewer_root, project, ["review:findings", "review:read"])
    client.app.state.review_policy = Policy()
    _seed(db_path, project, "s1", D)
    seed_subject(db_path, project)
    jid = client.post(
        f"/projects/{project}/review-jobs",
        headers={**submitter, "Idempotency-Key": "j"},
        json=job(),
    ).json()["id"]
    assert (
        client.post(
            f"/review-jobs/{jid}/findings",
            headers={**reviewer, "Idempotency-Key": "f"},
            json={"findings": [finding("a")]},
        ).status_code
        == 201
    )
    with sqlite3.connect(db_path) as db:
        _downgrade_to_v73(db)
        jobs_before = db.execute("SELECT * FROM review_jobs").fetchall()
        reviewer_principal = db.execute(
            "SELECT findings_actor_id FROM review_jobs WHERE id=?", (jid,)
        ).fetchone()[0]
        findings_before = db.execute("SELECT * FROM review_findings").fetchall()
        events_before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(db_path) as db, sqlite3.connect(fresh / "tessera.sqlite3") as ref:
        assert db.execute(OBJECTS).fetchall() == ref.execute(OBJECTS).fetchall()
        assert db.execute("SELECT * FROM review_jobs").fetchall() == jobs_before
        # The principal that supplied findings survives the rebuild, so it still cannot resolve.
        assert reviewer_principal is not None
        assert (
            db.execute("SELECT findings_actor_id FROM review_jobs WHERE id=?", (jid,)).fetchone()[0]
            == reviewer_principal
        )
        assert db.execute("SELECT * FROM review_findings").fetchall() == findings_before
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        columns = {r[1]: r for r in db.execute("PRAGMA table_info(code_review_jobs)")}
        assert columns["attempt_receipts_json"][3] == 1  # NOT NULL
        assert columns["attempt_receipts_json"][4] == "'[]'"
        runners = {r[1] for r in db.execute("PRAGMA table_info(code_review_runners)")}
        assert runners == {"runner_id", "max_running", "seen_at"}
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("DELETE FROM review_jobs")
            raise AssertionError("review jobs must stay permanent")
        except sqlite3.IntegrityError as error:
            assert "permanent" in str(error)
    # The widened CHECK now admits code reviews, and the side table is used.
    client = TestClient(create_app(state))
    client.app.state.review_policy = CRPolicy()
    r = client.post(
        f"/projects/{project}/review-jobs",
        headers={**submitter, "Idempotency-Key": "cr"},
        json=cr_job(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["code_review"]["mode"] == "diff"
    assert r.json()["code_review"]["attempt_receipts"] == []
    # Findings still reference the preserved job id.
    got = client.get(f"/review-jobs/{jid}", headers=reviewer).json()
    assert got["state"] == "completed" and got["code_review"] is None


REVIEW_TABLES = (
    "review_jobs",
    "review_job_approvals",
    "review_findings",
    "review_finding_decisions",
    "enrolled_people",
)


def _dump(db, table):
    cursor = db.execute(f"SELECT * FROM {table} ORDER BY 1")
    names = [d[0] for d in cursor.description]
    return names, [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def test_v74_rebuild_preserves_every_column_and_row(world):  # noqa: F811
    """The kind-CHECK rebuild copies the columns the OLD table has (read from the table), so
    every column of every review table survives with its values, whatever the column list."""
    approved = world.submit(job(tier="fable")).json()["id"]
    body = {"expected_revision": 1, "rationale": "ok to spend"}
    assert world.post(f"/review-jobs/{approved}/approve-tier", world.owner, body).status_code == 200
    first_job, first_finding = ingested(world)
    second_job, _ = ingested(world, [finding("a"), finding("b")])
    assert (
        world.post(
            f"/review-findings/{first_finding['id']}/resolve",
            world.resolver,
            resolve_body(first_finding),
        ).status_code
        == 201
    )
    with sqlite3.connect(world.db) as db:
        _downgrade_to_v73(db)
        db.execute("UPDATE schema_meta SET schema_version=74")
        before = {table: _dump(db, table) for table in REVIEW_TABLES}
        # Real rows exist in every table, and the later review-job columns carry values.
        counts = {table: len(rows) for table, (_, rows) in before.items()}
        assert counts["review_jobs"] == 3 and counts["review_job_approvals"] == 1
        assert counts["review_findings"] == 3 and counts["review_finding_decisions"] == 1
        assert counts["enrolled_people"] >= 1
        jobs = {row["id"]: row for row in before["review_jobs"][1]}
        for name in ("findings_actor_id", "findings_separation_basis", "receipt_digest"):
            assert name in before["review_jobs"][0]
        assert jobs[first_job]["findings_actor_id"] is not None
        assert jobs[second_job]["findings_separation_basis"] == "distinct_person"
        assert jobs[first_job]["actor_id"] is not None and jobs[first_job]["credential_id"]
        assert before["review_finding_decisions"][1][0]["separation_basis"] == "distinct_person"
    assert migrate_state(world.state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(world.db) as db:
        for table in REVIEW_TABLES:
            assert _dump(db, table) == before[table], table
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            "'code_review'"
            in db.execute("SELECT sql FROM sqlite_master WHERE name='review_jobs'").fetchone()[0]
        )
    fresh = world.state.parent / "fresh"
    initialize_state(fresh)
    with sqlite3.connect(world.db) as db, sqlite3.connect(fresh / "tessera.sqlite3") as ref:
        assert db.execute(OBJECTS).fetchall() == ref.execute(OBJECTS).fetchall()
        for table in REVIEW_TABLES:
            assert _dump(db, table)[0] == _dump(ref, table)[0], table
