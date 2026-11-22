"""A large artifact finalizes once, however long control takes to verify it.

Hashing a multi-gigabyte upload outlasts a worker's request deadline (at most 20 s). Control used
to hash the whole file inside every ``complete`` call, so a worker that timed out asked again and
started another hash while the first was still running, and never saw an answer. ``complete``
now waits briefly, answers 202 "verifying", and a repeat joins the one verification (ADR 0050).
Tiny thresholds and a slowed hash; no large file.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from test_large_artifacts import (  # noqa: F401
    DATA,
    _fresh_operator_limits,
    begin_body,
    put_all,
    start,
    tiny_thresholds,
)
from test_large_output_worker import EPOCH, LEASE, FakeControl, _collect, _workspace

from tessera_control import large_artifacts
from tessera_control.store import Conflict, canonical_digest
from tessera_worker import artifacts
from tessera_worker.artifacts import ArtifactTransferError, upload_large_artifact

DEADLINE = 0.4  # the worker's request deadline, scaled down
HASH_SECONDS = 1.0


@pytest.fixture
def slow_hash(monkeypatch):
    runs: list[float] = []
    real = large_artifacts.verify_artifact_bytes

    def slow(*args, **kwargs):
        runs.append(time.monotonic())
        time.sleep(HASH_SECONDS)
        return real(*args, **kwargs)

    monkeypatch.setattr(large_artifacts, "verify_artifact_bytes", slow)
    monkeypatch.setattr(large_artifacts, "VERIFY_WAIT_SECONDS", 0.15, raising=False)
    getattr(large_artifacts, "_FLIGHTS", {}).clear()
    yield runs
    getattr(large_artifacts, "_FLIGHTS", {}).clear()


def _complete(store, worker, lease, upload):
    started = time.monotonic()
    answer = store.complete_upload(worker, lease["id"], upload["id"], "done", canonical_digest({}))
    return answer, time.monotonic() - started


def test_a_worker_asking_again_never_starts_a_second_hash(tmp_path, slow_hash):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    answers = []
    while True:
        answer, took = _complete(store, worker, lease, upload)
        assert took < DEADLINE  # every call answers inside the worker's deadline
        answers.append(answer[0])
        if answer[0] != 202:
            break
        assert answer[1]["status"] == "verifying" and answer[1]["retry_after_seconds"] >= 1
        time.sleep(0.2)
    assert answers[0] == 202 and answers[-1] == 201 and len(answers) >= 3
    assert len(slow_hash) == 1  # one verification for all the calls (before: one per call)
    artifact = answer[1]
    assert artifact["status"] == "quarantined" and artifact["size"] == len(DATA)


def test_concurrent_completes_share_one_verification(tmp_path, slow_hash):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    results: list[int] = []

    def ask():
        results.append(_complete(store, worker, lease, upload)[0][0])

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [202, 202, 202, 202]  # none finished inside the wait, none hashed again
    assert len(slow_hash) == 1
    time.sleep(HASH_SECONDS)
    (status, body), _ = _complete(store, worker, lease, upload)
    assert status == 201 and len(slow_hash) == 1
    # A replay after completion is the recorded answer, with no hashing at all.
    assert _complete(store, worker, lease, upload)[0] == (201, body)
    assert len(slow_hash) == 1


def test_a_mismatch_found_by_the_shared_verification_is_refused_and_not_cached(tmp_path, slow_hash):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"], stop=31)  # one chunk short
    first = _complete(store, worker, lease, upload)[0]
    assert first[0] == 202
    time.sleep(HASH_SECONDS + 0.3)
    with pytest.raises(Exception, match="size or checksum"):
        _complete(store, worker, lease, upload)
    assert not getattr(large_artifacts, "_FLIGHTS", {})  # the failed answer is not kept


# -- the worker half ---------------------------------------------------------------------


class _Verifying(FakeControl):
    """Control that answers 'verifying' to the first completions, as for a very large file."""

    def __init__(self, polls: int, **kwargs):
        super().__init__(**kwargs)
        self.polls, self.completes = polls, 0

    def _large(self, route, body):
        if len(route) == 2 and route[1] == "complete":
            self.completes += 1
            if self.completes <= self.polls:
                return {"id": route[0], "status": "verifying", "retry_after_seconds": 2}
        return super()._large(route, body)


PAYLOAD = bytes(range(256)) * 20 + b"tail-bytes"


def _output(tmp_path):
    root = _workspace(tmp_path, {"model.bin": PAYLOAD})
    (output,) = _collect(root, ["model.bin"])
    return output


def test_the_worker_asks_again_with_the_same_key_until_the_receipt_arrives(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(artifacts, "_sleep", slept.append)
    control = _Verifying(polls=3)
    receipt = upload_large_artifact(
        control, lease_id=LEASE, expected_epoch=EPOCH, output=_output(tmp_path)
    )
    assert receipt["status"] == "quarantined" and control.completes == 4
    assert slept == [2.0, 2.0, 2.0]
    completes = [c for c in control.calls if c[0].endswith("/complete")]
    assert len(completes) == 4  # no chunk was sent again between the asks
    assert control.chunk_sizes and len(control.chunk_sizes) == 21


def test_the_worker_gives_up_when_verification_never_finishes(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(artifacts, "_monotonic", lambda: now[0])
    monkeypatch.setattr(artifacts, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(artifacts, "COMPLETE_MAX_WAIT_SECONDS", 7.0)
    control = _Verifying(polls=100)
    with pytest.raises(ArtifactTransferError, match="still verifying"):
        upload_large_artifact(
            control, lease_id=LEASE, expected_epoch=EPOCH, output=_output(tmp_path)
        )
    assert control.completes == 4  # 2 s per ask, bounded at 7 s, never a loop


# -- bound on the clock, the hold, cleanup, compatibility ------------------------------------


def test_the_poll_bound_counts_the_time_each_ask_takes(tmp_path, monkeypatch):
    """Each ask can itself wait on control for seconds; the bound is on elapsed time, not on the
    sum of the sleeps (which understated it several times over)."""
    now = [0.0]
    monkeypatch.setattr(artifacts, "_monotonic", lambda: now[0])
    monkeypatch.setattr(artifacts, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(artifacts, "COMPLETE_MAX_WAIT_SECONDS", 60.0)

    class Slow(_Verifying):
        def _large(self, route, body):
            if len(route) == 2 and route[1] == "complete":
                now[0] += 8.0  # control holds the request before answering 202
            return super()._large(route, body)

    control = Slow(polls=1000)
    with pytest.raises(ArtifactTransferError, match="still verifying"):
        upload_large_artifact(
            control, lease_id=LEASE, expected_epoch=EPOCH, output=_output(tmp_path)
        )
    # 10 s per round (8 s on the call, 2 s asleep): six rounds fit 60 s and a seventh does not;
    # counting only the sleeps would have allowed thirty asks.
    assert control.completes == 7 and now[0] <= 60.0 + 8.0


def test_the_poll_bound_is_below_the_hold_stall_window():
    from tessera_control.campaign_exited_resolve import UPLOAD_STALL_SECONDS

    assert artifacts.COMPLETE_MAX_WAIT_SECONDS < UPLOAD_STALL_SECONDS


@pytest.mark.parametrize(
    ("advice", "slept"), [(None, 5.0), ("soon", 5.0), (0, 1.0), (-5, 1.0), (7, 7.0), (9999, 30.0)]
)
def test_a_retry_hint_is_clamped_or_ignored(tmp_path, monkeypatch, advice, slept):
    waits: list[float] = []
    monkeypatch.setattr(artifacts, "_sleep", waits.append)

    class Hinted(_Verifying):
        def _large(self, route, body):
            reply = super()._large(route, body)
            if reply.get("status") == "verifying":
                reply = {**reply, "retry_after_seconds": advice}
            return reply

    upload_large_artifact(
        Hinted(polls=1), lease_id=LEASE, expected_epoch=EPOCH, output=_output(tmp_path)
    )
    assert waits == [slept]


def test_an_older_worker_gets_a_202_it_rejects_and_a_fast_201_on_its_retry(tmp_path, slow_hash):
    """An older worker does not know "verifying": it fails its receipt check on the 202 body and
    its next attempt asks again. That ask finds the verification done, with no second hash."""
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    (status, body), _ = _complete(store, worker, lease, upload)
    assert status == 202
    # What an older worker checks (lease_id, epoch, ... status "quarantined") is absent.
    assert "lease_id" not in body and body["status"] != "quarantined"
    time.sleep(HASH_SECONDS + 0.2)
    (status, body), took = _complete(store, worker, lease, upload)
    assert status == 201 and took < DEADLINE and body["status"] == "quarantined"
    assert len(slow_hash) == 1


def test_finished_answers_are_swept_and_a_slow_caller_never_drops_a_newer_flight(
    tmp_path, slow_hash, monkeypatch
):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    _complete(store, worker, lease, upload)
    time.sleep(HASH_SECONDS + 0.2)  # the verification finished; its answer is kept
    ((key, flight),) = large_artifacts._FLIGHTS.items()
    assert flight.finished is not None
    # An older flight object under the same key is not the one forgotten.
    other = large_artifacts._Flight()
    large_artifacts._forget(key, other)
    assert large_artifacts._FLIGHTS[key] is flight
    # Past the keep time the sweep drops it, so an upload that never completes cannot pin it.
    monkeypatch.setattr(large_artifacts, "FLIGHT_KEEP_SECONDS", 0.0)
    with large_artifacts._FLIGHTS_LOCK:
        large_artifacts._sweep_flights(time.monotonic() + 1)
    assert not large_artifacts._FLIGHTS


def test_a_restart_mid_verification_just_hashes_again(tmp_path, slow_hash):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    assert _complete(store, worker, lease, upload)[0][0] == 202
    large_artifacts._FLIGHTS.clear()  # the control process restarted: the in-memory flight is gone
    assert _complete(store, worker, lease, upload)[0][0] == 202
    time.sleep(2 * HASH_SECONDS)
    (status, _), _ = _complete(store, worker, lease, upload)
    assert status == 201 and len(slow_hash) == 2  # one more hash, never a stuck "verifying"


def test_a_lease_refenced_between_the_202_and_the_ask_finalizes_nothing(tmp_path, slow_hash):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    assert _complete(store, worker, lease, upload)[0][0] == 202
    time.sleep(HASH_SECONDS + 0.2)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE execution_leases SET epoch = epoch + 1")  # a newer fence
    with pytest.raises(Conflict):
        _complete(store, worker, lease, upload)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0  # nothing finalized
