"""Execution frames (ADR 0041): state machine, guard triggers, tenant scope, settle_frame.

Fresh control state, synthetic timestamps, no network. Ledger-free frames (no campaign
attempt) exercise settle_frame alone; the runner wiring has its own tests.
"""

from __future__ import annotations

import sqlite3

import pytest

from tessera_control import execution_frames as frames
from tessera_control.campaign_settlement import (
    FrameSettlement,
    SettlementRefused,
    settle_attempt,
    settle_frame,
)
from tessera_control.store import Conflict, NotFound, _connect, canonical_digest, initialize_state

NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-01T00:00:05Z"


_PRINCIPALS: dict[int, str] = {}


@pytest.fixture
def db(tmp_path):
    database, _ = initialize_state(tmp_path / "state")
    connection = _connect(database)
    principal = connection.execute("SELECT id FROM principals LIMIT 1").fetchone()["id"]
    for org, project in (("org_local", "project_a"), ("org_local", "project_b")):
        connection.execute(
            "INSERT INTO projects (id, org_id, name, created_at) VALUES (?,?,?,?)",
            (project, org, project, NOW),
        )
    _PRINCIPALS[id(connection)] = principal
    yield connection
    _PRINCIPALS.pop(id(connection), None)
    connection.close()


TENANT = frames.Tenant("org_local", "project_a")
OTHER = frames.Tenant("org_local", "project_b")


def _actor(db) -> frames.Actor:
    return frames.Actor("runner", _PRINCIPALS[id(db)])


def _open(db, key: str = "s1", attempt: int = 1, hold: int = 40) -> dict:
    frame_id = frames.stage_frame_id(frames.campaign_frame_id("campaign_x"), key, attempt)
    return frames.open_frame(
        db,
        frame_id=frame_id,
        tenant=TENANT,
        kind="stage",
        provider_id="fake-provider",
        idempotency_key="tessera-frame-" + frame_id[:32],
        hold_cents=hold,
        actor=_actor(db),
        now=NOW,
        parent_frame_id=frames.campaign_frame_id("campaign_x"),
    )


def _to(db, frame: dict, state: str, **fields) -> dict:
    return frames.transition(
        db,
        frame["frame_id"],
        TENANT,
        to_state=state,
        expected_revision=frame["revision"],
        trigger="test",
        actor=_actor(db),
        now=NOW,
        fields=fields or None,
    )


def _drive(db, frame: dict, state: str, **fields) -> dict:
    return frames.drive_to(
        db,
        frame["frame_id"],
        TENANT,
        state,
        trigger="test",
        actor=_actor(db),
        now=NOW,
        fields=fields or None,
    )


def _settlement(db, *, cents=0, basis="unclaimed_zero", digest="d1", **extra) -> FrameSettlement:
    return FrameSettlement(
        tenant=extra.pop("tenant", TENANT),
        cents=cents,
        basis=basis,
        request_digest=digest,
        now=LATER,
        actor=_actor(db),
        **extra,
    )


# -- identity and the state machine ------------------------------------------------------


def test_frame_id_is_deterministic_and_open_is_idempotent(db):
    parent = frames.campaign_frame_id("campaign_x")
    assert frames.stage_frame_id(parent, "s1", 1) == canonical_digest([parent, "s1", 1])
    first = _open(db)
    assert first["state"] == "requested" and first["revision"] == 1
    assert _open(db)["frame_id"] == first["frame_id"]
    with pytest.raises(Conflict):
        _open(db, hold=41)  # same id, different identity
    assert frames.stage_frame_id(parent, "s1", 2) != first["frame_id"]


def test_the_full_happy_path_writes_one_event_per_revision(db):
    frame = _open(db)
    for state in ("validated", "reserved", "creating", "running", "succeeded"):
        frame = _to(db, frame, state)
    assert frame["state"] == "succeeded" and frame["revision"] == 6
    events = frames.frame_events(db, frame["frame_id"], TENANT)
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        (None, "requested"),
        ("requested", "validated"),
        ("validated", "reserved"),
        ("reserved", "creating"),
        ("creating", "running"),
        ("running", "succeeded"),
    ]
    assert [e["revision"] for e in events] == [1, 2, 3, 4, 5, 6]
    ordinary = db.execute(
        "SELECT count(*) FROM events WHERE event_type='frame.state_changed'"
    ).fetchone()[0]
    assert ordinary == 6


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("requested", "reserved"),
        ("reserved", "running"),
        ("reserved", "lost"),
        ("creating", "succeeded"),
        ("creating", "cancelled"),
        ("running", "rejected"),
        ("succeeded", "closed"),
        ("failed", "running"),
    ],
)
def test_illegal_transitions_are_refused_in_code_and_by_the_trigger(db, before, after):
    frame = _drive(db, _open(db), before)
    with pytest.raises(frames.FrameRefused):
        _to(db, frame, after)
    # The pair check, or (for an unsettled close) the settlement guard, refuses it.
    with pytest.raises(sqlite3.IntegrityError, match="illegal frame transition|settlement"):
        db.execute(
            "UPDATE execution_frames SET state=?, revision=revision+1 WHERE frame_id=?",
            (after, frame["frame_id"]),
        )
    assert frames.get_frame(db, frame["frame_id"], TENANT)["state"] == before


def test_every_adr_pair_is_legal_and_nothing_else():
    assert frames.legal("reserved", "cancelled") and frames.legal("creating", "lost")
    for terminal in frames.TERMINAL_STATES:
        assert frames.legal(terminal, "settling")
    assert not any(frames.legal("closed", state) for state in frames.STATES)
    assert len(frames.TRANSITIONS) == 18


def test_revision_is_compare_and_swap(db):
    frame = _to(db, _open(db), "validated")
    with pytest.raises(Conflict):
        frames.transition(
            db,
            frame["frame_id"],
            TENANT,
            to_state="reserved",
            expected_revision=1,
            trigger="stale",
            actor=_actor(db),
            now=NOW,
        )
    with pytest.raises(sqlite3.IntegrityError, match="revision must advance"):
        db.execute(
            "UPDATE execution_frames SET next_resolve_at=? WHERE frame_id=?",
            (LATER, frame["frame_id"]),
        )


def test_identity_is_write_once_and_set_once_fields_never_change(db):
    frame = _drive(db, _open(db), "creating", create_intent_id="grant_1")
    for column, value in (("hold_cents", 99), ("org_id", "org_other"), ("provider_id", "x")):
        with pytest.raises(sqlite3.IntegrityError, match="write-once"):
            db.execute(
                f"UPDATE execution_frames SET {column}=?, revision=revision+1 WHERE frame_id=?",
                (value, frame["frame_id"]),
            )
    with pytest.raises(sqlite3.IntegrityError, match="set-once"):
        db.execute(
            "UPDATE execution_frames SET create_intent_id='grant_2', revision=revision+1 "
            "WHERE frame_id=?",
            (frame["frame_id"],),
        )
    with pytest.raises(frames.FrameRefused, match="already set"):
        _to(db, frame, "running", create_intent_id="grant_2")


def test_a_frame_starts_requested_and_is_never_deleted(db):
    frame = _open(db)
    with pytest.raises(sqlite3.IntegrityError, match="starts requested"):
        db.execute(
            """INSERT INTO execution_frames (frame_id, kind, org_id, project_id, provider_id,
            idempotency_key, hold_cents, created_at, state, revision, updated_at)
            VALUES ('f2','stage','org_local','project_a','p','k',0,?, 'running', 1, ?)""",
            (NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        db.execute("DELETE FROM execution_frames WHERE frame_id=?", (frame["frame_id"],))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE execution_frame_events SET trigger='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM execution_frame_events")


def test_closing_needs_a_settlement_and_a_closed_row_is_final(db):
    frame = _drive(db, _open(db), "settling")
    with pytest.raises(sqlite3.IntegrityError, match="settlement and receipt"):
        db.execute(
            "UPDATE execution_frames SET state='closed', revision=revision+1 WHERE frame_id=?",
            (frame["frame_id"],),
        )
    frame = _drive(db, _open(db, key="s2"), "cancelled")
    settle_frame(db, frame["frame_id"], _settlement(db))
    closed = frames.get_frame(db, frame["frame_id"], TENANT)
    assert closed["state"] == "closed" and closed["receipt_digest"] and closed["settlement_id"]
    with pytest.raises(sqlite3.IntegrityError, match="closed frame is final"):
        db.execute(
            "UPDATE execution_frames SET attempts=attempts+1, revision=revision+1 WHERE frame_id=?",
            (frame["frame_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE execution_frame_settlements SET cents=1")


def test_ambiguity_is_a_flag_with_a_reason(db):
    frame = _drive(db, _open(db), "creating")
    flagged = frames.flag_ambiguous(
        db,
        frame["frame_id"],
        TENANT,
        reason="create_unanswered",
        expected_revision=frame["revision"],
        actor=_actor(db),
        now=NOW,
    )
    assert flagged["state"] == "creating" and flagged["ambiguous_reason"] == "create_unanswered"
    # Setting the same reason again writes nothing.
    again = frames.flag_ambiguous(
        db,
        frame["frame_id"],
        TENANT,
        reason="create_unanswered",
        expected_revision=flagged["revision"],
        actor=_actor(db),
        now=NOW,
    )
    assert again["revision"] == flagged["revision"]
    # The outcome resolves the ambiguity; a lost frame keeps it.
    lost = _to(db, flagged, "lost")
    assert lost["ambiguous_reason"] == "create_unanswered"
    reserved = _drive(db, _open(db, key="s2"), "reserved")
    with pytest.raises(frames.FrameRefused):
        frames.flag_ambiguous(
            db,
            reserved["frame_id"],
            TENANT,
            reason="observed_unknown",
            expected_revision=reserved["revision"],
            actor=_actor(db),
            now=NOW,
        )
    unregistered = frames.flag_ambiguous(
        db,
        reserved["frame_id"],
        TENANT,
        reason=frames.PROVIDER_UNREGISTERED,
        expected_revision=reserved["revision"],
        actor=_actor(db),
        now=NOW,
    )
    assert unregistered["ambiguous_reason"] == "provider_unregistered"


def test_tenant_isolation_at_every_access(db):
    frame = _drive(db, _open(db), "cancelled")
    with pytest.raises(NotFound):
        frames.get_frame(db, frame["frame_id"], OTHER)
    with pytest.raises(NotFound):
        frames.frame_events(db, frame["frame_id"], OTHER)
    with pytest.raises(NotFound):
        frames.transition(
            db,
            frame["frame_id"],
            OTHER,
            to_state="settling",
            expected_revision=frame["revision"],
            trigger="t",
            actor=_actor(db),
            now=NOW,
        )
    with pytest.raises(NotFound):
        settle_frame(db, frame["frame_id"], _settlement(db, tenant=OTHER))
    assert frames.get_frame(db, frame["frame_id"], TENANT)["state"] == "cancelled"


# -- settle_frame ------------------------------------------------------------------------


def test_double_settle_is_a_no_op_and_a_different_digest_conflicts(db):
    frame = _drive(db, _open(db), "cancelled")
    first = settle_frame(db, frame["frame_id"], _settlement(db))
    assert first["replayed"] is False and first["settled_cents"] == 0
    events = db.execute("SELECT count(*) FROM execution_frame_events").fetchone()[0]
    again = settle_frame(db, frame["frame_id"], _settlement(db))
    assert again["replayed"] is True and again["receipt_digest"] == first["receipt_digest"]
    assert db.execute("SELECT count(*) FROM execution_frame_events").fetchone()[0] == events
    with pytest.raises(Conflict):
        settle_frame(db, frame["frame_id"], _settlement(db, digest="d2"))
    kinds = [
        row[0]
        for row in db.execute(
            "SELECT event_type FROM events WHERE event_type IN ('frame.settled','frame.closed')"
        )
    ]
    assert kinds == ["frame.settled", "frame.closed"]


def test_only_a_terminal_frame_settles(db):
    frame = _drive(db, _open(db), "running")
    with pytest.raises(frames.FrameRefused):
        settle_frame(db, frame["frame_id"], _settlement(db))


def test_settle_refuses_unknown_bases_and_nonzero_zero_bases(db):
    frame = _drive(db, _open(db), "failed")
    with pytest.raises(SettlementRefused):
        settle_frame(db, frame["frame_id"], _settlement(db, basis="made_up"))
    with pytest.raises(SettlementRefused):
        settle_frame(db, frame["frame_id"], _settlement(db, cents=5, basis="provider_absent_zero"))
    with pytest.raises(SettlementRefused, match="final"):
        settle_frame(
            db,
            frame["frame_id"],
            _settlement(db, cents=5, basis="guardian_bounded_hold", final=True),
        )
    assert frames.get_frame(db, frame["frame_id"], TENANT)["state"] == "failed"


def test_provider_absent_zero_needs_absence_evidence(db):
    lost = _drive(db, _open(db), "lost", create_intent_id="intent_1")
    with pytest.raises(SettlementRefused, match="absence"):
        settle_frame(db, lost["frame_id"], _settlement(db, basis="provider_absent_zero"))
    assert frames.get_frame(db, lost["frame_id"], TENANT)["state"] == "lost"
    settled = settle_frame(
        db,
        lost["frame_id"],
        _settlement(db, basis="provider_absent_zero", absence_evidence_digest="a" * 64),
    )
    assert settled["final"] is True and settled["settlement_basis"] == "provider_absent_zero"
    # A frame cancelled before any create intent needs no reads.
    unsent = _drive(db, _open(db, key="s2"), "cancelled", outcome="cancelled")
    assert (
        settle_frame(db, unsent["frame_id"], _settlement(db, basis="provider_absent_zero"))[
            "settled_cents"
        ]
        == 0
    )


def test_settle_attempt_refuses_the_absence_basis():
    with pytest.raises(SettlementRefused, match="settle_frame"):
        settle_attempt(
            None, "a1", cents=0, basis="provider_absent_zero", request_digest="d", now=NOW
        )


# -- experiment hold releases ------------------------------------------------------------


def _reservation(db, admitted: str = "admitted_1", held: int = 40) -> None:
    db.execute("PRAGMA foreign_keys = OFF")  # the experiment graph itself is not under test
    db.execute(
        "INSERT INTO experiment_budget_reservations VALUES (?,?,?,?,?,?,0,1,1)",
        (admitted, "experiment_1", "grant_" + admitted, 1, held, held),
    )


def test_a_zero_final_basis_without_evidence_releases_nothing(db):
    _reservation(db)
    frame = _drive(db, _open(db), "cancelled", admitted_attempt_id="admitted_1")
    settle_frame(db, frame["frame_id"], _settlement(db))
    assert db.execute("SELECT count(*) FROM experiment_budget_releases").fetchone()[0] == 0


def test_a_zero_final_basis_releases_the_experiment_hold_once(db):
    _reservation(db)
    frame = _drive(
        db,
        _open(db),
        "cancelled",
        admitted_attempt_id="admitted_1",
        outcome="cancelled",
        outcome_evidence_digest="e" * 64,
    )
    settle_frame(db, frame["frame_id"], _settlement(db, evidence={"note": "synthetic"}))
    settle_frame(db, frame["frame_id"], _settlement(db))  # replay: no second release
    rows = db.execute("SELECT * FROM experiment_budget_releases").fetchall()
    assert len(rows) == 1
    assert (rows[0]["admitted_attempt_id"], rows[0]["basis"], rows[0]["released_cents"]) == (
        "admitted_1",
        "unclaimed_zero",
        40,
    )
    assert rows[0]["evidence_digest"]
    # The reservation itself is never edited.
    held = db.execute("SELECT held_cents FROM experiment_budget_reservations").fetchone()[0]
    assert held == 40
    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        db.execute("UPDATE experiment_budget_releases SET released_cents=0")
    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        db.execute("DELETE FROM experiment_budget_releases")


@pytest.mark.parametrize(("basis", "cents"), [("guardian_bounded_hold", 40), ("human_charge", 3)])
def test_no_release_on_non_zero_or_non_final_bases(db, basis, cents):
    _reservation(db)
    frame = _drive(
        db,
        _open(db),
        "failed",
        admitted_attempt_id="admitted_1",
        outcome="failed",
        outcome_evidence_digest="e" * 64,
    )
    # A bounded hold on a failed billable frame needs the guardian verdict read (ADR 0041).
    verdict = {"verdict_digest": "v" * 64} if basis == "guardian_bounded_hold" else {}
    settled = settle_frame(
        db, frame["frame_id"], _settlement(db, cents=cents, basis=basis, **verdict)
    )
    assert settled["final"] is (basis != "guardian_bounded_hold")
    assert db.execute("SELECT count(*) FROM experiment_budget_releases").fetchone()[0] == 0


def test_a_released_hold_stops_counting_in_budget_status(db):
    from tessera_control.experiment_budgets import release_hold

    _reservation(db)
    assert (
        release_hold(
            db,
            admitted_attempt_id="admitted_1",
            frame_id="f1",
            basis="local_zero",
            evidence_digest="e",
            now=NOW,
        )["released_cents"]
        == 40
    )
    with pytest.raises(Conflict):
        release_hold(
            db,
            admitted_attempt_id="admitted_1",
            frame_id="f2",
            basis="local_zero",
            evidence_digest="e",
            now=NOW,
        )
    held = db.execute(
        "SELECT CASE WHEN rel.admitted_attempt_id IS NULL THEN r.held_cents ELSE 0 END "
        "FROM experiment_budget_reservations r LEFT JOIN experiment_budget_releases rel "
        "ON rel.admitted_attempt_id=r.admitted_attempt_id"
    ).fetchone()[0]
    assert held == 0
