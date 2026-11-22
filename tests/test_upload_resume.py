"""A restarted worker resumes a large-artifact upload from the parts control already confirmed
(ADR 0050): it never re-sends a confirmed part and never sends a part out of order."""

from __future__ import annotations

import hashlib

import pytest
from test_artifacts import _two_path_delivery
from test_large_artifacts import (  # noqa: F401 - the autouse fixtures of the control-side tests
    CHUNK,
    DATA,
    _fresh_operator_limits,
    tiny_thresholds,
)

from tessera_control import artifact_limits as limits
from tessera_control.large_artifacts import LargeArtifactStore
from tessera_control.store import Conflict, canonical_digest
from tessera_worker.artifacts import upload_large_artifact
from tessera_worker.outputs import collect_output_set

PART = 4  # a quarter of a 16-byte chunk


class Restart(Exception):
    """The poller process died; the part it had just sent was applied, its reply was lost."""


class StoreChannel:
    """The worker's channel wired straight to the real control store (no HTTP)."""

    def __init__(self, store, worker, lease_id, *, die_after_parts: int | None = None):
        self.store, self.worker, self.lease_id = store, worker, lease_id
        self.die_after_parts = die_after_parts
        self.parts: list[tuple[int, int | None]] = []  # (seq, part_offset) in send order
        # A channel that carries only the base64 of one 4-byte part: the worker sends 4-byte parts.
        self.max_request_bytes = limits.REQUEST_ENVELOPE_BYTES + 8

    def post(self, path, body, key):
        tail = path.split("/large-artifact-uploads", 1)[1].strip("/").split("/")
        digest = canonical_digest(body)
        if tail == [""]:
            return self.store.begin_upload(self.worker, self.lease_id, key, digest, body)[1]
        upload_id = tail[0]
        if tail[1] == "status":
            return self.store.upload_status(self.worker, self.lease_id, upload_id, key, digest)[1]
        if tail[1] == "complete":
            return self.store.complete_upload(self.worker, self.lease_id, upload_id, key, digest)[1]
        seq = int(tail[2])
        self.parts.append((seq, body.get("part_offset")))
        reply = self.store.put_chunk(self.worker, self.lease_id, upload_id, seq, body)[1]
        if self.die_after_parts is not None and len(self.parts) >= self.die_after_parts:
            raise Restart  # applied by control, reply lost with the process
        return reply


def _setup(tmp_path, monkeypatch):
    # Parts of 4 bytes: shrink the part granule and the envelope so a tiny channel bound allows it.
    monkeypatch.setattr(limits, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(limits, "PART_GRANULE_BYTES", PART)
    monkeypatch.setattr(limits, "MIN_PART_BYTES", PART)
    state, worker, lease = _two_path_delivery(tmp_path, 4096)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "first.txt").write_bytes(DATA)
    (output,) = collect_output_set(root, ["first.txt"], 100_000, 1 << 20)
    return LargeArtifactStore(state), worker, lease, output


def _upload(channel, lease, output):
    return upload_large_artifact(
        channel, lease_id=lease["id"], expected_epoch=lease["epoch"], output=output
    )


def _stored(store, worker, lease):
    import sqlite3

    with sqlite3.connect(store.database_path) as db:
        return dict(db.execute("SELECT seq, size FROM snapshot_chunks ORDER BY seq").fetchall())


def test_a_restart_between_parts_resumes_without_resending_or_reordering(tmp_path, monkeypatch):
    store, worker, lease, output = _setup(tmp_path, monkeypatch)
    first = StoreChannel(store, worker, lease["id"], die_after_parts=6)  # chunk 1 holds 8 of 16
    with pytest.raises(Restart):
        _upload(first, lease, output)
    assert first.parts == [(0, 0), (0, 4), (0, 8), (0, 12), (1, 0), (1, 4)]
    assert _stored(store, worker, lease) == {0: 16, 1: 8}  # chunk 0 whole, chunk 1 half held

    second = StoreChannel(store, worker, lease["id"])  # the poller restarted: a new process
    receipt = _upload(second, lease, output)
    assert receipt["status"] == "quarantined"
    assert receipt["sha256"] == hashlib.sha256(DATA).hexdigest()
    # Nothing confirmed is sent again: chunk 0 is not touched and chunk 1 continues at byte 8.
    assert second.parts[0] == (1, 8)
    assert (0, 0) not in second.parts and (1, 0) not in second.parts and (1, 4) not in second.parts
    seen: dict[int, int] = {}
    for seq, offset in second.parts:  # in order, no repeat, no gap within a chunk
        expected = seen.get(seq, 0 if seq != 1 else 8)
        assert offset == expected or (offset is None and expected == 0)
        seen[seq] = expected + PART
    assert len(second.parts) == len(set(second.parts))


def test_a_restart_with_a_different_part_size_continues_from_the_stored_byte(tmp_path, monkeypatch):
    store, worker, lease, output = _setup(tmp_path, monkeypatch)
    first = StoreChannel(store, worker, lease["id"], die_after_parts=2)
    with pytest.raises(Restart):
        _upload(first, lease, output)
    assert _stored(store, worker, lease) == {0: 8}
    second = StoreChannel(store, worker, lease["id"])
    second.max_request_bytes = limits.REQUEST_ENVELOPE_BYTES + 16  # now parts of 8 bytes fit
    _upload(second, lease, output)
    assert second.parts[0] == (0, 8)  # not (0, 0): the 8 stored bytes are not resent


def test_a_restart_after_the_last_part_but_before_complete_just_completes(tmp_path, monkeypatch):
    store, worker, lease, output = _setup(tmp_path, monkeypatch)
    parts_total = (len(DATA) // CHUNK) * (CHUNK // PART)
    first = StoreChannel(store, worker, lease["id"], die_after_parts=parts_total)
    with pytest.raises(Restart):
        _upload(first, lease, output)
    second = StoreChannel(store, worker, lease["id"])
    assert _upload(second, lease, output)["status"] == "quarantined"
    assert second.parts == []  # every chunk was already confirmed; only complete was sent


def test_status_reports_the_partial_chunks_control_holds(tmp_path, monkeypatch):
    store, worker, lease, output = _setup(tmp_path, monkeypatch)
    channel = StoreChannel(store, worker, lease["id"], die_after_parts=6)
    with pytest.raises(Restart):
        _upload(channel, lease, output)
    status = StoreChannel(store, worker, lease["id"])
    upload_id = _stored_upload_id(store)
    reply = status.post(
        f"/worker/leases/{lease['id']}/large-artifact-uploads/{upload_id}/status", {}, "s"
    )
    assert reply["partial_chunks"] == [{"seq": 1, "received_bytes": 8}]
    assert reply["received_chunks"] == 1 and reply["next_seq"] == 1


def _stored_upload_id(store):
    import sqlite3

    with sqlite3.connect(store.database_path) as db:
        return db.execute("SELECT id FROM snapshot_chunk_uploads").fetchone()[0]


def test_a_lost_reply_retry_of_a_stored_part_is_accepted_not_an_error(tmp_path, monkeypatch):
    """A duplicate part is a no-op; its receipt reports what is held, which may be more."""
    store, worker, lease, output = _setup(tmp_path, monkeypatch)
    channel = StoreChannel(store, worker, lease["id"])
    upload_id = None
    begun = channel.post(
        f"/worker/leases/{lease['id']}/large-artifact-uploads",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[
                :32
            ],
            "relative_path": "first.txt",
            "sha256": hashlib.sha256(DATA).hexdigest(),
            "size": len(DATA),
        },
        "b",
    )
    upload_id = begun["id"]
    import base64

    chunk = DATA[:CHUNK]

    def send(start, stop):
        return channel.post(
            f"/worker/leases/{lease['id']}/large-artifact-uploads/{upload_id}/chunks/0",
            {
                "content_base64": base64.b64encode(chunk[start:stop]).decode(),
                "sha256": hashlib.sha256(chunk).hexdigest(),
                "part_offset": start,
            },
            "k",
        )

    send(0, 4)
    send(4, 8)
    again = send(0, 4)  # the same part again, after a lost reply
    assert again["chunk_received_bytes"] == 8
    with pytest.raises(Conflict):
        send(12, 16)  # a gap is still refused


def test_a_corrupted_partial_chunk_is_dropped_and_the_next_restart_succeeds(tmp_path, monkeypatch):
    import sqlite3

    store, worker, lease, output = _setup(tmp_path, monkeypatch)
    first = StoreChannel(store, worker, lease["id"], die_after_parts=2)
    with pytest.raises(Restart):
        _upload(first, lease, output)
    with sqlite3.connect(store.database_path) as db:  # the held bytes are damaged at rest
        db.execute("UPDATE snapshot_chunks SET content = x'ffffffffffffffff' WHERE seq = 0")
    second = StoreChannel(store, worker, lease["id"])
    with pytest.raises(Conflict, match="sha256"):  # the whole-chunk digest catches it
        _upload(second, lease, output)
    assert _stored(store, worker, lease) == {}  # control dropped the bad partial
    third = StoreChannel(store, worker, lease["id"])
    assert _upload(third, lease, output)["status"] == "quarantined"
    assert third.parts[0] == (0, 0)
