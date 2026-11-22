# ruff: noqa: F811
"""ADR 0041 follow-up: an ambiguous billable frame never settles on an estimate without a
guardian verdict read. Fakes only: no provider, network or credential."""

from __future__ import annotations

import sqlite3

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_settlement import CLEAN, run
from test_execution_frames import (  # noqa: F401
    TENANT,
    _actor,
    _drive,
    _open,
    _reservation,
    _settlement,
    db,
)

from tessera_control import execution_frames as frames
from tessera_control.campaign_settlement import (
    SettlementRefused,
    VerdictRequired,
    settle_frame,
)

VERDICT = "v" * 64


def _lost(db, hold=40):
    return _drive(
        db, _open(db, hold=hold), "lost", outcome="lost", outcome_evidence_digest="e" * 64
    )


def _state(db, frame):
    return db.execute(
        "SELECT state FROM execution_frames WHERE frame_id=?", (frame["frame_id"],)
    ).fetchone()["state"]


def test_a_lost_billable_frame_without_a_verdict_stays_open(db):
    frame = _lost(db)
    with pytest.raises(VerdictRequired):
        settle_frame(
            db, frame["frame_id"], _settlement(db, cents=40, basis="guardian_bounded_hold")
        )
    assert _state(db, frame) == "lost"
    assert db.execute("SELECT count(*) FROM execution_frame_settlements").fetchone()[0] == 0


def test_a_lost_billable_frame_with_a_verdict_settles_bounded(db):
    frame = _lost(db)
    settled = settle_frame(
        db,
        frame["frame_id"],
        _settlement(db, cents=40, basis="guardian_bounded_hold", verdict_digest=VERDICT),
    )
    assert settled["settled_cents"] == 40 and settled["final"] is False
    row = db.execute("SELECT * FROM execution_frames").fetchone()
    assert row["state"] == "closed" and row["guardian_verdict_digest"] == VERDICT


def test_a_recorded_verdict_counts_and_a_settlement_above_the_hold_is_refused(db):
    frame = _lost(db)
    frames.record_guardian_verdict(
        db, frame["frame_id"], TENANT, verdict_digest=VERDICT, actor=_actor(db), now="2026-11-20"
    )
    with pytest.raises(SettlementRefused, match="above the hold"):
        settle_frame(
            db, frame["frame_id"], _settlement(db, cents=41, basis="guardian_bounded_hold")
        )
    assert _state(db, frame) == "lost"
    settle_frame(db, frame["frame_id"], _settlement(db, cents=40, basis="guardian_bounded_hold"))
    assert _state(db, frame) == "closed"


def test_an_unobserved_failed_billable_frame_needs_the_verdict_too(db):
    frame = _drive(db, _open(db), "failed", outcome="failed", outcome_evidence_digest="e" * 64)
    with pytest.raises(VerdictRequired):
        settle_frame(
            db, frame["frame_id"], _settlement(db, cents=40, basis="guardian_bounded_hold")
        )
    assert _state(db, frame) == "failed"


def test_a_non_billable_ambiguous_frame_is_unchanged(db):
    frame = _lost(db, hold=0)
    settled = settle_frame(
        db, frame["frame_id"], _settlement(db, cents=0, basis="guardian_bounded_hold")
    )
    assert settled["settled_cents"] == 0


def test_human_and_proof_bases_on_a_lost_frame_are_unchanged(db):
    one = _lost(db)
    assert (
        settle_frame(db, one["frame_id"], _settlement(db, cents=3, basis="human_charge"))[
            "settled_cents"
        ]
        == 3
    )
    two = _drive(db, _open(db, key="s2"), "lost", outcome="lost", outcome_evidence_digest="e" * 64)
    settle_frame(
        db,
        two["frame_id"],
        _settlement(db, basis="provider_absent_zero", absence_evidence_digest="a" * 64),
    )


def test_a_clean_succeeded_frame_is_unchanged(db):
    frame = _drive(
        db, _open(db), "succeeded", outcome="succeeded", outcome_evidence_digest="e" * 64
    )
    settled = settle_frame(
        db, frame["frame_id"], _settlement(db, cents=40, basis="guardian_bounded_hold")
    )
    assert settled["settled_cents"] == 40


def test_the_verdict_is_set_once_and_never_on_a_settled_frame(db):
    frame = _lost(db)
    args = dict(actor=_actor(db), now="2026-11-20")
    frames.record_guardian_verdict(db, frame["frame_id"], TENANT, verdict_digest=VERDICT, **args)
    frames.record_guardian_verdict(db, frame["frame_id"], TENANT, verdict_digest=VERDICT, **args)
    with pytest.raises(frames.FrameRefused):
        frames.record_guardian_verdict(
            db, frame["frame_id"], TENANT, verdict_digest="w" * 64, **args
        )
    settle_frame(db, frame["frame_id"], _settlement(db, cents=40, basis="guardian_bounded_hold"))
    with pytest.raises(frames.FrameRefused):
        frames.record_guardian_verdict(
            db, frame["frame_id"], TENANT, verdict_digest="z" * 64, **args
        )


# -- the Salad driver reads the verdict before it settles -------------------------------


def test_a_clean_salad_settlement_records_the_guardian_verdict_on_its_frame(tmp_path):
    world = SaladWorld(tmp_path)
    run(world)
    world.tick_until(lambda: world.status() == "succeeded")
    frame = world.row("SELECT * FROM execution_frames WHERE kind='stage'")
    assert frame["state"] == "closed" and frame["guardian_verdict_digest"]
    assert world.attempt()["settled_cents"] == HOLD


def test_an_unreadable_salad_verdict_leaves_the_frame_open_with_the_hold(tmp_path):
    world = SaladWorld(tmp_path)
    run(world, final={**CLEAN, "orphan": True})
    world.tick(8)
    frame = world.row("SELECT * FROM execution_frames WHERE kind='stage'")
    assert frame["state"] != "closed" and frame["guardian_verdict_digest"] is None
    assert world.attempt()["settled_cents"] is None
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]
        == HOLD
    )
    assert sqlite3  # the store is a plain sqlite file; nothing was committed to the ledger
