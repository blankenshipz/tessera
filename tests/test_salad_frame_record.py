"""The guardian's append-only per-frame record and the ``frame_record`` it adds to status
(ADR 0041 section 7). Fakes only: no provider, no network."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

pytest.importorskip("cryptography")
import test_salad_safety_service as base  # noqa: E402
from test_salad_safety_service import _ready_reservation, _rejected  # noqa: E402

from tessera_control.salad_safety_service import (  # noqa: E402
    ABSENT_REASONS,
    CREATE_REJECTED_SETTLE_SECONDS,
    FRAME_VERDICTS,
    frame_verdict,
)

service_case = base.service_case  # the shared fixture, reused as is


def _rows(service, reservation):
    return [
        (r[0], r[1], r[2], json.loads(r[3]) if r[3] else None)
        for r in service.db.execute(
            "SELECT sequence,kind,at,detail FROM frame_records WHERE reservation=? "
            "ORDER BY sequence",
            (reservation,),
        )
    ]


def _kinds(service, reservation):
    return [row[1] for row in _rows(service, reservation)]


def _status(service, body):
    return service.admin(dict(operation="status", reservation=body["reservation"]))


def _expected_digest(rows):
    return hashlib.sha256(
        json.dumps(
            [
                [s, k, a, None if d is None else json.dumps(d, sort_keys=True)]
                for s, k, a, d in rows
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_the_verdict_mapping_is_closed():
    assert frame_verdict("stopped", "x") == "stopped"
    assert frame_verdict("reconciled", "lost_create_absent") == "absent"
    assert frame_verdict("reconciled", "create_rejected_absent") == "absent"
    assert frame_verdict("reconciled", "operator_lost_create_absent") == "absent"
    assert frame_verdict("reconciled", "operator") == "reconciled"
    # A reason only maps to absent by exact code, never by containing the word.
    assert frame_verdict("reconciled", "not_absent_really") == "reconciled"
    assert frame_verdict("reconciled", None) == "reconciled"
    assert ABSENT_REASONS == {
        "lost_create_absent",
        "create_rejected_absent",
        "operator_lost_create_absent",
    }
    for status in ("reserved", "armed", "running", "stopping", "unknown", None):
        assert frame_verdict(status, "lost_create_absent") == "open"
    assert set(FRAME_VERDICTS) == {"open", "stopped", "reconciled", "absent"}


def test_the_record_is_append_only(service_case):
    s, b = service_case[0], service_case[1]
    assert _rows(s, b["reservation"])
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.db.execute("UPDATE frame_records SET kind='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.db.execute("DELETE FROM frame_records")


def test_rows_are_written_at_reserve_bind_ready_stop_and_terminal(service_case):
    s, b, _request, _token, clock, provider = service_case[:6]
    assert _kinds(s, b["reservation"])[:2] == ["reserved", "bound"]
    open_status = _status(s, b)
    record = open_status["frame_record"]
    assert record["verdict"] == "open" and record["reservation"] == b["reservation"]
    s.admin(dict(operation="ready", reservation=b["reservation"]))
    ready = [row for row in _rows(s, b["reservation"]) if row[1] == "ready"]
    assert ready and ready[0][3] == {"create_admitted_at": clock[0]}
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    assert "stop_requested" in _kinds(s, b["reservation"])
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    status = _status(s, b)
    assert status["status"] == "stopped"
    assert status["frame_record"]["verdict"] == "stopped"
    kinds = _kinds(s, b["reservation"])
    assert kinds[-1] == "terminal" and kinds.count("terminal") == 1


def test_the_status_record_digest_covers_every_row_and_is_stable(service_case):
    s, b = service_case[0], service_case[1]
    first = _status(s, b)["frame_record"]
    again = _status(s, b)["frame_record"]
    assert first == again
    rows = _rows(s, b["reservation"])
    assert first["sequence"] == rows[-1][0]
    assert first["record_digest"] == _expected_digest(rows)
    with s.db:
        s._frame_note(b["reservation"], "note", {"n": 1})
    changed = _status(s, b)["frame_record"]
    assert changed["sequence"] > first["sequence"]
    assert changed["record_digest"] != first["record_digest"]
    assert changed["record_digest"] == _expected_digest(_rows(s, b["reservation"]))


def test_the_record_is_additive_to_the_existing_status(service_case):
    s, b = service_case[0], service_case[1]
    status = _status(s, b)
    assert {"status", "reason", "estimated_exposure_cents", "budget_fenced"} <= set(status)
    assert set(status["frame_record"]) == {"reservation", "verdict", "sequence", "record_digest"}


def test_a_rejected_create_that_is_proven_absent_is_an_absent_verdict(tmp_path):
    s, b, clock, _provider = _rejected(tmp_path)
    try:
        assert {"reserved", "ready", "create_rejected"} <= set(_kinds(s, b["reservation"]))
        assert _status(s, b)["frame_record"]["verdict"] == "open"
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS
        s.loop()
        status = _status(s, b)
        assert status["status"] == "reconciled" and status["reason"] == "create_rejected_absent"
        assert status["frame_record"]["verdict"] == "absent"
        kinds = _kinds(s, b["reservation"])
        assert "lost_create_absent" in kinds and kinds[-1] == "terminal"
    finally:
        s.close()


def test_a_reserved_frame_with_no_create_is_open(tmp_path):
    s, b, _clock, _provider = _ready_reservation(tmp_path)
    try:
        record = _status(s, b)["frame_record"]
        assert record["verdict"] == "open"
        assert _kinds(s, b["reservation"]) == ["reserved", "ready"]
    finally:
        s.close()
