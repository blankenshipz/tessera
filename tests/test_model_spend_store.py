"""Model-spend ledger storage: units, immutability, migration from schemas 72 to 76 (ADR 0090)."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from tessera_control.migrations import LATEST_SCHEMA_VERSION, MODEL_SPEND_SCHEMA_VERSION
from tessera_control.model_spend import MICRO, to_cents, to_micro
from tessera_control.store import initialize_state, migrate_state

TABLES = ("model_spend_limits", "model_spend_calls")


def test_cents_convert_to_exact_integer_micro_cents():
    assert to_micro(1) == MICRO and to_micro(0.0213) == 21_300
    assert to_micro("0.1") + to_micro("0.2") == to_micro("0.3")  # no float drift
    assert to_micro(1.0000005) == 1_000_001  # half up
    assert to_cents(2_500_000) == 2.5


def _seed(db) -> str:
    org = db.execute("SELECT id FROM organizations").fetchone()[0]
    project = db.execute("SELECT id FROM projects").fetchone()
    if project is None:
        db.execute(
            "INSERT INTO projects VALUES (?,?,?,?)", ("proj_t", org, "t", "2026-01-01T00:00:00Z")
        )
        project = ("proj_t",)
    principal = db.execute("SELECT id FROM principals").fetchone()[0]
    ident = "modelspend_" + uuid.uuid4().hex
    db.execute(
        """INSERT INTO model_spend_calls (id, org_id, project_id, run_id, seq, provider_id, model,
        status, utc_day, reserved_micro, actor_principal_id, created_at)
        VALUES (?,?,?,?,?,?,?,'reserved','2026-01-01',100,?,'2026-01-01T00:00:00Z')""",
        (ident, org, project[0], "run", 1, "anthropic", "m", principal),
    )
    return ident


@pytest.fixture
def db(tmp_path):
    initialize_state(tmp_path / "state")
    connection = sqlite3.connect(tmp_path / "state" / "tessera.sqlite3")
    yield connection
    connection.close()


def test_a_reservation_resolves_once_and_its_identity_never_changes(db):
    ident = _seed(db)
    with pytest.raises(sqlite3.DatabaseError, match="identity"):
        db.execute("UPDATE model_spend_calls SET reserved_micro=1 WHERE id=?", (ident,))
    with pytest.raises(sqlite3.DatabaseError, match="identity"):
        db.execute("UPDATE model_spend_calls SET model='other' WHERE id=?", (ident,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE model_spend_calls SET status='reserved' WHERE id=?", (ident,))
    db.execute(
        """UPDATE model_spend_calls SET status='settled', settled_micro=90,
        receipt_sha256=?, resolved_at='2026-01-01T00:00:01Z' WHERE id=?""",
        ("c" * 64, ident),
    )
    with pytest.raises(sqlite3.DatabaseError, match="cannot change"):
        db.execute("UPDATE model_spend_calls SET status='released' WHERE id=?", (ident,))
    with pytest.raises(sqlite3.DatabaseError, match="permanent"):
        db.execute("DELETE FROM model_spend_calls WHERE id=?", (ident,))


def test_the_run_sequence_and_attempt_are_unique(db):
    _seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        _seed(db)


def test_a_bad_status_or_digest_is_refused(db):
    ident = _seed(db)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            "UPDATE model_spend_calls SET status='settled', settled_micro=1, receipt_sha256='x' "
            "WHERE id=?",
            (ident,),
        )


def test_schema_version_and_migration_from_76_converge_with_a_fresh_database(tmp_path):
    assert MODEL_SPEND_SCHEMA_VERSION == LATEST_SCHEMA_VERSION
    fresh, migrated = tmp_path / "fresh", tmp_path / "migrated"
    initialize_state(fresh)
    initialize_state(migrated)
    with sqlite3.connect(migrated / "tessera.sqlite3") as db:
        for trigger in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='model_spend_calls'"
        ).fetchall():
            db.execute(f"DROP TRIGGER {trigger[0]}")
        for name in reversed(TABLES):
            db.execute(f"DROP TABLE {name}")
        db.execute("UPDATE schema_meta SET schema_version=?", (MODEL_SPEND_SCHEMA_VERSION - 1,))
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION
    with (
        sqlite3.connect(fresh / "tessera.sqlite3") as a,
        sqlite3.connect(migrated / "tessera.sqlite3") as b,
    ):
        query = "SELECT name, sql FROM sqlite_schema WHERE (tbl_name IN (?,?)) ORDER BY name"
        assert a.execute(query, TABLES).fetchall() == b.execute(query, TABLES).fetchall()
        assert (
            b.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION  # idempotent


def test_a_resolved_call_records_who_and_why_and_cannot_change_again(db):
    ident = _seed(db)
    principal = db.execute("SELECT id FROM principals").fetchone()[0]
    db.execute(
        """UPDATE model_spend_calls SET status='released', resolved_at='2026-01-01',
        resolved_by_principal_id=?, resolution='person_released', resolution_reason='never sent'
        WHERE id=?""",
        (principal, ident),
    )
    for statement in (
        "UPDATE model_spend_calls SET resolution_reason='edited' WHERE id=?",
        "UPDATE model_spend_calls SET resolution='person_settled' WHERE id=?",
        "UPDATE model_spend_calls SET status='reserved' WHERE id=?",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(statement, (ident,))


def test_a_resolution_must_be_one_of_the_four_kinds(db):
    ident = _seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE model_spend_calls SET status='released', resolution='bogus' WHERE id=?",
            (ident,),
        )


@pytest.mark.parametrize("version", [74, 75, 76])
def test_a_populated_database_migrates_to_the_ledger_schema_preserving_its_rows(tmp_path, version):
    assert MODEL_SPEND_SCHEMA_VERSION == 77 == LATEST_SCHEMA_VERSION
    fresh, old = tmp_path / "fresh", tmp_path / "old"
    initialize_state(fresh)
    initialize_state(old)
    with sqlite3.connect(old / "tessera.sqlite3") as db:
        _seed(db)  # a real project row and a ledger row, then the ledger is taken away
        project = db.execute("SELECT id,org_id,name FROM projects ORDER BY id").fetchall()
        principals = db.execute("SELECT id FROM principals ORDER BY id").fetchall()
        for trigger in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='model_spend_calls'"
        ).fetchall():
            db.execute(f"DROP TRIGGER {trigger[0]}")
        for name in reversed(TABLES):
            db.execute(f"DROP TABLE {name}")
        db.execute("UPDATE schema_meta SET schema_version=?", (version,))
    assert migrate_state(old) == LATEST_SCHEMA_VERSION
    with (
        sqlite3.connect(fresh / "tessera.sqlite3") as a,
        sqlite3.connect(old / "tessera.sqlite3") as b,
    ):
        assert b.execute("SELECT id,org_id,name FROM projects ORDER BY id").fetchall() == project
        assert b.execute("SELECT id FROM principals ORDER BY id").fetchall() == principals
        query = "SELECT name, sql FROM sqlite_schema WHERE (tbl_name IN (?,?)) ORDER BY name"
        assert a.execute(query, TABLES).fetchall() == b.execute(query, TABLES).fetchall()
        assert b.execute("SELECT count(*) FROM model_spend_calls").fetchone() == (0,)
        assert b.execute("SELECT schema_version FROM schema_meta").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
