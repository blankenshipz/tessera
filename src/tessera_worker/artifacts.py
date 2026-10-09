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

from .outputs import CollectedOutput

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


def upload_output_set(
    channel: ArtifactChannel,
    *,
    lease_id: str,
    expected_epoch: int,
    outputs: tuple[CollectedOutput, ...],
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
    for output in outputs:
        if (
            not isinstance(output, CollectedOutput)
            or type(output.content) is not bytes
            or not isinstance(output.relative_path, str)
            or output.relative_path in seen
        ):
            raise ArtifactTransferError("Invalid retained output set.")
        total += len(output.content)
        if (
            total > MAX_ARTIFACT_BYTES
            or hashlib.sha256(output.content).hexdigest() != output.sha256
        ):
            raise ArtifactTransferError("Retained output size or checksum is invalid.")
        seen.add(output.relative_path)
    entries = []
    for output in sorted(outputs, key=lambda item: item.relative_path):
        receipt = upload_artifact(
            channel,
            lease_id=lease_id,
            expected_epoch=expected_epoch,
            relative_path=output.relative_path,
            content=output.content,
            operation_id=operation_id,
        )
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
                "size": len(output.content),
            }
        )
    placement_body = {
        "expected_epoch": expected_epoch,
        "operation_id": operation_id,
        "artifacts": [{name: item[name] for name in ("id", "sha256", "size")} for item in entries],
    }
    placement_path = f"/worker/leases/{lease_id}/project-artifacts"
    placement = channel.post(placement_path, placement_body, _key(placement_path, placement_body))
    receipts = placement.get("placements")
    if (
        not isinstance(receipts, list)
        or len(receipts) != len(entries)
        or {
            (item.get("artifact_id"), item.get("sha256"), item.get("size"), item.get("state"))
            for item in receipts
        }
        != {(item["id"], item["sha256"], item["size"], "committed") for item in entries}
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
