"""Large-artifact uploads fit the request limits on the path (ADR 0050): control advertises its
body limit and the largest part that fits, the worker sizes its requests to the smaller of that and
its own channel bound, parts reassemble into the same chunk, and a mismatch fails fast."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Any

import pytest
from test_large_artifacts import (
    CHUNK,
    DATA,
    _fresh_operator_limits,  # noqa: F401
    put_all,
    start,
    tiny_thresholds,  # noqa: F401 - the autouse fixtures of the control-side tests
)
from test_large_output_worker import EPOCH, LARGE_PAYLOAD, LEASE, FakeControl, _collect, _workspace

from tessera_control import artifact_limits as limits
from tessera_control.store import Conflict
from tessera_worker.artifacts import ArtifactTransferError, upload_large_artifact
from tessera_worker.control_channel import ControlChannelError, HttpControlChannel


def _part(store, worker, lease_id, upload_id, seq, data, start, stop, chunk_sha=None, **over):
    piece = data[start:stop]
    body = {
        "content_base64": base64.b64encode(piece).decode(),
        "sha256": chunk_sha or hashlib.sha256(data).hexdigest(),
        "part_offset": start,
    } | over
    return store.put_chunk(worker, lease_id, upload_id, seq, body)


# -- the limits and what control advertises ----------------------------------------------------


def test_part_size_fits_the_request_with_headroom_and_never_exceeds_one_chunk():
    assert limits.part_bytes_for(24 * limits.MiB) == limits.CHUNK_BYTES  # the default: whole chunks
    one_mib = limits.part_bytes_for(limits.MiB)
    assert one_mib == 11 * 64 * 1024 and limits.request_bytes_for_part(one_mib) <= limits.MiB
    for request in (512 * 1024, 700_000, limits.MiB, 3 * limits.MiB, 100 * limits.MiB):
        part = limits.part_bytes_for(request)
        assert limits.MIN_PART_BYTES <= part <= limits.CHUNK_BYTES and part % (64 * 1024) == 0
        assert limits.request_bytes_for_part(part) <= request


def test_the_request_limit_is_operator_configurable_and_validated(monkeypatch):
    assert limits.artifact_limits({}).max_request_bytes == 24 * limits.MiB
    assert limits.artifact_limits({"TESSERA_MAX_REQUEST_BYTES": "1048576"}).max_request_bytes == (
        limits.MiB
    )
    for bad in ("1000", "x", str(1 << 40)):
        with pytest.raises(limits.ArtifactLimitError):
            limits.artifact_limits({"TESSERA_MAX_REQUEST_BYTES": bad})
    from tessera_control.request_limits import RequestLimitsMiddleware

    monkeypatch.setenv("TESSERA_MAX_REQUEST_BYTES", str(limits.MiB))
    limits.operator_limits.cache_clear()
    assert RequestLimitsMiddleware(app=None).max_bytes == limits.MiB
    monkeypatch.delenv("TESSERA_MAX_REQUEST_BYTES")
    limits.operator_limits.cache_clear()
    assert RequestLimitsMiddleware(app=None).max_bytes == 24 * limits.MiB


def test_begin_and_status_advertise_the_request_limit_and_the_part_size(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERA_MAX_REQUEST_BYTES", str(limits.MiB))
    limits.operator_limits.cache_clear()
    _state, worker, lease, store, status, upload = start(tmp_path)
    assert status == 201
    assert upload["max_request_bytes"] == limits.MiB
    assert upload["part_bytes"] == limits.part_bytes_for(limits.MiB)
    from tessera_control.store import canonical_digest

    _, again = store.upload_status(worker, lease["id"], upload["id"], "s", canonical_digest({}))
    assert (again["max_request_bytes"], again["part_bytes"]) == (
        upload["max_request_bytes"],
        upload["part_bytes"],
    )
    assert upload["chunk_bytes"] == CHUNK  # the storage chunk is unchanged


# -- control: parts reassemble into the same chunk ----------------------------------------------


def test_a_chunk_sent_in_parts_is_stored_once_whole_and_the_artifact_completes(tmp_path):
    _state, worker, lease, store, _status, upload = start(tmp_path)
    lid, uid = lease["id"], upload["id"]
    chunks = [DATA[i : i + CHUNK] for i in range(0, len(DATA), CHUNK)]
    for seq, chunk in enumerate(chunks):
        first = _part(store, worker, lid, uid, seq, chunk, 0, 6)
        assert first[0] == 201
        assert first[1]["chunk_received_bytes"] == 6 and first[1]["received_chunks"] == seq
        assert first[1]["next_seq"] == seq  # a partial chunk is not received yet
        mid = _part(store, worker, lid, uid, seq, chunk, 6, 11)
        assert mid[1]["chunk_received_bytes"] == 11 and mid[1]["received_chunks"] == seq
        done = _part(store, worker, lid, uid, seq, chunk, 11, CHUNK)
        assert done[1]["chunk_received_bytes"] == CHUNK and done[1]["received_chunks"] == seq + 1
    from tessera_control.store import canonical_digest

    status, artifact = store.complete_upload(worker, lid, uid, "complete", canonical_digest({}))
    assert status == 201 and artifact["sha256"] == hashlib.sha256(DATA).hexdigest()


def test_part_errors_are_refused_without_corrupting_what_is_stored(tmp_path):
    _state, worker, lease, store, _status, upload = start(tmp_path)
    lid, uid = lease["id"], upload["id"]
    chunk = DATA[:CHUNK]
    with pytest.raises(Conflict, match="in order"):
        _part(store, worker, lid, uid, 0, chunk, 4, 9)  # a gap: parts start at the beginning
    assert _part(store, worker, lid, uid, 0, chunk, 0, 8)[0] == 201
    assert _part(store, worker, lid, uid, 0, chunk, 0, 8)[0] == 200  # a repeat stores nothing new
    assert (
        _part(store, worker, lid, uid, 0, chunk, 4, 12)[1]["chunk_received_bytes"] == 12
    )  # overlap
    with pytest.raises(Conflict, match="different bytes"):
        _part(store, worker, lid, uid, 0, DATA[1 : CHUNK + 1], 0, 8)  # not the bytes stored
    with pytest.raises(Conflict, match="different bytes"):
        _part(store, worker, lid, uid, 0, chunk, 8, 12, chunk_sha="f" * 64)  # another chunk digest
    with pytest.raises(Conflict, match="does not fit"):
        _part(store, worker, lid, uid, 0, chunk + b"x", 12, CHUNK + 1)  # past the planned chunk
    assert _part(store, worker, lid, uid, 0, chunk, 12, CHUNK)[1]["received_chunks"] == 1


def test_a_chunk_whose_parts_do_not_hash_to_the_declared_digest_is_dropped_and_resendable(tmp_path):
    _state, worker, lease, store, _status, upload = start(tmp_path)
    lid, uid = lease["id"], upload["id"]
    chunk = DATA[:CHUNK]
    wrong = bytearray(chunk)
    wrong[3] ^= 1
    _part(
        store, worker, lid, uid, 0, bytes(wrong), 0, 8, chunk_sha=hashlib.sha256(chunk).hexdigest()
    )
    with pytest.raises(Conflict, match="sha256"):
        _part(
            store,
            worker,
            lid,
            uid,
            0,
            bytes(wrong),
            8,
            CHUNK,
            chunk_sha=hashlib.sha256(chunk).hexdigest(),
        )
    got = _part(store, worker, lid, uid, 0, chunk, 0, CHUNK)  # the drop was committed: start over
    assert got[0] == 201 and got[1]["received_chunks"] == 1


def test_a_whole_chunk_still_works_unchanged_and_mixes_with_parts(tmp_path):
    _state, worker, lease, store, _status, upload = start(tmp_path)
    lid, uid = lease["id"], upload["id"]
    put_all(store, worker, lid, uid, stop=3)  # whole chunks, as an older worker sends them
    _part(store, worker, lid, uid, 3, DATA[3 * CHUNK : 4 * CHUNK], 0, 5)
    put_all(store, worker, lid, uid, start=4)
    assert _part(store, worker, lid, uid, 3, DATA[3 * CHUNK : 4 * CHUNK], 5, CHUNK)[0] == 201
    from tessera_control.store import canonical_digest

    assert store.complete_upload(worker, lid, uid, "c", canonical_digest({}))[0] == 201


# -- worker: sizes requests to the path, fails fast on a mismatch --------------------------------


class LimitedControl(FakeControl):
    """The in-memory control with a request limit, advertisement and part support."""

    def __init__(self, *, max_request: int, advertise: bool = True, parts: bool = True, **kw):
        super().__init__(**kw)
        self.max_request, self.advertise, self.parts = max_request, advertise, parts
        self.request_sizes: list[int] = []
        self.part_offsets: list[int | None] = []

    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        size = len(json.dumps(body).encode())
        self.request_sizes.append(size)
        if size > self.max_request:
            raise ControlChannelError("control rejected request", 413)
        if "/chunks/" in path and "part_offset" in body:
            return self._put_part(path, body)
        reply = super().post(path, body, key)
        if self.advertise and path.endswith("large-artifact-uploads") or path.endswith("/status"):
            reply = {**reply, "max_request_bytes": self.max_request}
            if self.parts:
                reply["part_bytes"] = limits.part_bytes_for(self.max_request)
        return reply

    def _put_part(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        route = path.split("/")
        upload = next(u for u in self.uploads.values() if u["id"] == route[-3])
        seq = int(route[-1])
        raw = base64.b64decode(body["content_base64"], validate=True)
        held = upload.setdefault("partial", {}).get(seq, b"")
        assert body["part_offset"] == len(held)
        held += raw
        self.part_offsets.append(body["part_offset"])
        total = min(upload["chunk_bytes"], upload["size"] - seq * upload["chunk_bytes"])
        if len(held) == total:
            assert hashlib.sha256(held).hexdigest() == body["sha256"]
            upload["chunks"][seq] = held
            upload["partial"].pop(seq, None)
        else:
            upload["partial"][seq] = held
        view = self._large_view(upload)
        return {
            "seq": seq,
            "received_chunks": view["received_chunks"],
            "next_seq": view["next_seq"],
            "chunk_received_bytes": len(held),
        }


def _one_file(tmp_path, payload=LARGE_PAYLOAD):
    root = _workspace(tmp_path, {"model.bin": payload})
    (output,) = _collect(root, ["model.bin"])
    return output


def _upload(control, output):
    return upload_large_artifact(control, lease_id=LEASE, expected_epoch=EPOCH, output=output)


def test_the_worker_splits_a_chunk_into_parts_that_fit_the_advertised_request_limit(tmp_path):
    output = _one_file(tmp_path)
    # Chunks of 256 bytes, but control (and the path) only take about 20 KiB requests.
    limit = limits.request_bytes_for_part(limits.MIN_PART_BYTES)  # the smallest part: 64 KiB
    control = LimitedControl(max_request=limit)
    assert limits.part_bytes_for(limit) == limits.MIN_PART_BYTES >= 256  # whole chunk fits here
    _upload(control, output)
    assert control.uploads["model.bin"]["data"] == LARGE_PAYLOAD
    assert (
        max(control.request_sizes) <= limit and control.part_offsets == []
    )  # nothing needed splitting


BIG_CHUNK = 96 * 1024  # a storage chunk larger than the smallest legal part (64 KiB)
BIG_PAYLOAD = bytes(range(256)) * 390 + b"end"  # 99843 bytes: a 96 KiB chunk and a short one


def _big(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "CHUNK_BYTES", BIG_CHUNK)
    root = _workspace(tmp_path, {"model.bin": BIG_PAYLOAD})
    (output,) = _collect(root, ["model.bin"])
    return output


def test_a_channel_bound_below_the_chunk_makes_the_worker_send_parts(tmp_path, monkeypatch):
    output = _big(tmp_path, monkeypatch)

    class Channel(LimitedControl):
        max_request_bytes = 150_000  # this worker's own bound; a 96 KiB chunk's base64 is bigger

    control = Channel(max_request=24 * limits.MiB, chunk_bytes=BIG_CHUNK)
    _upload(control, output)
    assert control.uploads["model.bin"]["data"] == BIG_PAYLOAD
    assert control.part_offsets == [0, 64 * 1024]  # the first chunk went in two parts
    assert max(control.request_sizes) <= 150_000


def test_control_advertising_a_small_request_limit_shrinks_the_parts_too(tmp_path, monkeypatch):
    output = _big(tmp_path, monkeypatch)
    control = LimitedControl(max_request=150_000, chunk_bytes=BIG_CHUNK)
    _upload(control, output)
    assert control.uploads["model.bin"]["data"] == BIG_PAYLOAD
    assert control.part_offsets == [0, 64 * 1024] and max(control.request_sizes) <= 150_000


def test_no_part_size_fits_fails_fast_with_the_numbers_and_sends_no_chunk(tmp_path, monkeypatch):
    output = _big(tmp_path, monkeypatch)

    class Narrow(LimitedControl):
        max_request_bytes = 90_000  # below what the smallest part (64 KiB) needs

    control = Narrow(max_request=24 * limits.MiB, chunk_bytes=BIG_CHUNK)
    with pytest.raises(ArtifactTransferError, match="No upload part fits the request limit"):
        _upload(control, output)
    assert not any("/chunks/" in path for path, _ in control.calls)


def test_a_control_that_refuses_with_413_fails_fast_not_forever(tmp_path):
    output = _one_file(tmp_path)
    attempts: list[str] = []

    class Refusing(FakeControl):
        def post(self, path, body, key):
            if "/chunks/" in path:
                attempts.append(path)
                raise ControlChannelError("control rejected request", 413)
            return super().post(path, body, key)

    with pytest.raises(ArtifactTransferError, match="TESSERA_MAX_REQUEST_BYTES"):
        _upload(Refusing(), output)
    assert len(attempts) == 1  # one try, no retry loop


def test_a_control_without_part_support_is_used_as_before(tmp_path):
    output = _one_file(tmp_path)
    control = FakeControl()  # advertises nothing: whole chunks, exactly the old behaviour
    _upload(control, output)
    assert control.uploads["model.bin"]["data"] == LARGE_PAYLOAD
    assert len(control.chunk_sizes) == math.ceil(len(LARGE_PAYLOAD) / 256)


def test_the_http_channel_exposes_the_bound_the_uploader_sizes_to():
    assert HttpControlChannel.max_request_bytes == 1024 * 1024
    assert limits.part_bytes_for(HttpControlChannel.max_request_bytes) == 11 * 64 * 1024


def test_credential_watch_forwards_the_channel_request_bound():
    """The development worker wraps its channel; the uploader must still see the 1 MiB bound."""
    from tessera_worker import artifacts as worker_artifacts
    from tessera_worker.control_channel import MAX_REQUEST_BYTES, HttpControlChannel
    from tessera_worker.service import _CredentialWatch

    class Bounded:
        max_request_bytes = MAX_REQUEST_BYTES

    watch = _CredentialWatch(Bounded())
    assert watch.max_request_bytes == HttpControlChannel.max_request_bytes == MAX_REQUEST_BYTES
    begun = {"max_request_bytes": 24 * limits.MiB, "part_bytes": limits.CHUNK_BYTES}
    part = worker_artifacts._part_bytes(watch, begun, limits.CHUNK_BYTES)
    assert part == limits.part_bytes_for(MAX_REQUEST_BYTES) < limits.CHUNK_BYTES
    assert limits.request_bytes_for_part(part) <= MAX_REQUEST_BYTES
    assert _CredentialWatch(object()).max_request_bytes is None
