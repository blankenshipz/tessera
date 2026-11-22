"""An attempt whose exit was reported is held while its outputs are still uploading (ADR 0064)."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World  # noqa: F401 - imported for the fixtures below
from test_campaign_exited_resolve import (
    PAST,
    _end_lease,
    _exit_without_receipt,
    _later,
    _world,
)

from tessera_control import artifact_limits as limits_module
from tessera_control import campaign_exited_resolve as cer
from tessera_control import snapshot_chunks

CHUNK = 16
DATA = bytes(range(256)) * 2  # 512 bytes: 32 chunks of 16


@pytest.fixture(autouse=True)
def _tiny(monkeypatch):
    limits_module.operator_limits.cache_clear()
    monkeypatch.setattr(limits_module, "LEGACY_INLINE_BYTES", 100)
    monkeypatch.setattr(snapshot_chunks, "CHUNK_BYTES", CHUNK)
    yield
    limits_module.operator_limits.cache_clear()


def _begin_upload(world, reach, gid):
    lease = world.row("SELECT * FROM execution_leases WHERE grant_id=?", gid)
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", gid)
    started = reach(
        f"/worker/leases/{lease['id']}/large-artifact-uploads",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": operation["operation_id"],
            "relative_path": "data.json",
            "sha256": hashlib.sha256(DATA).hexdigest(),
            "size": len(DATA),
        },
        "begin-large",
    )
    return lease, started["id"]


def _put(reach, lease, upload_id, seq):
    part = DATA[seq * CHUNK : (seq + 1) * CHUNK]
    return reach(
        f"/worker/leases/{lease['id']}/large-artifact-uploads/{upload_id}/chunks/{seq}",
        {
            "content_base64": base64.b64encode(part).decode(),
            "sha256": hashlib.sha256(part).hexdigest(),
        },
        f"put-{seq}",
    )


def _setup(tmp_path, *, grant_expired=True):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    reach = world.channel.post  # the worker's own reports are lost; this is the route itself
    attempt = _exit_without_receipt(world, campaign["id"])
    gid = attempt["grant_id"]
    lease, upload_id = _begin_upload(world, reach, gid)
    _end_lease(world, gid)
    if grant_expired:
        world.env.execute("UPDATE execution_grants SET expires_at=? WHERE id=?", PAST, gid)
    return world, attempt, reach, lease, upload_id


def _assess(world, attempt_id, progress, *, at, approval_expires=None):
    with world.runner.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        return cer.assess(
            connection,
            row,
            now=at,
            free_worker_grant=True,
            progress=progress,
            approval_expires=approval_expires,
        )


def _t(minutes):
    return datetime.now(UTC) + timedelta(minutes=minutes)


def test_an_expired_grant_does_not_close_an_attempt_whose_upload_is_still_gaining_bytes(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    progress = cer.UploadProgress()
    aid = attempt["id"]
    _put(reach, lease, upload_id, 0)
    assert _assess(world, aid, progress, at=_t(1)) is None  # first sight: it is arriving
    _put(reach, lease, upload_id, 1)
    assert _assess(world, aid, progress, at=_t(9)) is None  # bytes grew: still arriving
    assert _assess(world, aid, progress, at=_t(15)) is None  # 6 minutes since the last growth
    _put(reach, lease, upload_id, 2)
    assert _assess(world, aid, progress, at=_t(24)) is None  # growth reset the stall window
    verdict = _assess(world, aid, progress, at=_t(40))  # 16 minutes with no new byte: stalled
    assert verdict is not None
    assert verdict.evidence["open_uploads"] == {"unexpired": 1, "expired": 0}
    assert verdict.evidence["hold_ended_by"] == "stall"
    assert "1 output upload(s) were still open (no bytes arrived in time)" in verdict.detail


def test_an_unknown_attempt_is_held_too_while_its_upload_is_arriving(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown' WHERE id=?", attempt["id"]
    )
    progress = cer.UploadProgress()
    _put(reach, lease, upload_id, 0)
    assert _assess(world, attempt["id"], progress, at=_t(1)) is None
    assert _assess(world, attempt["id"], progress, at=_t(30)) is not None


def test_a_restarted_runner_gives_an_open_upload_a_fresh_stall_window(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    _put(reach, lease, upload_id, 0)
    old = cer.UploadProgress()
    assert _assess(world, attempt["id"], old, at=_t(1)) is None
    restarted = cer.UploadProgress()  # nothing remembered: the upload has "just" progressed
    assert _assess(world, attempt["id"], restarted, at=_t(60)) is None
    assert _assess(world, attempt["id"], restarted, at=_t(75)) is not None


def test_without_a_watcher_an_open_unexpired_upload_always_holds(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    assert _assess(world, attempt["id"], None, at=_t(90)) is None  # inside the extended hold
    assert _assess(world, attempt["id"], None, at=_t(600)) is not None  # past the ceiling


def test_the_manifest_and_receipt_get_a_grace_after_the_last_byte_lands(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    done = (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    world.env.execute(
        "UPDATE snapshot_chunk_uploads SET status='completed', completed_at=? WHERE id=?",
        done,
        upload_id,
    )
    progress = cer.UploadProgress()
    assert _assess(world, attempt["id"], progress, at=_t(10) + timedelta(seconds=30)) is None
    assert _assess(world, attempt["id"], progress, at=_t(10) + timedelta(seconds=200)) is not None


def test_an_expired_open_upload_no_longer_holds_the_attempt(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    world.env.execute("UPDATE snapshot_chunk_uploads SET expires_at=?", PAST)
    assert _assess(world, attempt["id"], cer.UploadProgress(), at=_t(1)) is not None


def test_the_runner_does_not_mark_the_attempt_unknown_or_resolve_it_while_uploading(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    cid = attempt["campaign_id"]
    _put(reach, lease, upload_id, 0)
    _later(world, 5)
    for _ in range(3):
        world.runner.tick()
    row = world.row(
        "SELECT status, failure_class FROM campaign_stage_attempts WHERE id=?", attempt["id"]
    )
    assert row["status"] in ("admitted", "running") and row["failure_class"] is None
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "active"
    )
    assert not world.events_of("campaign.attempt_exit_resolved") and world.status(cid) != "blocked"
    # The upload stalls (the clock moves on, no byte arrives): the attempt is then settled.
    _later(world, 20)
    for _ in range(4):
        world.runner.tick()
    resolved = world.events_of("campaign.attempt_exit_resolved")
    assert (
        resolved
        and world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])[
            "status"
        ]
        == "reconciled"
    )


def test_progress_tracking_follows_held_bytes_and_forgets(tmp_path):
    tracker = cer.UploadProgress(stall_seconds=60)
    start = datetime.now(UTC)
    assert tracker.last_change("u", 10, start) == start
    assert tracker.last_change("u", 10, start + timedelta(seconds=30)) == start
    later = start + timedelta(seconds=45)
    assert tracker.last_change("u", 20, later) == later  # bytes changed: progress
    tracker.forget("u")
    assert tracker.last_change("u", 20, later + timedelta(seconds=300)) == later + timedelta(
        seconds=300
    )


def test_a_trickling_upload_cannot_hold_an_attempt_past_the_ceiling(tmp_path):
    """One chunk every few minutes keeps the stall watcher happy, but the hold is bounded by the
    recorded exit plus MAX_EXTENDED_HOLD_SECONDS (the grant window already ended)."""
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    progress = cer.UploadProgress()
    aid = attempt["id"]
    minute = 0
    seq = 0
    held_until = cer.MAX_EXTENDED_HOLD_SECONDS // 60
    while minute < held_until - 5:
        _put(reach, lease, upload_id, seq)
        seq += 1
        assert _assess(world, aid, progress, at=_t(minute)) is None, minute
        minute += 5  # a chunk every five minutes: always "progress"
    _put(reach, lease, upload_id, seq)
    verdict = _assess(world, aid, progress, at=_t(held_until + 10))
    assert verdict is not None  # the ceiling ended the hold although the upload was still gaining
    assert verdict.evidence["hold_ended_by"] == "ceiling"
    assert verdict.evidence["open_uploads"] == {"unexpired": 1, "expired": 0}
    assert "the hold ceiling was reached" in verdict.detail


def test_the_approval_window_also_ends_the_hold(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    progress = cer.UploadProgress()
    _put(reach, lease, upload_id, 0)
    window = _t(20)
    assert _assess(world, attempt["id"], progress, at=_t(10), approval_expires=window) is None
    _put(reach, lease, upload_id, 1)
    verdict = _assess(world, attempt["id"], progress, at=_t(25), approval_expires=window)
    assert verdict is not None and verdict.evidence["hold_ended_by"] == "ceiling"


def test_the_grant_window_still_holds_as_before_the_ceiling(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path, grant_expired=False)
    _end_lease(world, attempt["grant_id"])
    # In-grant behaviour is unchanged: an open upload within the grant lifetime holds.
    assert _assess(world, attempt["id"], None, at=_t(1)) is None


def test_part_by_part_growth_of_one_chunk_counts_as_progress_and_survives_a_restart(tmp_path):
    """A chunk sent in parts is stored in place, so its bytes grow with every part; a restarted
    worker resuming mid-chunk and a restarted runner both keep the upload alive."""
    import sqlite3

    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    progress = cer.UploadProgress()
    path = world.runner.store.database_path
    chunk = DATA[:CHUNK]
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO snapshot_chunks VALUES (?, 0, 'data.json', 0, 4, ?, ?)",
            (upload_id, hashlib.sha256(chunk).hexdigest(), chunk[:4]),
        )
    assert _assess(world, attempt["id"], progress, at=_t(1)) is None  # first sight
    with sqlite3.connect(path) as db:  # a second part lands inside the same chunk
        db.execute(
            "UPDATE snapshot_chunks SET size=8, content=? WHERE owner_id=?", (chunk[:8], upload_id)
        )
    assert _assess(world, attempt["id"], progress, at=_t(9)) is None
    assert (
        _assess(world, attempt["id"], progress, at=_t(15)) is None
    )  # 6 minutes: within the window
    # The worker restarted and resumes the chunk: more bytes, still alive after the window.
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE snapshot_chunks SET size=12, content=? WHERE owner_id=?",
            (chunk[:12], upload_id),
        )
    assert _assess(world, attempt["id"], progress, at=_t(22)) is None
    # A dropped partial chunk (its digest failed) changes the bytes too: that is activity.
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM snapshot_chunks WHERE owner_id=?", (upload_id,))
    assert _assess(world, attempt["id"], progress, at=_t(30)) is None
    assert _assess(world, attempt["id"], progress, at=_t(45)) is not None  # then it really stalls


def test_the_receipt_evidence_counts_open_and_expired_uploads_separately(tmp_path):
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    world.env.execute("UPDATE snapshot_chunk_uploads SET expires_at=?", PAST)  # now expired
    verdict = _assess(world, attempt["id"], cer.UploadProgress(), at=_t(1))
    assert verdict.evidence["open_uploads"] == {"unexpired": 0, "expired": 1}
    assert verdict.evidence["hold_ended_by"] is None
    assert "still open" not in verdict.detail  # an expired upload is not described as open


def test_exit_code_outcomes_carry_the_same_exact_evidence(tmp_path):
    """The detail names open uploads for every outcome class, not only exit_confirmed_unresolved."""
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    progress = cer.UploadProgress()
    _put(reach, lease, upload_id, 0)
    verdict = _assess(world, attempt["id"], progress, at=_t(0))
    assert verdict is None  # held first
    verdict = _assess(world, attempt["id"], progress, at=_t(30))
    assert verdict.failure_class == cer.EXIT_CONFIRMED_UNRESOLVED
    assert "still open" in verdict.detail


def test_finished_uploads_are_forgotten_and_eviction_gives_a_fresh_window():
    tracker = cer.UploadProgress(stall_seconds=60)
    tracker.MAX_TRACKED = 2
    start = datetime.now(UTC)
    tracker.last_change("a", 1, start)
    tracker.last_change("b", 1, start)
    tracker.last_change("c", 1, start + timedelta(seconds=100))  # evicts "a", the oldest
    assert "a" not in tracker._seen
    # An evicted upload simply starts a fresh window the next time it is seen: it is never closed
    # early, which is the safe direction.
    assert tracker.last_change("a", 1, start + timedelta(seconds=200)) == start + timedelta(
        seconds=200
    )
    tracker.forget("a")
    assert "a" not in tracker._seen
