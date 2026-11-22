"""Resumable transfer of retained bytes into control-plane quarantine.

The caller retains immutable bytes across retries. This component neither reads
container files nor decides whether outputs are scientifically valid.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from typing import Any, Protocol

from tessera_control import artifact_limits as limits

from .outputs import CollectedOutput, LargeOutput

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


class ArtifactChannel(Protocol):
    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]: ...


class ArtifactTransferError(ValueError):
    """Artifact input or a control receipt failed validation."""


def _key(path: str, body: dict[str, Any]) -> str:
    encoded = json.dumps([path, body], sort_keys=True, separators=(",", ":")).encode()
    return "artifact-" + hashlib.sha256(encoded).hexdigest()


def upload_artifact(
    channel: ArtifactChannel,
    *,
    lease_id: str,
    expected_epoch: int,
    relative_path: str,
    content: bytes,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Transfer at most 16 MiB; a later call resumes after uncertain responses.

    Transport errors propagate without retrying. Each mutating request uses a
    stable content-derived key; a fresh status request discovers committed
    progress after process restart or a lost response. The caller must schedule
    this outside the supervisor's cancellation/watchdog loop.
    """
    if not isinstance(lease_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", lease_id):
        raise ArtifactTransferError("Invalid lease identity.")
    if type(expected_epoch) is not int or not 1 <= expected_epoch <= 2**63 - 1:
        raise ArtifactTransferError("Invalid lease epoch.")
    if type(content) is not bytes or len(content) > MAX_ARTIFACT_BYTES:
        raise ArtifactTransferError("Artifact bytes exceed the transfer bound.")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path.encode()) > 4096
        or "\\" in relative_path
        or "\x00" in relative_path
        or len(relative_path.split("/")) > 64
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        or (len(relative_path) >= 2 and relative_path[1] == ":")
    ):
        raise ArtifactTransferError("Invalid artifact path.")
    digest = hashlib.sha256(content).hexdigest()
    if operation_id is None:
        operation_id = hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ArtifactTransferError("Invalid operation identity.")
    body = {
        "expected_epoch": expected_epoch,
        "operation_id": operation_id,
        "relative_path": relative_path,
        "sha256": digest,
        "size": len(content),
    }
    base = f"/worker/leases/{lease_id}/artifact-uploads"
    begun = channel.post(base, body, _key(base, body))
    upload_id = begun.get("id")
    if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", upload_id):
        raise ArtifactTransferError("Invalid artifact upload identity.")
    base += f"/{upload_id}"

    def progress(receipt: dict[str, Any]) -> int:
        if (
            receipt.get("id") != upload_id
            or receipt.get("lease_id") != lease_id
            or receipt.get("relative_path") != relative_path
            or receipt.get("sha256") != digest
            or type(receipt.get("size")) is not int
            or receipt["size"] != len(content)
            or receipt.get("status") not in {"uploading", "completed"}
            or type(receipt.get("received_bytes")) is not int
            or not 0 <= receipt["received_bytes"] <= len(content)
            or (receipt["status"] == "completed" and receipt["received_bytes"] != len(content))
        ):
            raise ArtifactTransferError("Artifact upload receipt does not match retained bytes.")
        return receipt["received_bytes"]

    progress(begun)
    status = channel.post(base + "/status", {}, "artifact-status-" + uuid.uuid4().hex)
    offset = progress(status)
    while offset < len(content):
        chunk = content[offset : offset + CHUNK_BYTES]
        payload = {"offset": offset, "content_base64": base64.b64encode(chunk).decode("ascii")}
        receipt = channel.post(base + "/chunks", payload, _key(base + "/chunks", payload))
        received = progress(receipt)
        if received != offset + len(chunk):
            raise ArtifactTransferError("Artifact chunk receipt has an unexpected offset.")
        offset = received
    receipt = channel.post(base + "/complete", {}, _key(base + "/complete", {}))
    if (
        receipt.get("lease_id") != lease_id
        or type(receipt.get("epoch")) is not int
        or receipt.get("epoch") != expected_epoch
        or receipt.get("operation_id") != operation_id
        or receipt.get("relative_path") != relative_path
        or receipt.get("sha256") != digest
        or type(receipt.get("size")) is not int
        or receipt["size"] != len(content)
        or receipt.get("status") != "quarantined"
        or receipt.get("classification") != "unclassified"
    ):
        raise ArtifactTransferError("Artifact completion receipt does not match retained bytes.")
    return receipt


def _validate_artifact_target(
    lease_id: str, expected_epoch: int, relative_path: str, operation_id: str | None
) -> str:
    if not isinstance(lease_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", lease_id):
        raise ArtifactTransferError("Invalid lease identity.")
    if type(expected_epoch) is not int or not 1 <= expected_epoch <= 2**63 - 1:
        raise ArtifactTransferError("Invalid lease epoch.")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path.encode()) > 4096
        or "\\" in relative_path
        or "\x00" in relative_path
        or len(relative_path.split("/")) > 64
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        or (len(relative_path) >= 2 and relative_path[1] == ":")
    ):
        raise ArtifactTransferError("Invalid artifact path.")
    if operation_id is None:
        operation_id = hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ArtifactTransferError("Invalid operation identity.")
    return operation_id


def upload_large_artifact(
    channel: ArtifactChannel,
    *,
    lease_id: str,
    expected_epoch: int,
    output: LargeOutput,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Stream one large file to control in fixed-size chunks with bounded memory.

    The file is re-opened from its reference, its identity re-verified and the whole
    content re-hashed (one chunk at a time) before any byte is sent; a changed file fails
    closed. Progress is resumed from the control's ``next_seq``; each chunk carries its own
    sha256 and control verifies the whole-file sha256 on completion.
    """
    if not isinstance(output, LargeOutput):
        raise ArtifactTransferError("Invalid large artifact reference.")
    relative_path = output.relative_path
    operation_id = _validate_artifact_target(lease_id, expected_epoch, relative_path, operation_id)
    size = output.size
    if (
        type(size) is not int
        or not limits.LEGACY_INLINE_BYTES < size <= limits.ABSOLUTE_MAX_ARTIFACT_BYTES
        or not isinstance(output.sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", output.sha256)
    ):
        raise ArtifactTransferError("Large artifact size or checksum is invalid.")
    digest = output.sha256
    # A changed or unreadable file raises OutputCollectionError (fail closed, nothing sent).
    with output.open() as handle:
        handle.verify_hash()
        return _stream_large(
            channel, handle, lease_id, expected_epoch, operation_id, relative_path, digest, size
        )


def _part_bytes(channel: Any, begun: dict[str, Any], chunk_bytes: int) -> int:
    """Raw bytes to send per request: what control and this worker's own channel can both carry.

    Control advertises its request limit and the largest part that fits it; the channel may bound
    a request lower (``max_request_bytes``). The smaller wins, with headroom for the envelope. If
    neither a whole chunk nor any part fits, the upload fails fast with the numbers, instead of
    being refused (or retried) chunk after chunk."""
    own = getattr(channel, "max_request_bytes", None)
    theirs = begun.get("max_request_bytes")
    advertised = begun.get("part_bytes")
    part = chunk_bytes
    if type(advertised) is int and advertised >= limits.MIN_PART_BYTES:
        part = min(part, advertised)
    elif advertised is not None:
        raise ArtifactTransferError("Artifact upload part size is not acceptable.")
    if type(own) is int and own > 0:
        part = min(part, limits.part_bytes_for(own))
    bound = (
        min(value for value in (own, theirs) if type(value) is int and value > 0)
        if any(type(value) is int and value > 0 for value in (own, theirs))
        else None
    )
    if bound is not None and limits.request_bytes_for_part(part) > bound:
        raise ArtifactTransferError(
            f"No upload part fits the request limit: the smallest request bound on the path is "
            f"{bound} bytes (this worker's channel: {own}, control: {theirs}) and a part of "
            f"{part} bytes needs {limits.request_bytes_for_part(part)}. Raise the bound, or "
            "upgrade control so it accepts part uploads; the upload is not retried."
        )
    return part


def _stream_large(
    channel: ArtifactChannel,
    handle: Any,
    lease_id: str,
    expected_epoch: int,
    operation_id: str,
    relative_path: str,
    digest: str,
    size: int,
) -> dict[str, Any]:
    body = {
        "expected_epoch": expected_epoch,
        "operation_id": operation_id,
        "relative_path": relative_path,
        "sha256": digest,
        "size": size,
    }
    base = f"/worker/leases/{lease_id}/large-artifact-uploads"
    begun = channel.post(base, body, _key(base, body))
    upload_id = begun.get("id")
    if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", upload_id):
        raise ArtifactTransferError("Invalid artifact upload identity.")
    base += f"/{upload_id}"
    chunk_bytes = begun.get("chunk_bytes")
    if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= limits.CHUNK_BYTES:
        raise ArtifactTransferError("Artifact upload chunk size is not acceptable.")
    chunk_count = -(-size // chunk_bytes)
    part_bytes = _part_bytes(channel, begun, chunk_bytes)

    def progress(receipt: dict[str, Any]) -> int:
        next_seq = receipt.get("next_seq")
        received = receipt.get("received_chunks")
        if (
            receipt.get("id") != upload_id
            or receipt.get("lease_id") != lease_id
            or receipt.get("relative_path") != relative_path
            or receipt.get("sha256") != digest
            or type(receipt.get("size")) is not int
            or receipt["size"] != size
            or receipt.get("chunk_bytes") != chunk_bytes
            or receipt.get("chunk_count") != chunk_count
            or receipt.get("status") not in {"uploading", "completed"}
            or type(received) is not int
            or type(next_seq) is not int
            or not 0 <= next_seq <= received <= chunk_count
            or (receipt["status"] == "completed" and next_seq != chunk_count)
        ):
            raise ArtifactTransferError("Artifact upload receipt does not match retained bytes.")
        return next_seq

    progress(begun)
    seq = progress(channel.post(base + "/status", {}, "artifact-status-" + uuid.uuid4().hex))
    while seq < chunk_count:
        chunk = handle.read_chunk(seq * chunk_bytes, chunk_bytes)
        chunk_digest = hashlib.sha256(chunk).hexdigest()
        chunk_path = f"{base}/chunks/{seq}"
        if len(chunk) <= part_bytes:
            parts: list[tuple[int | None, bytes]] = [(None, chunk)]
        else:  # the path bounds a request below one chunk: send it in order, in parts
            parts = [(at, chunk[at : at + part_bytes]) for at in range(0, len(chunk), part_bytes)]
        for at, piece in parts:
            payload: dict[str, Any] = {
                "content_base64": base64.b64encode(piece).decode("ascii"),
                "sha256": chunk_digest,
            }
            if at is not None:
                payload["part_offset"] = at
            try:
                receipt = channel.post(
                    chunk_path,
                    payload,
                    _key(chunk_path, {"sha256": chunk_digest, "size": len(piece), "at": at}),
                )
            except ConnectionError as error:
                if getattr(error, "status", None) == 413:
                    raise ArtifactTransferError(
                        f"Control refused an upload part of {len(piece)} bytes as larger than its "
                        "request limit (HTTP 413). Set the operator's TESSERA_MAX_REQUEST_BYTES "
                        "to the smallest request size on the path to control; the upload is not "
                        "retried."
                    ) from None
                raise
            last = at is None or at + len(piece) == len(chunk)
            if not last and (
                receipt.get("seq") != seq or receipt.get("chunk_received_bytes") != at + len(piece)
            ):
                raise ArtifactTransferError("Artifact chunk part receipt does not match.")
        if (
            receipt.get("seq") != seq
            or type(receipt.get("next_seq")) is not int
            # Control may already hold later chunks (an older worker, an out-of-order retry):
            # next_seq then jumps past seq + 1. Every chunk is still sent, so a stored chunk
            # that differs from the file is refused by control instead of being skipped.
            or not seq + 1 <= receipt["next_seq"] <= chunk_count
            or type(receipt.get("received_chunks")) is not int
            or not seq < receipt["received_chunks"] <= chunk_count
        ):
            raise ArtifactTransferError("Artifact chunk receipt has an unexpected sequence.")
        seq += 1
    handle.assert_unchanged()
    receipt = channel.post(base + "/complete", {}, _key(base + "/complete", {}))
    if (
        receipt.get("lease_id") != lease_id
        or type(receipt.get("epoch")) is not int
        or receipt.get("epoch") != expected_epoch
        or receipt.get("operation_id") != operation_id
        or receipt.get("relative_path") != relative_path
        or receipt.get("sha256") != digest
        or type(receipt.get("size")) is not int
        or receipt["size"] != size
        or receipt.get("status") != "quarantined"
        or receipt.get("classification") != "unclassified"
    ):
        raise ArtifactTransferError("Artifact completion receipt does not match retained bytes.")
    return receipt


def upload_output_set(
    channel: ArtifactChannel,
    *,
    lease_id: str,
    expected_epoch: int,
    outputs: tuple[CollectedOutput | LargeOutput, ...],
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Upload retained outputs and record a complete quarantined manifest.

    The caller owns custody, termination checks and durable retention. Retrying
    with the same immutable tuple resumes individual transfers and manifest
    publication. Completeness is checked against the approved plan by control.
    This helper does not run in the cancellation loop or accept research results.
    """
    if type(outputs) is not tuple or len(outputs) > 128:
        raise ArtifactTransferError("Invalid retained output set.")
    if (
        not isinstance(lease_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", lease_id)
        or type(expected_epoch) is not int
        or not 1 <= expected_epoch <= 2**63 - 1
    ):
        raise ArtifactTransferError("Invalid lease identity or epoch.")
    seen: set[str] = set()
    if operation_id is None:
        operation_id = hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ArtifactTransferError("Invalid operation identity.")
    total = 0
    inline_total = 0
    for output in outputs:
        if isinstance(output, LargeOutput):
            if (
                not isinstance(output.relative_path, str)
                or output.relative_path in seen
                or type(output.size) is not int
                or not limits.LEGACY_INLINE_BYTES
                < output.size
                <= limits.ABSOLUTE_MAX_ARTIFACT_BYTES
            ):
                raise ArtifactTransferError("Invalid retained output set.")
            total += output.size
            if total > limits.ABSOLUTE_MAX_ARTIFACT_BYTES:
                raise ArtifactTransferError("Retained output size or checksum is invalid.")
            seen.add(output.relative_path)
            continue
        if (
            not isinstance(output, CollectedOutput)
            or type(output.content) is not bytes
            or not isinstance(output.relative_path, str)
            or output.relative_path in seen
        ):
            raise ArtifactTransferError("Invalid retained output set.")
        total += len(output.content)
        inline_total += len(output.content)
        if (
            inline_total > MAX_ARTIFACT_BYTES
            or hashlib.sha256(output.content).hexdigest() != output.sha256
        ):
            raise ArtifactTransferError("Retained output size or checksum is invalid.")
        seen.add(output.relative_path)
    entries = []
    for output in sorted(outputs, key=lambda item: item.relative_path):
        if isinstance(output, LargeOutput):
            receipt = upload_large_artifact(
                channel,
                lease_id=lease_id,
                expected_epoch=expected_epoch,
                output=output,
                operation_id=operation_id,
            )
            size = output.size
        else:
            receipt = upload_artifact(
                channel,
                lease_id=lease_id,
                expected_epoch=expected_epoch,
                relative_path=output.relative_path,
                content=output.content,
                operation_id=operation_id,
            )
            size = len(output.content)
        artifact_id = receipt.get("id")
        if not isinstance(artifact_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", artifact_id
        ):
            raise ArtifactTransferError("Invalid retained artifact identity.")
        entries.append(
            {
                "id": artifact_id,
                "relative_path": output.relative_path,
                "sha256": output.sha256,
                "size": size,
            }
        )
    # Large artifacts are exempt from project placement (control stores them chunked).
    placed = [item for item in entries if item["size"] <= limits.LEGACY_INLINE_BYTES]
    # An all-large set has nothing to place (control exempts large artifacts).
    if placed or not entries:
        placement_body = {
            "expected_epoch": expected_epoch,
            "operation_id": operation_id,
            "artifacts": [
                {name: item[name] for name in ("id", "sha256", "size")} for item in placed
            ],
        }
        placement_path = f"/worker/leases/{lease_id}/project-artifacts"
        placement = channel.post(
            placement_path, placement_body, _key(placement_path, placement_body)
        )
        receipts = placement.get("placements")
        if (
            not isinstance(receipts, list)
            or len(receipts) != len(placed)
            or {
                (item.get("artifact_id"), item.get("sha256"), item.get("size"), item.get("state"))
                for item in receipts
            }
            != {(item["id"], item["sha256"], item["size"], "committed") for item in placed}
        ):
            raise ArtifactTransferError(
                "Project artifact placement receipt does not match retained bytes."
            )
    body = {"expected_epoch": expected_epoch, "operation_id": operation_id, "artifacts": entries}
    path = f"/worker/leases/{lease_id}/artifact-manifest"
    receipt = channel.post(path, body, _key(path, body))
    if (
        receipt.get("lease_id") != lease_id
        or type(receipt.get("epoch")) is not int
        or receipt.get("epoch") != expected_epoch
        or receipt.get("operation_id") != operation_id
        or receipt.get("artifacts") != entries
        or type(receipt.get("total_bytes")) is not int
        or receipt["total_bytes"] != total
        or receipt.get("status") != "quarantined"
        or receipt.get("classification") != "unclassified"
    ):
        raise ArtifactTransferError("Output manifest receipt does not match retained outputs.")
    canonical = {"version": 1}
    for name in (
        "project_id",
        "task_id",
        "grant_id",
        "lease_id",
        "worker_principal_id",
        "epoch",
        "operation_id",
        "plan_digest",
        "artifacts",
    ):
        if name not in receipt:
            raise ArtifactTransferError("Output manifest receipt is incomplete.")
        canonical[name] = receipt[name]
    # Hash our typed entries, rather than relying on Python's equality between
    # booleans and integers in an otherwise matching response object.
    canonical["artifacts"] = entries
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != receipt.get("manifest_digest"):
        raise ArtifactTransferError("Output manifest checksum is invalid.")
    return {**receipt, "project_placements_committed": True}
