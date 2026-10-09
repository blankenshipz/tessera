"""Byte verification and existing snapshot envelopes; no API or authority bypass."""

import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import stat
from pathlib import Path

from .dataset_snapshot_models import (
    MAX_DATA_BYTES,
    MAX_DATA_ROWS,
    MAX_MANIFEST_BYTES,
    DatasetDeclaration,
    DatasetManifest,
    DatasetSplit,
    UploadDatasetSnapshot,
)
from .snapshots import SnapshotStore
from .store import Conflict, Forbidden, StoreError, _connect
from .workspace import CapturedWorkspace, InventoryEntry, WorkspaceInventory, inventory_digest


class DatasetIntegrityError(ValueError):
    """A data target, parse or retained snapshot did not pass byte verification."""


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def manifest_bytes(manifest: DatasetManifest) -> bytes:
    return canonical_bytes(manifest.model_dump(mode="json"))


def verify_dataset_bytes(content, declaration, *, expected_sha256, expected_size):
    """Recompute every observed fact from immutable captured bytes, not metadata echoes."""
    d = DatasetDeclaration.model_validate(declaration)
    if (
        not isinstance(content, bytes)
        or type(expected_size) is not int
        or not 1 <= len(content) == expected_size <= MAX_DATA_BYTES
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
        or hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise DatasetIntegrityError("Dataset bytes do not match the reviewed bounded target")
    try:
        text = content.decode("utf-8", errors="strict")
        if text.startswith("\ufeff") or "\0" in text:
            raise DatasetIntegrityError("UTF8 CSV must not contain BOM or NUL")
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows)
        if header != [c.name for c in d.columns]:
            raise DatasetIntegrityError("CSV header differs from the declared exact schema")
        groups = {label: [] for label in ("train", "validation", "test")}
        seen = set()
        count = 0
        for row in rows:
            count += 1
            if count > MAX_DATA_ROWS or len(row) != len(header):
                raise DatasetIntegrityError("CSV row count or width is outside bounds")
            ident = split = None
            for column, cell in zip(d.columns, row, strict=True):
                if (
                    not cell
                    or len(cell.encode("utf-8")) > 1024
                    or any(ord(c) < 32 or ord(c) == 127 for c in cell)
                ):
                    raise DatasetIntegrityError("CSV cell is empty, oversized or contains controls")
                if column.role == "id":
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", cell):
                        raise DatasetIntegrityError("Row ID is invalid")
                    ident = cell
                elif column.role == "split":
                    if cell not in groups:
                        raise DatasetIntegrityError("Unknown split label")
                    split = cell
                elif column.type == "integer":
                    if not re.fullmatch(r"[+-]?[0-9]+", cell) or not -(2**63) <= int(cell) < 2**63:
                        raise DatasetIntegrityError("Integer cell is invalid or outside int64")
                elif (
                    cell != cell.strip()
                    or not re.fullmatch(
                        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", cell
                    )
                    or not math.isfinite(float(cell))
                ):
                    raise DatasetIntegrityError("Numeric cell must be finite and well formed")
            if ident in seen:
                raise DatasetIntegrityError("Row IDs must be unique across splits")
            seen.add(ident)
            groups[split].append(row)
        if count < 3 or any(not rows for rows in groups.values()):
            raise DatasetIntegrityError("All three immutable splits must contain rows")
    except (UnicodeError, csv.Error, StopIteration, OverflowError) as error:
        raise DatasetIntegrityError("Dataset is not valid bounded UTF8 CSV") from error
    return DatasetManifest(
        **d.model_dump(mode="python"),
        data_path="dataset.csv",
        data_sha256=expected_sha256,
        data_size=len(content),
        row_count=count,
        splits=tuple(
            DatasetSplit(
                label=label,
                row_count=len(values),
                sha256=hashlib.sha256(
                    canonical_bytes({"columns": header, "rows": values})
                ).hexdigest(),
            )
            for label, values in groups.items()
        ),
        source_verification="declared-not-independently-verified",
        license_verification="declared-not-legally-verified",
        scientific_validation="not-established",
    )


def build_dataset_capture(
    content, declaration, *, expected_sha256, expected_size, expected_manifest_digest
):
    manifest = verify_dataset_bytes(
        content, declaration, expected_sha256=expected_sha256, expected_size=expected_size
    )
    encoded = manifest_bytes(manifest)
    if (
        len(encoded) > MAX_MANIFEST_BYTES
        or hashlib.sha256(encoded).hexdigest() != expected_manifest_digest
    ):
        raise DatasetIntegrityError("Observed manifest differs from the reviewed manifest digest")
    files = (("dataset-manifest.json", encoded), ("dataset.csv", content))
    entries = tuple(
        InventoryEntry(path, len(data), hashlib.sha256(data).hexdigest()) for path, data in files
    )
    return CapturedWorkspace(
        WorkspaceInventory(
            "tessera-dataset-snapshot-v1", entries, (), sum(len(data) for _, data in files)
        ),
        files,
    )


def _read_file(root: Path, relative_path: str, limit: int):
    """Anchor every component with no-follow FDs; reject mutation during reading."""
    root = Path(root)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not isinstance(relative_path, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", relative_path)
        or relative_path in {".", ".."}
    ):
        raise DatasetIntegrityError("Dataset path must be anchored and safe")
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", directory)
    file_fd = None
    try:
        for part in root.parts[1:]:
            next_fd = os.open(part, directory, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(relative_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= limit:
            raise DatasetIntegrityError("Dataset file must be a bounded regular file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(file_fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise DatasetIntegrityError("Dataset file grew beyond bounds")
        after = os.fstat(file_fd)

        def identity(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

        current = os.stat(relative_path, dir_fd=fd, follow_symlinks=False)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise DatasetIntegrityError("Dataset file changed while reading")
        return b"".join(chunks)
    except OSError as error:
        raise DatasetIntegrityError("Dataset path cannot be safely opened") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(fd)


def verify_dataset_file(root, relative_path, declaration, *, expected_sha256, expected_size):
    return verify_dataset_bytes(
        _read_file(Path(root), relative_path, MAX_DATA_BYTES),
        declaration,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )


def verify_dataset_snapshot_bytes(content, encoded, *, expected_manifest_digest):
    """Verify retained profile against exact bounded bytes, independent of filesystem."""
    if not isinstance(encoded, bytes) or not 1 <= len(encoded) <= MAX_MANIFEST_BYTES:
        raise DatasetIntegrityError("Retained manifest exceeds bounds")
    if hashlib.sha256(encoded).hexdigest() != expected_manifest_digest:
        raise DatasetIntegrityError("Retained manifest digest differs from the approved target")
    try:
        manifest = DatasetManifest.model_validate(json.loads(encoded))
    except (ValueError, UnicodeError) as error:
        raise DatasetIntegrityError("Retained manifest is invalid") from error
    if encoded != manifest_bytes(manifest):
        raise DatasetIntegrityError("Retained manifest must be canonical JSON")
    declaration = {
        key: manifest.model_dump(mode="json")[key] for key in DatasetDeclaration.model_fields
    }
    observed = verify_dataset_bytes(
        content,
        declaration,
        expected_sha256=manifest.data_sha256,
        expected_size=manifest.data_size,
    )
    if manifest_bytes(observed) != encoded:
        raise DatasetIntegrityError(
            "Retained manifest observation differs from actual dataset bytes"
        )
    return observed, content


def load_dataset_snapshot(root, *, expected_manifest_digest):
    """Return observed manifest AND the exact verified immutable bytes for consumption."""
    encoded = _read_file(Path(root), "dataset-manifest.json", MAX_MANIFEST_BYTES)
    content = _read_file(Path(root), "dataset.csv", MAX_DATA_BYTES)
    return verify_dataset_snapshot_bytes(
        content, encoded, expected_manifest_digest=expected_manifest_digest
    )


def read_dataset_snapshot(root, *, expected_manifest_digest):
    return load_dataset_snapshot(root, expected_manifest_digest=expected_manifest_digest)[0]


class DatasetSnapshotStore(SnapshotStore):
    """Existing retained snapshot storage, human adoption, no parallel blob ledger."""

    @staticmethod
    def _dataset(c, actor, snapshot_id):
        snapshot = SnapshotStore._response(SnapshotStore._snapshot(c, actor, snapshot_id))
        inventory = snapshot["manifest"]
        limits = {"dataset.csv": MAX_DATA_BYTES, "dataset-manifest.json": MAX_MANIFEST_BYTES}
        if (
            inventory["root"] != "tessera-dataset-snapshot-v1"
            or inventory["exclusions"]
            or {e["relative_path"] for e in inventory["entries"]} != set(limits)
            or any(not 1 <= e["size"] <= limits[e["relative_path"]] for e in inventory["entries"])
        ):
            raise Conflict("Snapshot is not a bounded tabular dataset profile")
        SnapshotStore._verify_retained(c, actor, snapshot_id)
        files = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT relative_path,content FROM snapshot_files WHERE snapshot_id=?",
                (snapshot_id,),
            )
        }
        manifest_digest = next(
            e["sha256"]
            for e in inventory["entries"]
            if e["relative_path"] == "dataset-manifest.json"
        )
        try:
            observed, _ = verify_dataset_snapshot_bytes(
                files["dataset.csv"],
                files["dataset-manifest.json"],
                expected_manifest_digest=manifest_digest,
            )
        except (ValueError, KeyError, TypeError) as error:
            raise Conflict("Retained dataset profile integrity check failed") from error
        return {
            **snapshot,
            "dataset_manifest_digest": manifest_digest,
            "dataset_manifest": observed.model_dump(mode="json"),
        }

    def adopt_dataset(self, actor, project_id, key, payload):
        if actor["role"] != "human":
            raise Forbidden("A human must confirm dataset snapshot adoption")
        request = UploadDatasetSnapshot.model_validate(payload)
        try:
            content = base64.b64decode(request.content_base64, validate=True)
            if base64.b64encode(content).decode("ascii") != request.content_base64:
                raise ValueError("Noncanonical base64")
        except ValueError as error:
            raise StoreError("Dataset upload requires canonical base64 bytes") from error
        try:
            capture = build_dataset_capture(
                content,
                request.declaration,
                expected_sha256=request.expected_data_sha256,
                expected_size=request.expected_data_size,
                expected_manifest_digest=request.expected_manifest_digest,
            )
        except DatasetIntegrityError as error:
            raise Conflict(
                "Dataset bytes/schema/manifest differ from the reviewed target"
            ) from error
        if inventory_digest(capture.inventory) != request.expected_input_snapshot_digest:
            raise Conflict("Dataset retained-input envelope differs from the reviewed target")

        def replay(c, live, result):
            self._project(c, live, project_id)
            if live["role"] != "human":
                raise Forbidden("A human must confirm dataset snapshot adoption")
            self._dataset(c, live, result["id"])

        status, result = self.adopt_snapshot(
            actor,
            project_id,
            key,
            request.expected_input_snapshot_digest,
            capture,
            replay_guard=replay,
        )
        observed = verify_dataset_bytes(
            content,
            request.declaration,
            expected_sha256=request.expected_data_sha256,
            expected_size=request.expected_data_size,
        )
        return status, {
            **result,
            "dataset_manifest_digest": request.expected_manifest_digest,
            "dataset_manifest": observed.model_dump(mode="json"),
        }

    def get_dataset(self, actor, snapshot_id):
        c = _connect(self.database_path)
        try:
            c.execute("BEGIN")
            return self._dataset(c, self._live_principal(c, actor), snapshot_id)
        finally:
            c.close()
