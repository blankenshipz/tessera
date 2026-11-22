"""Verify and stage chunked input snapshots in trusted supervisor custody.

Stdlib-only (it reuses helpers from ``inputs``, which is also stdlib-only) so it
can run inside provider images. It grants no authority: the caller supplies the
expected digest and fetches chunks over the authenticated control channel. Only
one chunk is held in memory at a time. See ADR 0037 section 2.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .inputs import InputIntegrityError, MaterializedSnapshot, _path

CHUNK_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_BYTES = 8 * 1024**3


def _operator_bound() -> int:
    """The operator artifact bound, ``TESSERA_MAX_ARTIFACT_BYTES`` (default 8 GiB).

    Read once at start, with the same range as ``tessera_control.artifact_limits`` (this
    module stays stdlib-only so a workload image can ship it). It was a fixed 2 GiB.
    """
    raw = os.environ.get("TESSERA_MAX_ARTIFACT_BYTES")
    if raw is None or raw == "":
        return _DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("TESSERA_MAX_ARTIFACT_BYTES must be a whole number of bytes.") from None
    if not 16 * 1024 * 1024 <= value <= 1024**4:
        raise ValueError("TESSERA_MAX_ARTIFACT_BYTES must lie between 16 MiB and 1 TiB.")
    return value


MAX_BYTES = _operator_bound()
MAX_FILES = 10_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_CHUNK_KEYS = {"snapshot_id", "seq", "relative_path", "offset", "size", "sha256", "content_base64"}
_INDEX_KEYS = {"seq", "relative_path", "offset", "size", "sha256"}
_MALFORMED = (KeyError, TypeError, ValueError, AttributeError, UnicodeError, RecursionError)


def chunk_plan(entries: list[dict[str, Any]]) -> list[tuple[int, str, int, int]]:
    """Return ``(seq, relative_path, offset, size)`` for manifest-ordered entries."""
    plan: list[tuple[int, str, int, int]] = []
    for entry in entries:
        size = entry["size"]
        for offset in range(0, size, CHUNK_BYTES):
            plan.append(
                (len(plan), entry["relative_path"], offset, min(CHUNK_BYTES, size - offset))
            )
    return plan


def verify_chunked_authority(
    bundle: dict[str, Any],
    expected_snapshot_digest: str,
    expected_lease_id: str,
    expected_epoch: int,
    expected_worker_id: str,
) -> None:
    """Same receipt/authority checks as the legacy bundle verifier."""
    try:
        if (
            not isinstance(expected_snapshot_digest, str)
            or not _DIGEST.fullmatch(expected_snapshot_digest)
            or type(expected_epoch) is not int
            or expected_epoch < 1
            or not expected_lease_id
            or not expected_worker_id
            or not isinstance(bundle, dict)
        ):
            raise InputIntegrityError("Expected input authority is invalid.")
        receipt, snapshot = bundle["receipt"], bundle["snapshot"]
        if (
            receipt["lease_id"] != expected_lease_id
            or type(receipt["epoch"]) is not int
            or receipt["epoch"] != expected_epoch
            or receipt["worker_principal_id"] != expected_worker_id
            or receipt["snapshot_digest"] != expected_snapshot_digest
            or receipt["snapshot_id"] != snapshot["id"]
            or receipt["delivery_confirmed"] is not False
            or snapshot["digest"] != expected_snapshot_digest
            or snapshot.get("delivery") != "chunked"
        ):
            raise InputIntegrityError("Bundle does not match the expected lease and snapshot.")
    except InputIntegrityError:
        raise
    except _MALFORMED as error:
        raise InputIntegrityError("Input bundle is malformed.") from error


def _verify_manifest(snapshot: dict[str, Any], digest: str) -> dict[str, dict[str, Any]]:
    manifest = snapshot["manifest"]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if (
        len(encoded) > MAX_MANIFEST_BYTES
        or hashlib.sha256(encoded).hexdigest() != digest
        or snapshot["digest"] != digest
        or set(manifest) != {"root", "entries", "exclusions", "total_bytes"}
        or not isinstance(manifest["root"], str)
        or not isinstance(manifest["exclusions"], list)
        or len(manifest["exclusions"]) > 10000
        or type(manifest["total_bytes"]) is not int
        or not 0 <= manifest["total_bytes"] <= MAX_BYTES
    ):
        raise InputIntegrityError("Input manifest integrity check failed.")
    for exclusion in manifest["exclusions"]:
        if set(exclusion) != {"relative_path", "reason"} or not all(
            isinstance(value, str) for value in exclusion.values()
        ):
            raise InputIntegrityError("Input exclusion metadata is invalid.")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise InputIntegrityError("Input file set exceeds bounds or is incomplete.")
    expected: dict[str, dict[str, Any]] = {}
    total = 0
    for entry in entries:
        path = _path(entry["relative_path"])
        if (
            set(entry) != {"relative_path", "size", "sha256", "provenance", "classification"}
            or path in expected
            or type(entry["size"]) is not int
            or not 0 <= entry["size"] <= MAX_BYTES
            or not isinstance(entry["sha256"], str)
            or not _DIGEST.fullmatch(entry["sha256"])
            or entry["provenance"] != "imported_unverified"
            or entry["classification"] != "unclassified"
        ):
            raise InputIntegrityError("Input manifest entry is invalid.")
        expected[path] = entry
        total += entry["size"]
    if total != manifest["total_bytes"]:
        raise InputIntegrityError("Input total size does not match the manifest.")
    aliases: dict[str, str] = {}
    for path in expected:
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            alias = unicodedata.normalize("NFC", prefix).casefold()
            if alias in aliases and aliases[alias] != prefix:
                raise InputIntegrityError("Input paths alias on a supported filesystem.")
            aliases[alias] = prefix
        if any(str(parent) in expected for parent in Path(path).parents if str(parent) != "."):
            raise InputIntegrityError("Input file path collides with a directory.")
    return expected


def _verify_index(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    index = snapshot["chunks"]
    plan = chunk_plan(snapshot["manifest"]["entries"])
    if not isinstance(index, list) or len(index) != len(plan):
        raise InputIntegrityError("Chunk index does not match the manifest.")
    for item, (seq, path, offset, size) in zip(index, plan, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != _INDEX_KEYS
            or type(item["seq"]) is not int
            or item["seq"] != seq
            or item["relative_path"] != path
            or type(item["offset"]) is not int
            or item["offset"] != offset
            or type(item["size"]) is not int
            or item["size"] != size
            or not isinstance(item["sha256"], str)
            or not _DIGEST.fullmatch(item["sha256"])
        ):
            raise InputIntegrityError("Chunk index does not match the manifest.")
    return index


def _decode_chunk(snapshot: dict[str, Any], item: dict[str, Any], chunk: Any) -> bytes:
    if (
        not isinstance(chunk, dict)
        or set(chunk) != _CHUNK_KEYS
        or chunk["snapshot_id"] != snapshot["id"]
        or any(
            type(chunk[key]) is not type(item[key]) or chunk[key] != item[key]
            for key in _INDEX_KEYS
        )
    ):
        raise InputIntegrityError("Delivered chunk does not match the chunk index.")
    encoded = chunk["content_base64"]
    if not isinstance(encoded, str) or len(encoded) != ((item["size"] + 2) // 3) * 4:
        raise InputIntegrityError("Encoded chunk size does not match the chunk index.")
    content = base64.b64decode(encoded, validate=True)
    if (
        len(content) != item["size"]
        or hashlib.sha256(content).hexdigest() != item["sha256"]
        or base64.b64encode(content).decode("ascii") != encoded
    ):
        raise InputIntegrityError("Chunk checksum or size is invalid.")
    return content


def materialize_chunked_snapshot(
    snapshot: dict[str, Any],
    fetch_chunk: Callable[[int], dict[str, Any]],
    parent: Path,
    *,
    expected_snapshot_digest: str,
) -> MaterializedSnapshot:
    """Verify the manifest and index, then stream chunks into a fresh private tree.

    Authority (lease/epoch/worker) must be checked by the caller with
    ``verify_chunked_authority``. A partial tree is removed on any failure.
    """
    if os.name != "posix":
        raise InputIntegrityError("Input staging requires POSIX filesystem permissions.")
    try:
        if (
            not isinstance(expected_snapshot_digest, str)
            or not _DIGEST.fullmatch(expected_snapshot_digest)
            or not isinstance(snapshot, dict)
            or snapshot["delivery"] != "chunked"
        ):
            raise InputIntegrityError("Chunked snapshot does not match the expected digest.")
        expected = _verify_manifest(snapshot, expected_snapshot_digest)
        index = _verify_index(snapshot)
    except InputIntegrityError:
        raise
    except _MALFORMED as error:
        raise InputIntegrityError("Input bundle is malformed.") from error
    parent = Path(parent)
    try:
        parent_mode = parent.lstat().st_mode
    except OSError as error:
        raise InputIntegrityError("Input staging parent must be a trusted directory.") from error
    if (
        stat.S_ISLNK(parent_mode)
        or not stat.S_ISDIR(parent_mode)
        or stat.S_IMODE(parent_mode) != 0o700
    ):
        raise InputIntegrityError("Input staging parent must be a private 0700 directory.")
    directory = Path(
        tempfile.mkdtemp(prefix=f"tessera-input-{expected_snapshot_digest}-", dir=parent)
    )
    directories = {directory}

    def cleanup() -> None:
        for path in directories:
            path.chmod(0o700)
        shutil.rmtree(directory)

    try:
        by_path: dict[str, list[dict[str, Any]]] = {}
        for item in index:
            by_path.setdefault(item["relative_path"], []).append(item)
        total = 0
        for path, entry in expected.items():
            target = directory / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories.update(
                p for p in target.parents if p == directory or directory in p.parents
            )
            digest = hashlib.sha256()
            written = 0
            with target.open("xb") as output:
                for item in by_path.get(path, []):
                    content = _decode_chunk(snapshot, item, fetch_chunk(item["seq"]))
                    output.write(content)
                    digest.update(content)
                    written += len(content)
                    total += len(content)
                if written != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise InputIntegrityError("Input file checksum or size is invalid.")
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o444)
        if total != snapshot["manifest"]["total_bytes"]:
            raise InputIntegrityError("Input total size does not match the manifest.")
        for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            path.chmod(0o555)
        return MaterializedSnapshot(
            directory,
            expected_snapshot_digest,
            len(expected),
            total,
            json.dumps(snapshot["manifest"], sort_keys=True, separators=(",", ":")),
        )
    except InputIntegrityError:
        cleanup()
        raise
    except _MALFORMED as error:
        cleanup()
        raise InputIntegrityError("Input bundle is malformed.") from error
    except BaseException:
        cleanup()
        raise
