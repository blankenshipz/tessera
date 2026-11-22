"""Schema 79: databases that migrated through schema 73 while its tables were still being
edited in place converge to the fresh shapes, so backup/restore validation accepts them."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tessera_control import recovery
from tessera_control.migrations import (
    LATEST_SCHEMA_VERSION,
    REVIEW_PARITY_SCHEMA_VERSION,
    migrate_database,
)
from tessera_control.store import Store, StoreError, initialize_state, migrate_state

# --- Old DDL, as first deployed (db003aa, 34388b8 = f792a85, 31f8871) -------------------------

_KIND = "('plan_review','dataset_scout','technique_scout','pre_gate_review','code_review')"


def _review_jobs(extra_columns: str) -> str:
    return f"""CREATE TABLE review_jobs (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        kind TEXT NOT NULL CHECK(kind IN {_KIND}),
        title TEXT NOT NULL, inputs_json TEXT NOT NULL,
        prompt_digest TEXT NOT NULL CHECK(length(prompt_digest)=64),
        destination_id TEXT NOT NULL,
        tier TEXT NOT NULL CHECK(tier IN ('haiku','sonnet','opus','fable')),
        budget_cents INTEGER NOT NULL CHECK(budget_cents BETWEEN 0 AND 10000),
        egress TEXT NOT NULL CHECK(egress IN ('none','model_runtime')),
        state TEXT NOT NULL CHECK(state IN ('awaiting_approval','queued','running','completed',
            'failed','not_started','cancelled')),
        reason TEXT, revision INTEGER NOT NULL CHECK(revision>=1),
        digest TEXT NOT NULL CHECK(length(digest)=64),
        receipt_digest TEXT, review_md_digest TEXT,{extra_columns}
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


_APPROVALS = """CREATE TABLE review_job_approvals (
        job_id TEXT PRIMARY KEY REFERENCES review_jobs(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        rationale TEXT NOT NULL, job_digest TEXT NOT NULL, created_at TEXT NOT NULL)"""
_FINDINGS = """CREATE TABLE review_findings (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES review_jobs(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        finding_key TEXT NOT NULL, finding_json TEXT NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        state TEXT NOT NULL CHECK(state IN ('open','accepted','rejected')),
        revision INTEGER NOT NULL CHECK(revision>=1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(job_id, finding_key))"""
_DECISIONS_OLD = """CREATE TABLE review_finding_decisions (
        finding_id TEXT PRIMARY KEY REFERENCES review_findings(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        credential_kind TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('accept','reject')),
        rationale TEXT NOT NULL, finding_digest TEXT NOT NULL,
        job_digest TEXT NOT NULL, subject_digests_json TEXT NOT NULL, created_at TEXT NOT NULL)"""
_DECISIONS_NEW = (
    _DECISIONS_OLD[:-1]
    + """,
        separation_basis TEXT NOT NULL CHECK(separation_basis IN
            ('distinct_person','single_owner_opt_in')))"""
)

_INDEXES = (
    "CREATE INDEX review_jobs_project ON review_jobs(org_id,project_id,sequence)",
    "CREATE INDEX review_findings_project ON review_findings(org_id,project_id,sequence)",
)


def _triggers() -> tuple[str, ...]:
    result = []
    for table in ("review_job_approvals", "review_finding_decisions"):
        for action in ("UPDATE", "DELETE"):
            result.append(
                f"CREATE TRIGGER {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN "
                "SELECT RAISE(ABORT,'review decisions are immutable'); END"
            )
    result.append(
        "CREATE TRIGGER review_findings_no_delete BEFORE DELETE ON review_findings "
        "BEGIN SELECT RAISE(ABORT,'review findings are permanent'); END"
    )
    result.append(
        "CREATE TRIGGER review_jobs_no_delete BEFORE DELETE ON review_jobs "
        "BEGIN SELECT RAISE(ABORT,'review jobs are permanent'); END"
    )
    result.append(
        "CREATE TRIGGER review_findings_content_fixed "
        "BEFORE UPDATE ON review_findings "
        "WHEN NEW.finding_json IS NOT OLD.finding_json OR NEW.digest IS NOT OLD.digest "
        "OR NEW.job_id IS NOT OLD.job_id BEGIN "
        "SELECT RAISE(ABORT,'review finding content is immutable'); END"
    )
    return tuple(result)


# name -> (review_jobs extra columns, decisions DDL, has enrolled_people)
SHAPES = {
    # db003aa: first schema 73
    "first_73": ("", _DECISIONS_OLD, False),
    # 34388b8 / f792a85 (the schema-73 merge): findings_actor_id added
    "f792a85": (" findings_actor_id TEXT,", _DECISIONS_OLD, False),
    # 31f8871: person-level separation, before people enrolment
    "separation_no_people": (
        " findings_actor_id TEXT, findings_separation_basis TEXT,",
        _DECISIONS_NEW,
        False,
    ),
}


def _make_legacy(state: Path, shape: str) -> Path:
    extra, decisions, people = SHAPES[shape]
    initialize_state(state)
    path = state / "tessera.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for table in (
            "review_finding_decisions",
            "review_findings",
            "review_job_approvals",
            "review_jobs",
        ):
            db.execute(f"DROP TABLE {table}")  # takes its indexes and triggers with it
        if not people:
            db.execute("DROP TABLE enrolled_people")
        for ddl in (_review_jobs(extra), _APPROVALS, _FINDINGS, decisions, *_INDEXES):
            db.execute(ddl)
        for ddl in _triggers():
            db.execute(ddl)
        db.execute("UPDATE schema_meta SET schema_version=77")
        db.execute("COMMIT")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _shapes(path: Path):
    with closing(sqlite3.connect(path)) as db:
        return recovery._table_shapes(db)[:2]


def _fresh_shapes(tmp_path: Path):
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    return _shapes(fresh / "tessera.sqlite3")


def _seed_rows(path: Path, *, decision: bool) -> None:
    with closing(sqlite3.connect(path)) as db:
        org = db.execute("SELECT id FROM organizations").fetchone()[0]
        principal = db.execute("SELECT id FROM principals").fetchone()[0]
        credential = db.execute("SELECT id FROM credentials").fetchone()[0]
        db.execute("INSERT INTO projects VALUES ('p1',?,'P','2026-01-01T00:00:00Z')", (org,))
        columns = [r[1] for r in db.execute("PRAGMA table_info(review_jobs)")]
        values = {
            "id": "job1",
            "org_id": org,
            "project_id": "p1",
            "credential_id": credential,
            "actor_id": principal,
            "kind": "plan_review",
            "title": "T",
            "inputs_json": "[]",
            "prompt_digest": "a" * 64,
            "destination_id": "d",
            "tier": "haiku",
            "budget_cents": 1,
            "egress": "none",
            "state": "completed",
            "revision": 1,
            "digest": "b" * 64,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        present = [c for c in columns if c in values]
        db.execute(
            f"INSERT INTO review_jobs ({','.join(present)}) "
            f"VALUES ({','.join('?' * len(present))})",
            [values[c] for c in present],
        )
        db.execute(
            "INSERT INTO review_findings (id,job_id,org_id,project_id,finding_key,finding_json,"
            "digest,state,revision,created_at,updated_at) VALUES "
            "('f1','job1',?,'p1','k','{}',?, 'open',1,'t','t')",
            (org, "c" * 64),
        )
        if decision:
            db.execute(
                "INSERT INTO review_finding_decisions VALUES "
                "('f1',?,?,'owner','accept','why','d','d','[]','t')",
                (org, principal),
            )
        db.commit()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_legacy_shape_differs_then_converges_and_backup_restore_passes(tmp_path, shape):
    assert REVIEW_PARITY_SCHEMA_VERSION == 80 <= LATEST_SCHEMA_VERSION
    fresh = _fresh_shapes(tmp_path)
    state = tmp_path / "old"
    path = _make_legacy(state, shape)
    assert _shapes(path) != fresh  # the legacy helper really reproduces the drift
    with pytest.raises(StoreError, match="does not match supported control tables"):
        recovery._validate_snapshot(path)

    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert _shapes(path) == fresh
    assert migrate_state(state) == LATEST_SCHEMA_VERSION  # idempotent
    assert _shapes(path) == fresh

    backup = tmp_path / "backup"
    assert recovery.backup_state(state, backup)["schema_version"] == LATEST_SCHEMA_VERSION
    recovery.restore_state(backup, tmp_path / "held")


def test_rows_are_preserved_when_there_are_no_decisions(tmp_path):
    state = tmp_path / "old"
    path = _make_legacy(state, "first_73")
    _seed_rows(path, decision=False)
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT id,findings_actor_id FROM review_jobs").fetchall() == [
            ("job1", None)
        ]
        assert db.execute("SELECT id FROM review_findings").fetchall() == [("f1",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _shapes(path) == _fresh_shapes(tmp_path)
    recovery._validate_snapshot(path)


def test_rebuilt_decisions_table_keeps_immutability_triggers(tmp_path):
    state = tmp_path / "old"
    path = _make_legacy(state, "f792a85")
    _seed_rows(path, decision=False)
    migrate_state(state)
    with closing(sqlite3.connect(path)) as db:
        notnull = {r[1]: r[3] for r in db.execute("PRAGMA table_info(review_finding_decisions)")}
        assert notnull["separation_basis"] == 1
        org = db.execute("SELECT id FROM organizations").fetchone()[0]
        actor = db.execute("SELECT id FROM principals").fetchone()[0]
        db.execute(
            "INSERT INTO review_finding_decisions VALUES "
            "('f1',?,?,'k','accept','r','d','d','[]','t','distinct_person')",
            (org, actor),
        )
        for statement in (
            "UPDATE review_finding_decisions SET rationale='x'",
            "DELETE FROM review_finding_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)


def _decision_rows(path: Path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM review_finding_decisions")]


@pytest.mark.parametrize("shape", ["first_73", "f792a85"])
def test_existing_decisions_survive_as_legacy_unrecorded(tmp_path, shape):
    state = tmp_path / "old"
    path = _make_legacy(state, shape)
    _seed_rows(path, decision=True)
    with closing(sqlite3.connect(path)) as db:
        old = [
            dict(
                zip(
                    [c[1] for c in db.execute("PRAGMA table_info(review_finding_decisions)")],
                    r,
                    strict=True,
                )
            )
            for r in db.execute("SELECT * FROM review_finding_decisions")
        ]
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    rows = _decision_rows(path)
    assert len(rows) == len(old) == 1
    assert rows[0].pop("separation_basis") == "legacy_unrecorded"
    assert rows[0] == old[0]  # ids, digests, timestamps and every other column unchanged
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not db.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name='_review_decisions_old'"
        ).fetchall()
        for statement in (
            "UPDATE review_finding_decisions SET rationale='x'",
            "DELETE FROM review_finding_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)
    assert _shapes(path) == _fresh_shapes(tmp_path)
    assert migrate_state(state) == LATEST_SCHEMA_VERSION  # idempotent
    assert _decision_rows(path)[0]["separation_basis"] == "legacy_unrecorded"
    recovery._validate_snapshot(path)


def test_finding_read_reports_legacy_unrecorded(tmp_path):
    from tessera_control.review_jobs import ReviewJobStore

    state = tmp_path / "old"
    path = _make_legacy(state, "f792a85")
    _seed_rows(path, decision=True)
    migrate_state(state)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM review_findings WHERE id='f1'").fetchone()
        record = ReviewJobStore._finding_record(db, row)
    assert record["separation_basis"] == "legacy_unrecorded"
    assert record["decision"]["separation_basis"] == "legacy_unrecorded"


def test_unverifiable_copy_error_is_actionable(tmp_path, monkeypatch):
    from tessera_control import migrations

    state = tmp_path / "old"
    path = _make_legacy(state, "f792a85")
    _seed_rows(path, decision=True)
    real = migrations.__dict__["_rebuild_review_decisions"]

    def lossy(connection, create_schema):
        def drop_then_create(c):
            create_schema(c)
            c.execute("DROP TRIGGER review_finding_decisions_no_delete")
            c.execute(
                "CREATE TRIGGER lose_rows AFTER INSERT ON review_finding_decisions "
                "BEGIN DELETE FROM review_finding_decisions; END"
            )

        real(connection, drop_then_create)

    monkeypatch.setattr(migrations, "_rebuild_review_decisions", lossy)
    with pytest.raises(StoreError, match="File-copy the whole state directory"):
        migrate_database(path, _connect)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 77
        assert db.execute("SELECT count(*) FROM review_finding_decisions").fetchone()[0] == 1


def test_application_never_writes_legacy_unrecorded():
    """Only the DDL CHECK names the value: a new decision records one of the two real bases."""
    import re

    from tessera_control import review_jobs

    source = Path(review_jobs.__file__).read_text()
    assert source.count("legacy_unrecorded") == 1
    assert re.search(
        r"IN\s*\('distinct_person','single_owner_opt_in','legacy_unrecorded'\)", source
    )


def test_decisions_with_basis_are_kept_when_only_people_are_missing(tmp_path):
    state = tmp_path / "old"
    path = _make_legacy(state, "separation_no_people")
    _seed_rows(path, decision=False)
    with closing(sqlite3.connect(path)) as db:
        org = db.execute("SELECT id FROM organizations").fetchone()[0]
        actor = db.execute("SELECT id FROM principals").fetchone()[0]
        db.execute(
            "INSERT INTO review_finding_decisions VALUES "
            "('f1',?,?,'k','accept','r','d','d','[]','t','distinct_person')",
            (org, actor),
        )
        db.commit()
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT separation_basis FROM review_finding_decisions").fetchall() == [
            ("distinct_person",)
        ]
    assert _shapes(path) == _fresh_shapes(tmp_path)


def test_fresh_state_is_unchanged_by_migration(tmp_path):
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    assert migrate_state(fresh) == LATEST_SCHEMA_VERSION
    assert Store(fresh) is not None
