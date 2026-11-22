"""Chunked inputs follow the operator artifact bound; a Salad stage takes a multi-GB artifact.

No network and no live provider: the guardian's input client is driven against a fake control
opener that serves a virtual 3 GiB custody artifact (the same 4 MiB block repeated), so no test
holds more than a few chunks in memory.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

from tessera_control import salad_workload_contract as contract
from tessera_control.salad_input_delivery import GuardianInputs
from tessera_worker import chunked_inputs
from tessera_worker.chunked_inputs import materialize_chunked_snapshot

GIB = 1024**3
BLOCK = bytes(range(256)) * (4 * 1024 * 1024 // 256)  # one 4 MiB chunk
BLOCKS = 768  # 3 GiB
SIZE = BLOCKS * len(BLOCK)
ORIGIN = "http://127.0.0.1:8765"
TOKEN = "ticket-" + "a" * 40


def test_default_bound_follows_the_operator_limit_not_a_fixed_2_gib():
    assert chunked_inputs.MAX_BYTES == 8 * GIB
    assert contract.MAX_INPUT_BYTES == 8 * GIB
    # 3 GiB was refused before: the contract now accepts a declared 3 GiB staged input.
    contract.InputSnapshot(kind="staged_snapshot", snapshot_digest="a" * 64, max_bytes=3 * GIB)
    with pytest.raises(ValueError):
        contract.InputSnapshot(
            kind="staged_snapshot", snapshot_digest="a" * 64, max_bytes=8 * GIB + 1
        )


def test_the_bound_is_read_from_the_operator_environment():
    code = (
        "from tessera_worker import chunked_inputs as c\n"
        "from tessera_control import salad_workload_contract as k\n"
        "print(c.MAX_BYTES, k.MAX_INPUT_BYTES)\n"
    )

    def run(value):
        env = {"PATH": "/usr/bin:/bin", "TESSERA_MAX_ARTIFACT_BYTES": value}
        return subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
        )

    ok = run(str(3 * GIB))
    assert ok.stdout.split() == [str(3 * GIB)] * 2, ok.stderr[-300:]
    assert run("lots").returncode != 0
    assert run("1024").returncode != 0


def _virtual_snapshot():
    sha = hashlib.sha256()
    for _ in range(BLOCKS):
        sha.update(BLOCK)
    chunk_sha = hashlib.sha256(BLOCK).hexdigest()
    entry = dict(
        relative_path="model.bin",
        size=SIZE,
        sha256=sha.hexdigest(),
        provenance="imported_unverified",
        classification="unclassified",
    )
    manifest = dict(root="synthetic", entries=[entry], exclusions=[], total_bytes=SIZE)
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    index = [
        dict(
            seq=seq,
            relative_path="model.bin",
            offset=seq * len(BLOCK),
            size=len(BLOCK),
            sha256=chunk_sha,
        )
        for seq in range(BLOCKS)
    ]
    return manifest, digest, index


class _Response:
    def __init__(self, value):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._body = io.BytesIO(json.dumps(value).encode())

    def read(self, limit=-1):
        return self._body.read(limit)

    def close(self):
        pass


@pytest.fixture(scope="module")
def snapshot():
    return _virtual_snapshot()


def _control(snapshot):
    manifest, digest, index = snapshot
    served = []

    def opener(request, timeout):
        path = "/" + request.full_url.split("://", 1)[1].split("/", 1)[1]
        if path == "/provider-inputs/manifest":
            return _Response(
                dict(
                    snapshot_id="snapshot_virtual",
                    digest=digest,
                    manifest=copy.deepcopy(manifest),
                    delivery="chunked",
                    chunks=index,
                )
            )
        seq = int(path.rsplit("/", 1)[1])
        served.append(seq)
        return _Response(
            dict(
                snapshot_id="snapshot_virtual",
                **index[seq],
                content_base64=base64.b64encode(BLOCK).decode(),
            )
        )

    return opener, served


def _guardian(snapshot, max_bytes):
    from datetime import UTC, datetime, timedelta

    opener, served = _control(snapshot)
    guard = GuardianInputs(ORIGIN, opener=opener, clock=lambda: 100.0)
    expires = (datetime.fromtimestamp(100.0, UTC) + timedelta(minutes=30)).isoformat()
    ticket = dict(
        token=TOKEN,
        expires_at=expires.replace("+00:00", "Z"),
        snapshot_id="snapshot_virtual",
        snapshot_digest=snapshot[1],
    )
    guard.install("op", ticket, expected_digest=snapshot[1], max_bytes=max_bytes, deadline=10**9)
    return guard, served


def test_a_salad_stage_reads_a_multi_gb_custody_artifact_through_the_guardian(snapshot):
    guard, served = _guardian(snapshot, max_bytes=SIZE)
    reply = guard.manifest("op")
    assert reply["manifest"]["total_bytes"] == SIZE > 2 * GIB
    assert len(reply["chunks"]) == BLOCKS
    for seq in (0, BLOCKS // 2, BLOCKS - 1):  # first, middle and last chunk, in 1 MiB ranges
        for offset in range(0, len(BLOCK), 1 << 20):
            piece = guard.chunk("op", seq, offset, 1 << 20)
            assert base64.b64decode(piece["content_base64"]) == BLOCK[offset : offset + (1 << 20)]
    assert sorted(set(served)) == [0, BLOCKS // 2, BLOCKS - 1]  # each chunk fetched once


def test_the_reviewed_cap_and_the_operator_bound_still_refuse_larger_artifacts(snapshot):
    smaller, _ = _guardian(snapshot, max_bytes=SIZE - 1)
    with pytest.raises(ValueError):
        smaller.manifest("op")
    from tessera_control import salad_input_delivery

    guard, _ = _guardian(snapshot, max_bytes=SIZE)
    original = salad_input_delivery.MAX_BYTES
    salad_input_delivery.MAX_BYTES = 2 * GIB  # an operator bound below the artifact
    try:
        with pytest.raises(ValueError):
            guard.manifest("op")
    finally:
        salad_input_delivery.MAX_BYTES = original


def test_materializing_a_chunked_input_holds_about_one_chunk(tmp_path, monkeypatch):
    # 8 MiB in 64 KiB chunks: peak traced memory stays far below the file size.
    chunk = 64 * 1024
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", chunk)
    data = bytes(range(256)) * (8 * 1024 * 1024 // 256)
    entry = dict(
        relative_path="weights.bin",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        provenance="imported_unverified",
        classification="unclassified",
    )
    manifest = dict(root="synthetic", entries=[entry], exclusions=[], total_bytes=len(data))
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    index = [
        dict(
            seq=i,
            relative_path="weights.bin",
            offset=i * chunk,
            size=chunk,
            sha256=hashlib.sha256(data[i * chunk : (i + 1) * chunk]).hexdigest(),
        )
        for i in range(len(data) // chunk)
    ]
    snap = dict(id="snapshot_x", digest=digest, manifest=manifest, delivery="chunked", chunks=index)

    def fetch(seq):
        return dict(
            snapshot_id="snapshot_x",
            **index[seq],
            content_base64=base64.b64encode(data[seq * chunk : (seq + 1) * chunk]).decode(),
        )

    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    tracemalloc.start()
    try:
        staged = materialize_chunked_snapshot(snap, fetch, parent, expected_snapshot_digest=digest)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (Path(staged.directory) / "weights.bin").read_bytes() == data
    assert peak < len(data) // 2
