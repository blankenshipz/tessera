"""Worker half of large-artifact custody: streaming collect, seal by reference, chunked upload.

Thresholds are monkeypatched to tiny values; no test writes a large file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tessera_control import artifact_limits as limits
from tessera_worker import artifacts
from tessera_worker.artifacts import ArtifactTransferError, upload_large_artifact, upload_output_set
from tessera_worker.outputs import (
    CollectedOutput,
    LargeOutput,
    OutputCollectionError,
    collect_output_set,
    collect_outputs,
)
from tessera_worker.sealed_outputs import OutputSealError, SealedOutputJournal

LEASE = "lease_large_1"
EPOCH = 3
OPERATION = hashlib.sha256(f"tessera-cpu-v1:{LEASE}".encode()).hexdigest()[:32]
LARGE_PAYLOAD = bytes(range(256)) * 20 + b"tail-bytes"  # 5130 bytes: 21 chunks of 256
WORKER_LIMIT = 1 << 20


@pytest.fixture(autouse=True)
def tiny_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "LEGACY_INLINE_BYTES", 1024)
    monkeypatch.setattr(limits, "CHUNK_BYTES", 256)


def _workspace(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


class FakeControl:
    """A small in-memory control mimicking the worker upload protocol."""

    def __init__(self, *, fail_chunk_at: int | None = None, chunk_bytes: int = 256) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.small: dict[str, dict[str, Any]] = {}
        self.chunk_sizes: list[int] = []
        self.placement_bodies: list[dict[str, Any]] = []
        self.manifest_bodies: list[dict[str, Any]] = []
        self.fail_chunk_at = fail_chunk_at
        self.chunk_bytes = chunk_bytes
        self.tamper_complete_sha = False

    # --- helpers -------------------------------------------------------------------------
    def _large_view(self, upload: dict[str, Any]) -> dict[str, Any]:
        contiguous = 0
        while contiguous in upload["chunks"]:
            contiguous += 1
        count = math.ceil(upload["size"] / upload["chunk_bytes"])
        return {
            "id": upload["id"],
            "lease_id": LEASE,
            "epoch": EPOCH,
            "operation_id": OPERATION,
            "relative_path": upload["relative_path"],
            "sha256": upload["sha256"],
            "size": upload["size"],
            "status": "completed" if upload.get("done") else "uploading",
            "chunk_bytes": upload["chunk_bytes"],
            "chunk_count": count,
            "received_chunks": len(upload["chunks"]),
            "next_seq": contiguous,
        }

    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        self.calls.append((path, body))
        prefix = f"/worker/leases/{LEASE}/"
        assert path.startswith(prefix)
        route = path[len(prefix) :].split("/")
        if route[0] == "large-artifact-uploads":
            return self._large(route[1:], body)
        if route[0] == "artifact-uploads":
            return self._legacy(route[1:], body)
        if route[0] == "project-artifacts":
            self.placement_bodies.append(body)
            return {
                "placements": [
                    {
                        "artifact_id": item["id"],
                        "sha256": item["sha256"],
                        "size": item["size"],
                        "state": "committed",
                    }
                    for item in body["artifacts"]
                ]
            }
        assert route == ["artifact-manifest"]
        self.manifest_bodies.append(body)
        entries = body["artifacts"]
        receipt = {
            "project_id": "project_1",
            "task_id": "task_1",
            "grant_id": "grant_1",
            "lease_id": LEASE,
            "worker_principal_id": "worker_1",
            "epoch": EPOCH,
            "operation_id": OPERATION,
            "plan_digest": "d" * 64,
            "artifacts": entries,
        }
        canonical = {"version": 1, **receipt}
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            **receipt,
            "manifest_digest": digest,
            "total_bytes": sum(item["size"] for item in entries),
            "status": "quarantined",
            "classification": "unclassified",
        }

    def _large(self, route: list[str], body: dict[str, Any]) -> dict[str, Any]:
        if not route:
            key = body["relative_path"]
            upload = self.uploads.setdefault(
                key,
                {
                    "id": f"large_{len(self.uploads)}",
                    "relative_path": key,
                    "sha256": body["sha256"],
                    "size": body["size"],
                    "chunk_bytes": self.chunk_bytes,
                    "chunks": {},
                },
            )
            return self._large_view(upload)
        upload = next(item for item in self.uploads.values() if item["id"] == route[0])
        if route[1] == "status":
            return self._large_view(upload)
        if route[1] == "chunks":
            seq = int(route[2])
            if self.fail_chunk_at is not None and seq == self.fail_chunk_at:
                self.fail_chunk_at = None
                raise ConnectionError("synthetic lost chunk response")
            raw = base64.b64decode(body["content_base64"], validate=True)
            assert hashlib.sha256(raw).hexdigest() == body["sha256"]
            expected = min(upload["chunk_bytes"], upload["size"] - seq * upload["chunk_bytes"])
            assert len(raw) == expected
            self.chunk_sizes.append(len(raw))
            upload["chunks"][seq] = raw
            view = self._large_view(upload)
            return {
                "seq": seq,
                "received_chunks": view["received_chunks"],
                "next_seq": view["next_seq"],
            }
        assert route[1] == "complete"
        data = b"".join(upload["chunks"][i] for i in sorted(upload["chunks"]))
        assert hashlib.sha256(data).hexdigest() == upload["sha256"]
        upload["done"] = True
        upload["data"] = data
        return {
            "id": upload["id"],
            "lease_id": LEASE,
            "epoch": EPOCH,
            "operation_id": OPERATION,
            "relative_path": upload["relative_path"],
            "sha256": "0" * 64 if self.tamper_complete_sha else upload["sha256"],
            "size": upload["size"],
            "status": "quarantined",
            "classification": "unclassified",
        }

    def _legacy(self, route: list[str], body: dict[str, Any]) -> dict[str, Any]:
        if not route:
            item = self.small.setdefault(
                body["relative_path"],
                {"id": f"small_{len(self.small)}", "received": 0, **body},
            )
        else:
            item = next(entry for entry in self.small.values() if entry["id"] == route[0])
            if route[1] == "chunks":
                item["received"] = body["offset"] + len(base64.b64decode(body["content_base64"]))
            if route[1] == "complete":
                return {
                    "id": item["id"],
                    "lease_id": LEASE,
                    "epoch": EPOCH,
                    "operation_id": OPERATION,
                    "relative_path": item["relative_path"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "status": "quarantined",
                    "classification": "unclassified",
                }
        return {
            "id": item["id"],
            "lease_id": LEASE,
            "relative_path": item["relative_path"],
            "sha256": item["sha256"],
            "size": item["size"],
            "status": "uploading",
            "received_bytes": item["received"],
        }


def _collect(root: Path, paths: list[str]) -> tuple[CollectedOutput | LargeOutput, ...]:
    return collect_output_set(root, paths, 100_000, WORKER_LIMIT)


def test_streaming_collect_hashes_whole_file_with_bounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    reads: list[int] = []
    real_read = os.read

    def spy(fd: int, length: int) -> bytes:
        data = real_read(fd, length)
        reads.append(len(data))
        return data

    monkeypatch.setattr(os, "read", spy)
    (output,) = _collect(root, ["model.bin"])
    monkeypatch.undo()
    assert isinstance(output, LargeOutput)
    assert output.sha256 == hashlib.sha256(LARGE_PAYLOAD).hexdigest()
    assert output.size == len(LARGE_PAYLOAD)
    assert reads and max(reads) <= 256 and sum(reads) == len(LARGE_PAYLOAD)


def test_small_outputs_keep_the_legacy_in_memory_shape(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"a.txt": b"alpha", "dir/b.txt": b"beta"})
    mixed = _collect(root, ["a.txt", "dir/b.txt"])
    assert mixed == collect_outputs(root, ["a.txt", "dir/b.txt"], 1024)


def test_plan_limit_above_worker_limit_is_refused_not_truncated(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"a.txt": b"alpha"})
    with pytest.raises(OutputCollectionError, match="exceeds this worker's configured maximum"):
        collect_output_set(root, ["a.txt"], WORKER_LIMIT + 1, WORKER_LIMIT)
    with pytest.raises(OutputCollectionError, match="invalid worker output limit"):
        collect_output_set(root, ["a.txt"], 10, 5)


def test_total_output_limit_counts_large_files(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    with pytest.raises(OutputCollectionError, match="output size exceeded"):
        collect_output_set(root, ["model.bin"], len(LARGE_PAYLOAD) - 1, WORKER_LIMIT)


def test_inline_files_are_bounded_in_total(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"a": b"x" * 800, "b": b"y" * 800})
    with pytest.raises(OutputCollectionError, match="inline output files"):
        _collect(root, ["a", "b"])


def _upload(control: FakeControl, outputs: tuple[Any, ...]) -> dict[str, Any]:
    return upload_output_set(control, lease_id=LEASE, expected_epoch=EPOCH, outputs=outputs)


def test_mixed_upload_streams_large_chunks_and_exempts_them_from_placement(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD, "notes.txt": b"small notes"})
    outputs = _collect(root, ["model.bin", "notes.txt"])
    control = FakeControl()
    receipt = _upload(control, outputs)
    assert control.uploads["model.bin"]["data"] == LARGE_PAYLOAD
    assert max(control.chunk_sizes) <= 256
    assert len(control.chunk_sizes) == math.ceil(len(LARGE_PAYLOAD) / 256)
    (placement,) = control.placement_bodies
    assert [item["size"] for item in placement["artifacts"]] == [len(b"small notes")]
    (manifest,) = control.manifest_bodies
    assert {item["relative_path"]: item["size"] for item in manifest["artifacts"]} == {
        "model.bin": len(LARGE_PAYLOAD),
        "notes.txt": 11,
    }
    assert receipt["total_bytes"] == len(LARGE_PAYLOAD) + 11
    assert receipt["project_placements_committed"] is True


def test_all_large_set_makes_no_placement_request(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    control = FakeControl()
    _upload(control, _collect(root, ["model.bin"]))
    # Nothing to place: large artifacts are exempt, so no placement request is made.
    assert control.placement_bodies == []


def test_upload_resumes_from_next_seq_after_a_lost_chunk(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    outputs = _collect(root, ["model.bin"])
    control = FakeControl(fail_chunk_at=5)
    with pytest.raises(ConnectionError):
        _upload(control, outputs)
    assert sorted(control.uploads["model.bin"]["chunks"]) == [0, 1, 2, 3, 4]
    control.chunk_sizes.clear()
    _upload(control, outputs)
    assert len(control.chunk_sizes) == math.ceil(len(LARGE_PAYLOAD) / 256) - 5
    assert control.uploads["model.bin"]["data"] == LARGE_PAYLOAD


def test_receipt_mismatch_is_refused(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    (output,) = _collect(root, ["model.bin"])
    control = FakeControl()
    control.tamper_complete_sha = True
    with pytest.raises(ArtifactTransferError, match="completion receipt"):
        upload_large_artifact(control, lease_id=LEASE, expected_epoch=EPOCH, output=output)
    # A control proposing a chunk size above the shared bound is refused before any chunk.
    oversized = FakeControl(chunk_bytes=4096)
    with pytest.raises(ArtifactTransferError, match="chunk size"):
        upload_large_artifact(oversized, lease_id=LEASE, expected_epoch=EPOCH, output=output)
    assert oversized.chunk_sizes == []


def test_changed_file_is_refused_before_any_chunk_is_sent(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    (output,) = _collect(root, ["model.bin"])
    path = root / "model.bin"
    # Same size and content length, one flipped byte (and mtime restored is not enough).
    data = bytearray(LARGE_PAYLOAD)
    data[100] ^= 1
    stat = path.stat()
    path.write_bytes(bytes(data))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    control = FakeControl()
    with pytest.raises(OutputCollectionError, match="changed after sealing"):
        upload_large_artifact(control, lease_id=LEASE, expected_epoch=EPOCH, output=output)
    assert control.calls == []


def test_replaced_file_is_refused(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    (output,) = _collect(root, ["model.bin"])
    replacement = root / "other"
    replacement.write_bytes(LARGE_PAYLOAD)
    os.replace(replacement, root / "model.bin")
    control = FakeControl()
    with pytest.raises(OutputCollectionError, match="changed after sealing"):
        upload_large_artifact(control, lease_id=LEASE, expected_epoch=EPOCH, output=output)
    assert control.calls == []


def test_hardlinked_or_symlinked_large_output_is_refused(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    os.link(root / "model.bin", tmp_path / "alias")
    with pytest.raises(OutputCollectionError, match="unsafe output file"):
        _collect(root, ["model.bin"])
    (tmp_path / "alias").unlink()
    (root / "link.bin").symlink_to(root / "model.bin")
    with pytest.raises(OutputCollectionError):
        _collect(root, ["link.bin"])


# --- seal by reference -------------------------------------------------------------------


def _observation() -> dict[str, Any]:
    return {
        "id": "obs_1",
        "lease_id": LEASE,
        "epoch": EPOCH,
        "observed_state": "exited",
        "termination_confirmed": True,
        "provider_id": "synthetic-container",
        "observation_proof": "trusted_worker_assertion",
        "operational_only": True,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def _journal(tmp_path: Path) -> SealedOutputJournal:
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    return SealedOutputJournal(root)


def _seal(journal: SealedOutputJournal, workspace: Path, paths: list[str]) -> str:
    return journal.seal(
        workspace=workspace,
        lease_id=LEASE,
        expected_epoch=EPOCH,
        output_paths=paths,
        max_output_bytes=100_000,
        termination_observation=_observation(),
        worker_max_output_bytes=WORKER_LIMIT,
    )


def test_seal_by_reference_stores_no_bytes_and_uploads(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD, "notes.txt": b"small notes"})
    journal = _journal(tmp_path)
    seal_id = _seal(journal, root, ["model.bin", "notes.txt"])
    assert _seal(journal, root, ["model.bin", "notes.txt"]) == seal_id
    with sqlite3.connect(journal.path) as connection:
        rows = connection.execute(
            "SELECT relative_path, large, length(content), size FROM sealed_outputs "
            "ORDER BY relative_path"
        ).fetchall()
    assert rows == [("model.bin", 1, 0, len(LARGE_PAYLOAD)), ("notes.txt", 0, 11, 11)]
    control = FakeControl()
    receipt = journal.resume(control, seal_id)
    assert receipt["total_bytes"] == len(LARGE_PAYLOAD) + 11
    assert control.uploads["model.bin"]["data"] == LARGE_PAYLOAD
    assert journal.resume(control, seal_id) == receipt


def test_small_only_manifest_is_unchanged(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"a.txt": b"alpha"})
    journal = _journal(tmp_path)
    seal_id = _seal(journal, root, ["a.txt"])
    expected = {
        "version": 1,
        "seal_id": seal_id,
        "lease_id": LEASE,
        "epoch": EPOCH,
        "operation_id": OPERATION,
        "artifacts": [
            {"relative_path": "a.txt", "sha256": hashlib.sha256(b"alpha").hexdigest(), "size": 5}
        ],
    }
    encoded = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(journal.path) as connection:
        manifest_json, digest = connection.execute(
            "SELECT manifest_json, manifest_digest FROM seals"
        ).fetchone()
    assert manifest_json == encoded
    assert digest == hashlib.sha256(encoded.encode()).hexdigest()


def test_modified_large_file_fails_closed_on_reseal_and_resume(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    journal = _journal(tmp_path)
    seal_id = _seal(journal, root, ["model.bin"])
    path = root / "model.bin"
    stat = path.stat()
    path.write_bytes(LARGE_PAYLOAD[:-1] + b"X")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    control = FakeControl()
    with pytest.raises(OutputSealError, match="changed after sealing"):
        journal.resume(control, seal_id)
    assert control.calls == []
    with pytest.raises(OutputSealError, match="changed after sealing"):
        _seal(journal, root, ["model.bin"])


def test_replaced_large_file_fails_closed_on_resume(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    journal = _journal(tmp_path)
    seal_id = _seal(journal, root, ["model.bin"])
    (root / "new").write_bytes(LARGE_PAYLOAD)
    os.replace(root / "new", root / "model.bin")
    with pytest.raises(OutputSealError):
        journal.resume(FakeControl(), seal_id)


def test_seal_refuses_a_plan_above_the_worker_limit(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"a.txt": b"alpha"})
    with pytest.raises(OutputSealError, match="worker's configured maximum"):
        _journal(tmp_path).seal(
            workspace=root,
            lease_id=LEASE,
            expected_epoch=EPOCH,
            output_paths=["a.txt"],
            max_output_bytes=WORKER_LIMIT + 1,
            termination_observation=_observation(),
            worker_max_output_bytes=WORKER_LIMIT,
        )


def test_journal_migrates_a_legacy_schema(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    with sqlite3.connect(root / "sealed-outputs.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE seals (
                id TEXT PRIMARY KEY, lease_id TEXT NOT NULL UNIQUE, epoch INTEGER NOT NULL,
                operation_id TEXT NOT NULL, paths_json TEXT NOT NULL,
                max_output_bytes INTEGER NOT NULL, observation_id TEXT NOT NULL,
                manifest_json TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                transfer_receipt_json TEXT
            );
            CREATE TABLE sealed_outputs (
                seal_id TEXT NOT NULL REFERENCES seals(id), relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY (seal_id, relative_path)
            );
            INSERT INTO seals VALUES ('s','l',1,'o','[]',1,'x','{}','d',NULL);
            INSERT INTO sealed_outputs VALUES ('s','a.txt','h',1,x'61');
            """
        )
    journal = SealedOutputJournal(root)
    SealedOutputJournal(root)  # idempotent
    with journal._connect() as connection:
        row = connection.execute("SELECT large, source_root FROM sealed_outputs").fetchone()
    assert tuple(row) == (0, None)


def test_artifacts_module_has_no_stale_chunk_alias() -> None:
    # The legacy single-BLOB constants keep their values for the unchanged path.
    assert artifacts.MAX_ARTIFACT_BYTES == 16 * 1024 * 1024
    assert artifacts.CHUNK_BYTES == 256 * 1024


def test_upload_tolerates_control_holding_later_chunks(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"model.bin": LARGE_PAYLOAD})
    control = FakeControl()
    original = control._large

    def seeded(route: list[str], body: dict[str, Any]) -> dict[str, Any]:
        view = original(route, body)
        if not route:  # right after begin, pre-store chunks 3-5
            upload = next(iter(control.uploads.values()))
            for seq in (3, 4, 5):
                upload["chunks"][seq] = LARGE_PAYLOAD[seq * 256 : (seq + 1) * 256]
            view = control._large_view(upload)
        return view

    control._large = seeded  # type: ignore[method-assign]
    _upload(control, _collect(root, ["model.bin"]))
    assert control.uploads["model.bin"]["data"] == LARGE_PAYLOAD
    # next_seq jumped past seq + 1 more than once, and every chunk was still sent and checked.
    assert len(control.chunk_sizes) == 21
