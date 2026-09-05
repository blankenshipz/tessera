"""Verify released input bundles and stage bytes in trusted supervisor custody.

This module grants no authority. The caller must obtain the bundle over the
authenticated control channel and supply the expected lease and plan digest.
The destination parent belongs to the trusted supervisor, never an experiment.
Read-only permissions prevent accidental writes, not modification by its owner.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 1000
MAX_MANIFEST_BYTES = 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class InputIntegrityError(ValueError):
    """A delivered bundle does not match its pinned input contract."""


@dataclass(frozen=True)
class MaterializedSnapshot:
    directory: Path
    snapshot_digest: str
    file_count: int
    total_bytes: int
    manifest_json: str


def _path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 4096
        or len(value.split("/")) > 64
        or "\\" in value
        or "\0" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} or len(part.encode()) > 255 for part in value.split("/"))
    ):
        raise InputIntegrityError("Input path is not a bounded relative file path.")
    return value


def _verify(
    bundle: dict[str, Any],
    expected_snapshot_digest: str,
    expected_lease_id: str,
    expected_epoch: int,
    expected_worker_id: str,
) -> list[tuple[str, bytes]]:
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
        ):
            raise InputIntegrityError("Bundle does not match the expected lease and snapshot.")
        manifest = snapshot["manifest"]
        encoded_manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if (
            len(encoded_manifest) > MAX_MANIFEST_BYTES
            or hashlib.sha256(encoded_manifest).hexdigest() != expected_snapshot_digest
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
        entries, files = manifest["entries"], snapshot["files"]
        if (
            not isinstance(entries, list)
            or not isinstance(files, list)
            or len(entries) > MAX_FILES
            or len(files) != len(entries)
        ):
            raise InputIntegrityError("Input file set exceeds bounds or is incomplete.")
        expected = {}
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
        verified = []
        seen: set[str] = set()
        total = 0
        for item in files:
            path = _path(item["relative_path"])
            encoded = item["content_base64"]
            if (
                set(item) != {"relative_path", "content_base64"}
                or path not in expected
                or path in seen
            ):
                raise InputIntegrityError("Input file set does not match the manifest.")
            entry = expected[path]
            if not isinstance(encoded, str) or len(encoded) != ((entry["size"] + 2) // 3) * 4:
                raise InputIntegrityError("Encoded input size does not match the manifest.")
            content = base64.b64decode(encoded, validate=True)
            total += len(content)
            if (
                total > MAX_BYTES
                or len(content) != entry["size"]
                or hashlib.sha256(content).hexdigest() != entry["sha256"]
                or base64.b64encode(content).decode("ascii") != encoded
            ):
                raise InputIntegrityError("Input file checksum or size is invalid.")
            seen.add(path)
            verified.append((path, content))
        if total != manifest["total_bytes"]:
            raise InputIntegrityError("Input total size does not match the manifest.")
        return verified
    except InputIntegrityError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        binascii.Error,
        UnicodeError,
        RecursionError,
    ) as error:
        raise InputIntegrityError("Input bundle is malformed.") from error


def materialize_snapshot(
    bundle: dict[str, Any],
    parent: Path,
    *,
    expected_snapshot_digest: str,
    expected_lease_id: str,
    expected_epoch: int,
    expected_worker_id: str,
) -> MaterializedSnapshot:
    """Validate everything before creating a fresh, non-executable input tree.

    No archive extraction, source-root resolution, existing-directory merge or
    shell execution occurs. An incomplete tree is removed if staging fails.
    The parent must be an existing private directory controlled by the caller.
    """
    if os.name != "posix":
        raise InputIntegrityError("Input staging requires POSIX filesystem permissions.")
    files = _verify(
        bundle, expected_snapshot_digest, expected_lease_id, expected_epoch, expected_worker_id
    )
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
    try:
        for relative, content in files:
            target = directory / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories.update(
                p for p in target.parents if p == directory or directory in p.parents
            )
            with target.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
                # The bind mount is entered at ``directory``; its private 0700
                # parent remains outside the container.  The non-root workload
                # must be able to traverse and read this verified tree.
                os.fchmod(output.fileno(), 0o444)
        for path in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            path.chmod(0o555)
        return MaterializedSnapshot(
            directory,
            expected_snapshot_digest,
            len(files),
            sum(len(content) for _, content in files),
            json.dumps(bundle["snapshot"]["manifest"], sort_keys=True, separators=(",", ":")),
        )
    except BaseException:
        for path in directories:
            path.chmod(0o700)
        shutil.rmtree(directory)
        raise
