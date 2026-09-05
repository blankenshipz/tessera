from __future__ import annotations

import base64
import hashlib
import json

import pytest
from test_input_delivery import _delivery
from test_runtime import ApiChannel

from tessera_control.store import _connect
from tessera_worker.artifacts import CHUNK_BYTES, ArtifactTransferError, upload_artifact
from tessera_worker.control_channel import MAX_REQUEST_BYTES


class MemoryChannel:
    def __init__(self):
        self.body = None
        self.content = b""
        self.requests = []

    def post(self, path, body, key):
        assert len(json.dumps(body).encode()) < MAX_REQUEST_BYTES
        self.requests.append((path, body, key))
        if path.endswith("/artifact-uploads"):
            self.body = body
        if path.endswith("/chunks"):
            assert body["offset"] == len(self.content)
            chunk = base64.b64decode(body["content_base64"])
            assert 0 < len(chunk) <= CHUNK_BYTES
            self.content += chunk
        receipt = {
            "id": "upload_test",
            "lease_id": "lease_test",
            "relative_path": self.body["relative_path"],
            "sha256": self.body["sha256"],
            "size": self.body["size"],
            "received_bytes": len(self.content),
            "status": "uploading",
        }
        if path.endswith("/complete"):
            assert hashlib.sha256(self.content).hexdigest() == self.body["sha256"]
            receipt.update(
                epoch=1,
                operation_id=self.body["operation_id"],
                status="quarantined",
                classification="unclassified",
            )
        return receipt


def test_large_artifact_uses_bounded_requests():
    channel = MemoryChannel()
    content = b"large synthetic output" * 100_000
    result = upload_artifact(
        channel,
        lease_id="lease_test",
        expected_epoch=1,
        relative_path="result.txt",
        content=content,
    )
    assert result["status"] == "quarantined"
    assert channel.content == content
    chunks = [body for path, body, _ in channel.requests if path.endswith("/chunks")]
    assert len(chunks) > 1


@pytest.mark.parametrize("endpoint", ["artifact-uploads", "chunks", "complete"])
def test_lost_reply_resumes_without_duplicate_bytes(tmp_path, monkeypatch, endpoint):
    state, _, _, _, _, lease = _delivery(tmp_path)
    monkeypatch.setattr("tessera_worker.artifacts.CHUNK_BYTES", 3)

    class LoseReply(ApiChannel):
        lost = False

        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path.endswith("/" + endpoint) and not self.lost:
                self.lost = True
                raise ConnectionError("synthetic lost committed response")
            return result

    arguments = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "relative_path": "result.txt",
        "content": b"retained\x00\xff\x80 output",
    }
    with pytest.raises(ConnectionError, match="lost committed"):
        upload_artifact(LoseReply(state), **arguments)
    # A new client/runtime can rediscover committed progress from the service.
    result = upload_artifact(ApiChannel(state), **arguments)
    assert result["status"] == "quarantined"
    assert upload_artifact(ApiChannel(state), **arguments) == result
    with _connect(state / "tessera.sqlite3") as connection:
        rows = connection.execute("SELECT content FROM artifacts").fetchall()
        assert len(rows) == 1 and rows[0][0] == arguments["content"]


def test_empty_artifact_needs_no_chunk(tmp_path):
    state, _, _, _, _, lease = _delivery(tmp_path)
    receipt = upload_artifact(
        ApiChannel(state),
        lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        relative_path="result.txt",
        content=b"",
    )
    assert receipt["size"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"lease_id": "../escape"},
        {"expected_epoch": True},
        {"relative_path": "../escape"},
        {"relative_path": "/absolute"},
        {"content": bytearray(b"mutable")},
        {"content": b"x" * (16 * 1024 * 1024 + 1)},
    ],
)
def test_invalid_input_does_not_send(change):
    channel = MemoryChannel()
    arguments = dict(
        lease_id="lease_test", expected_epoch=1, relative_path="result.txt", content=b"x"
    )
    arguments.update(change)
    with pytest.raises(ArtifactTransferError):
        upload_artifact(channel, **arguments)
    assert not channel.requests


@pytest.mark.parametrize(
    "endpoint,change",
    [
        ("artifact-uploads", {"id": "../escape"}),
        ("status", {"received_bytes": True}),
        ("status", {"received_bytes": 99}),
        ("status", {"sha256": "0" * 64}),
        ("chunks", {"received_bytes": 0}),
        ("complete", {"status": "succeeded"}),
        ("complete", {"epoch": True}),
        ("complete", {"operation_id": "0" * 32}),
    ],
)
def test_unmatched_receipt_fails_closed(endpoint, change):
    class BadReceipt(MemoryChannel):
        def post(self, path, body, key):
            receipt = super().post(path, body, key)
            if path.endswith("/" + endpoint):
                receipt.update(change)
            return receipt

    with pytest.raises(ArtifactTransferError):
        upload_artifact(
            BadReceipt(),
            lease_id="lease_test",
            expected_epoch=1,
            relative_path="result.txt",
            content=b"x",
        )
