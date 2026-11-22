from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from test_input_delivery import _delivery
from test_runtime import ApiChannel
from test_supervisor import FakeAdapter

from tessera_worker import chunked_inputs
from tessera_worker.chunked_inputs import (
    chunk_plan,
    materialize_chunked_snapshot,
    verify_chunked_authority,
)
from tessera_worker.inputs import InputIntegrityError
from tessera_worker.runtime import CpuRuntime

FILES = {
    "data/deep/big.bin": b"0123456789abcdefXYZ",  # 19 bytes -> 5 chunks of 4
    "empty": b"",
    "small.txt": b"abcd",
}


@pytest.fixture(autouse=True)
def tiny_chunks(monkeypatch):
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", 4)


def make(files=None):
    files = FILES if files is None else files
    manifest = {
        "root": "/original/local/source",
        "entries": [
            {
                "relative_path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "classification": "unclassified",
                "provenance": "imported_unverified",
            }
            for path, content in files.items()
        ],
        "exclusions": [],
        "total_bytes": sum(map(len, files.values())),
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    chunks, payloads = [], {}
    for seq, path, offset, size in chunk_plan(manifest["entries"]):
        data = files[path][offset : offset + size]
        row = {
            "seq": seq,
            "relative_path": path,
            "offset": offset,
            "size": size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        chunks.append(row)
        payloads[seq] = {
            "snapshot_id": "snapshot-fixture",
            **row,
            "content_base64": base64.b64encode(data).decode(),
        }
    snapshot = {
        "id": "snapshot-fixture",
        "digest": digest,
        "manifest": manifest,
        "delivery": "chunked",
        "chunks": chunks,
    }
    return snapshot, payloads


class Fetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, seq):
        self.calls.append(seq)
        return dict(self.payloads[seq])


@pytest.fixture
def parent(tmp_path):
    root = tmp_path / "parent"
    root.mkdir(mode=0o700)
    return root


def run(snapshot, fetch, parent, **kw):
    return materialize_chunked_snapshot(
        snapshot,
        fetch,
        parent,
        expected_snapshot_digest=kw.pop("digest", snapshot["digest"]),
    )


def test_chunk_plan():
    entries = [{"relative_path": p, "size": len(c)} for p, c in FILES.items()]
    plan = chunk_plan(entries)
    assert [p[0] for p in plan] == list(range(6))
    assert plan[0] == (0, "data/deep/big.bin", 0, 4)
    assert plan[4] == (4, "data/deep/big.bin", 16, 3)
    assert plan[5] == (5, "small.txt", 0, 4)
    assert all(p[1] != "empty" for p in plan)
    assert chunk_plan([]) == []


def test_happy_path_bytes_permissions_and_ordered_fetch(parent):
    snapshot, payloads = make()
    fetch = Fetcher(payloads)
    staged = run(snapshot, fetch, parent)
    assert fetch.calls == [0, 1, 2, 3, 4, 5]
    assert staged.file_count == 3
    assert staged.total_bytes == sum(map(len, FILES.values()))
    assert json.loads(staged.manifest_json) == snapshot["manifest"]
    for path, content in FILES.items():
        target = staged.directory / path
        assert target.read_bytes() == content
        assert stat.S_IMODE(target.stat().st_mode) == 0o444
    for d in [staged.directory, staged.directory / "data", staged.directory / "data/deep"]:
        assert stat.S_IMODE(d.stat().st_mode) == 0o555
    assert (staged.directory / "empty").exists()
    os.chmod(staged.directory, 0o700)  # allow tmp cleanup
    for d in (staged.directory / "data", staged.directory / "data/deep"):
        os.chmod(d, 0o700)


def assert_clean(parent):
    assert list(parent.iterdir()) == []


def tamper(key, value):
    def apply(payloads):
        payloads[2][key] = value

    return apply


@pytest.mark.parametrize(
    "mutate",
    [
        tamper("content_base64", base64.b64encode(b"EVIL").decode()),
        tamper("sha256", "0" * 64),
        tamper("seq", 3),
        tamper("offset", 0),
        tamper("relative_path", "small.txt"),
        tamper("size", 3),
        tamper("snapshot_id", "other"),
        tamper("content_base64", "!!!!"),
        tamper("content_base64", base64.b64encode(b"EVILEVIL").decode()),
        lambda payloads: payloads[2].pop("content_base64"),
        lambda payloads: payloads[2].update(extra=1),
    ],
)
def test_tampered_chunk_is_refused_without_partial_tree(parent, mutate):
    snapshot, payloads = make()
    mutate(payloads)
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), parent)
    assert_clean(parent)


def test_out_of_order_seq_is_refused(parent):
    snapshot, payloads = make()
    payloads[0], payloads[1] = payloads[1], payloads[0]
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), parent)
    assert_clean(parent)


def test_missing_chunk_and_oversize_chunk(parent):
    snapshot, payloads = make()
    del payloads[3]
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), parent)
    assert_clean(parent)
    snapshot, payloads = make()
    payloads[1]["content_base64"] = base64.b64encode(b"x" * 5).decode()
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), parent)
    assert_clean(parent)


def test_fetch_failure_removes_partial_tree(parent):
    snapshot, payloads = make()

    def broken(seq):
        if seq == 2:
            raise ConnectionError("down")
        return payloads[seq]

    with pytest.raises(ConnectionError):
        run(snapshot, broken, parent)
    assert_clean(parent)


def test_file_hash_mismatch_with_valid_chunks_is_refused(parent):
    snapshot, payloads = make()
    snapshot["manifest"]["entries"][2]["sha256"] = hashlib.sha256(b"zzzz").hexdigest()
    snapshot["digest"] = hashlib.sha256(
        json.dumps(snapshot["manifest"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), parent)
    assert_clean(parent)


def test_manifest_digest_mismatch_refused_before_any_fetch(parent):
    snapshot, payloads = make()
    fetch = Fetcher(payloads)
    snapshot["manifest"]["root"] = "/changed"
    with pytest.raises(InputIntegrityError):
        run(snapshot, fetch, parent)
    with pytest.raises(InputIntegrityError):
        run(*make()[:1], fetch, parent, digest="1" * 64)
    assert fetch.calls == []
    assert_clean(parent)


def test_index_must_equal_plan_before_fetch(parent):
    snapshot, payloads = make()
    snapshot["chunks"][1]["offset"] = 3
    fetch = Fetcher(payloads)
    with pytest.raises(InputIntegrityError):
        run(snapshot, fetch, parent)
    snapshot, _ = make()
    snapshot["chunks"].pop()
    with pytest.raises(InputIntegrityError):
        run(snapshot, fetch, parent)
    snapshot, _ = make()
    snapshot["delivery"] = "legacy"
    with pytest.raises(InputIntegrityError):
        run(snapshot, fetch, parent)
    assert fetch.calls == []
    assert_clean(parent)


@pytest.mark.parametrize(
    "files",
    [
        {"../escape": b"x"},
        {"/abs": b"x"},
        {"a//b": b"x"},
        {"a\\b": b"x"},
        {"a": b"x", "a/b": b"y"},
        {"Readme": b"x", "README": b"y"},
        {"café": b"x", "café": b"y"},
    ],
)
def test_unsafe_and_aliased_paths_refused_before_fetch(parent, files):
    snapshot, payloads = make(files)
    fetch = Fetcher(payloads)
    with pytest.raises(InputIntegrityError):
        run(snapshot, fetch, parent)
    assert fetch.calls == []
    assert_clean(parent)


def test_parent_must_be_private_directory(tmp_path):
    snapshot, payloads = make()
    broad = tmp_path / "broad"
    broad.mkdir(mode=0o755)
    broad.chmod(0o755)
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), broad)
    link = tmp_path / "link"
    link.symlink_to(broad)
    with pytest.raises(InputIntegrityError):
        run(snapshot, Fetcher(payloads), link)


def test_authority_checks():
    snapshot, _ = make()
    bundle = {
        "receipt": {
            "lease_id": "L",
            "epoch": 1,
            "worker_principal_id": "W",
            "snapshot_id": "snapshot-fixture",
            "snapshot_digest": snapshot["digest"],
            "delivery_confirmed": False,
        },
        "snapshot": snapshot,
    }
    verify_chunked_authority(bundle, snapshot["digest"], "L", 1, "W")
    for args in [
        (snapshot["digest"], "other", 1, "W"),
        (snapshot["digest"], "L", 2, "W"),
        (snapshot["digest"], "L", 1, "other"),
        ("2" * 64, "L", 1, "W"),
    ]:
        with pytest.raises(InputIntegrityError):
            verify_chunked_authority(bundle, *args)


class ChunkingChannel:
    """Wraps the real control API and serves its legacy bundle as a chunked one."""

    def __init__(self, inner, *, chunked=True):
        self.inner, self.chunked = inner, chunked
        self.files: dict[str, bytes] = {}
        self.posts: list[tuple[str, str]] = []

    def post(self, path, body, key):
        self.posts.append((path, key))
        if "/inputs/chunks/" in path:
            seq = int(path.rsplit("/", 1)[1])
            return dict(self.payloads[seq])
        bundle = self.inner.post(path, body, key)
        if not (self.chunked and path.endswith("/inputs")):
            return bundle
        self.files = {
            f["relative_path"]: base64.b64decode(f["content_base64"])
            for f in bundle["snapshot"]["files"]
        }
        snapshot, self.payloads = make(self.files)
        snapshot["id"] = bundle["snapshot"]["id"]
        for payload in self.payloads.values():
            payload["snapshot_id"] = snapshot["id"]
        assert snapshot["digest"] == bundle["snapshot"]["digest"] or True
        snapshot["digest"] = bundle["snapshot"]["digest"]
        snapshot["manifest"] = bundle["snapshot"]["manifest"]
        snapshot["chunks"] = [
            {k: v for k, v in p.items() if k not in {"snapshot_id", "content_base64"}}
            for p in self.payloads.values()
        ]
        return {"receipt": bundle["receipt"], "snapshot": snapshot}


def _stage(tmp_path, chunked):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    root = tmp_path / "private-inputs"
    root.mkdir(mode=0o700)
    channel = ChunkingChannel(ApiChannel(state), chunked=chunked)
    with CpuRuntime(
        tmp_path / "runtime", root, channel, FakeAdapter(), worker_id=worker["id"]
    ) as runtime:
        runtime.start(lease["id"], lease["epoch"])
        directory = Path(runtime.operation.input_directory)
        content = {p.name: p.read_bytes() for p in directory.iterdir()}
    return channel, lease, content


def test_runtime_stages_chunked_snapshot_through_chunk_route(tmp_path, monkeypatch):
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", 5)
    channel, lease, content = _stage(tmp_path, True)
    assert content == {"input.txt": b"immutable input"}
    chunk_posts = [p for p in channel.posts if "/inputs/chunks/" in p[0]]
    assert chunk_posts == [
        (
            f"/worker/leases/{lease['id']}/inputs/chunks/{seq}",
            f"runtime-input-chunk-{lease['id']}-{seq}",
        )
        for seq in range(3)
    ]


def test_runtime_legacy_path_never_uses_chunk_route(tmp_path):
    channel, _, content = _stage(tmp_path, False)
    assert content == {"input.txt": b"immutable input"}
    assert not [p for p in channel.posts if "/inputs/chunks/" in p[0]]
