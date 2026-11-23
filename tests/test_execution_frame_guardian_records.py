"""What control read from the guardian's per-frame record (ADR 0041 section 7, schema 87).

Fresh control state, synthetic timestamps, no network. The read is recorded; it settles nothing.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import test_execution_frames as base
from test_execution_frames import LATER, NOW, OTHER, TENANT, _actor, _open, _settlement, _to

from tessera_control import campaign_settlement
from tessera_control import execution_frames as frames
from tessera_control.migrations import (
    GUARDIAN_RECORD_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
)
from tessera_control.store import NotFound

db = base.db  # the shared fresh-state fixture, reused as is
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _frame(db, **kwargs):
    frame = _open(db)
    frame = _to(db, frame, "validated")
    return _to(db, frame, "reserved", guardian_reservation_id="reservation_1")


def _read(db, frame, **over):
    arguments = dict(verdict="stopped", record_sequence=4, record_digest=DIGEST_A, now=LATER)
    return frames.record_guardian_read(db, frame["frame_id"], TENANT, **(arguments | over))


def test_a_read_is_recorded_with_its_reservation_and_state_is_unchanged(db):
    frame = _frame(db)
    row = _read(db, frame)
    assert (row["verdict"], row["record_sequence"], row["record_digest"]) == (
        "stopped",
        4,
        DIGEST_A,
    )
    assert row["guardian_reservation_id"] == "reservation_1" and row["read_at"] == LATER
    assert (row["org_id"], row["project_id"]) == (TENANT.org_id, TENANT.project_id)
    assert frames.get_frame(db, frame["frame_id"], TENANT)["revision"] == frame["revision"]
    assert frames.guardian_reads(db, frame["frame_id"], TENANT) == [row]


def test_the_same_read_is_idempotent_and_a_new_one_appends(db):
    frame = _frame(db)
    first = _read(db, frame)
    assert _read(db, frame) == first
    second = _read(db, frame, record_sequence=5, record_digest=DIGEST_B, verdict="reconciled")
    assert second["sequence"] > first["sequence"]
    assert [r["record_sequence"] for r in frames.guardian_reads(db, frame["frame_id"], TENANT)] == [
        4,
        5,
    ]


def test_the_table_is_append_only(db):
    frame = _frame(db)
    _read(db, frame)
    for statement in (
        "UPDATE execution_frame_guardian_records SET verdict='open'",
        "DELETE FROM execution_frame_guardian_records",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(statement)


def test_a_read_is_tenant_scoped(db):
    frame = _frame(db)
    with pytest.raises(NotFound):
        frames.record_guardian_read(
            db,
            frame["frame_id"],
            OTHER,
            verdict="stopped",
            record_sequence=1,
            record_digest=DIGEST_A,
            now=LATER,
        )
    _read(db, frame)
    with pytest.raises(NotFound):
        frames.guardian_reads(db, frame["frame_id"], OTHER)


@pytest.mark.parametrize(
    "over",
    [
        {"verdict": "running"},
        {"record_sequence": -1},
        {"record_sequence": True},
        {"record_digest": "A" * 64},
        {"record_digest": "abc"},
    ],
)
def test_a_malformed_read_is_refused_and_nothing_is_written(db, over):
    frame = _frame(db)
    with pytest.raises((frames.FrameRefused, sqlite3.DatabaseError)):
        _read(db, frame, **over)
    assert frames.guardian_reads(db, frame["frame_id"], TENANT) == []


def test_a_frame_with_no_guardian_reservation_takes_no_read(db):
    frame = _open(db)
    with pytest.raises(frames.FrameRefused, match="guardian reservation"):
        _read(db, frame)


def test_the_latest_read_for_an_attempt_is_a_receipt_section(db):
    frame = frames.open_frame(
        db,
        frame_id="f" * 64,
        tenant=TENANT,
        kind="stage",
        provider_id="fake-provider",
        idempotency_key="key",
        hold_cents=10,
        actor=_actor(db),
        now=NOW,
        campaign_attempt_id="attempt_1",
    )
    frame = _to(db, frame, "validated")
    frame = _to(db, frame, "reserved", guardian_reservation_id="reservation_1")
    assert frames.latest_guardian_record(db, "attempt_1") is None
    _read(db, frame, verdict="open", record_sequence=2)
    _read(db, frame, verdict="stopped", record_sequence=6, record_digest=DIGEST_B)
    assert frames.latest_guardian_record(db, "attempt_1") == {
        "verdict": "stopped",
        "record_sequence": 6,
        "record_digest": DIGEST_B,
        "verdict_source": "guardian_read",
    }
    assert frames.latest_guardian_record(db, "attempt_unknown") is None


def test_the_frame_receipt_carries_the_read_only_when_there_is_one(db):
    frame = _frame(db)
    shape = dict(
        cents=0,
        basis="unclaimed_zero",
        final=True,
        request_digest="d",
        evidence={},
        estimate_cents=None,
    )
    assert "guardian_record" not in frames.build_receipt(frame, **shape)
    record = {"verdict": "stopped", "record_sequence": 3, "record_digest": DIGEST_A}
    assert frames.build_receipt(frame, guardian_record=record, **shape)["guardian_record"] == record


def test_settle_frame_puts_the_read_on_the_stored_receipt(db, monkeypatch):
    monkeypatch.setattr(campaign_settlement, "_settle_attempt", lambda *a, **k: None)
    frame = frames.open_frame(
        db,
        frame_id="e" * 64,
        tenant=TENANT,
        kind="stage",
        provider_id="fake-provider",
        idempotency_key="key2",
        hold_cents=10,
        actor=_actor(db),
        now=NOW,
        campaign_attempt_id="attempt_2",
    )
    for state in ("validated", "reserved", "creating", "running", "succeeded"):
        fields = {"guardian_reservation_id": "reservation_2"} if state == "reserved" else None
        frame = frames.transition(
            db,
            frame["frame_id"],
            TENANT,
            to_state=state,
            expected_revision=frame["revision"],
            trigger="test",
            actor=_actor(db),
            now=NOW,
            fields=fields,
        )
    _read(db, frame, verdict="stopped", record_sequence=9, record_digest=DIGEST_B)
    campaign_settlement.settle_frame(db, frame["frame_id"], _settlement(db))
    stored = db.execute(
        "SELECT receipt_json FROM execution_frame_settlements WHERE frame_id=?",
        (frame["frame_id"],),
    ).fetchone()[0]
    assert json.loads(stored)["guardian_record"] == {
        "verdict": "stopped",
        "record_sequence": 9,
        "record_digest": DIGEST_B,
        "verdict_source": "guardian_read",
    }


def test_the_migration_is_registered_and_adds_the_table_to_an_older_database(tmp_path):
    from tessera_control.store import _connect, initialize_state

    assert GUARDIAN_RECORD_SCHEMA_VERSION == 87 == LATEST_SCHEMA_VERSION
    assert MIGRATIONS[GUARDIAN_RECORD_SCHEMA_VERSION - 1]
    database, _ = initialize_state(tmp_path / "state")
    connection = _connect(database)
    try:
        for trigger in ("update", "delete"):
            connection.execute(f"DROP TRIGGER execution_frame_guardian_records_no_{trigger}")
        connection.execute("DROP TABLE execution_frame_guardian_records")
        connection.execute(
            "UPDATE schema_meta SET schema_version=?", (GUARDIAN_RECORD_SCHEMA_VERSION - 1,)
        )
        connection.commit()
        MIGRATIONS[GUARDIAN_RECORD_SCHEMA_VERSION - 1](connection)
        assert (
            connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == GUARDIAN_RECORD_SCHEMA_VERSION
        )
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "execution_frame_guardian_records" in names
        assert "execution_frame_guardian_records_no_update" in names
    finally:
        connection.close()
