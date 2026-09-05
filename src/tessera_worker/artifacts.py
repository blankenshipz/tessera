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
    operation_id = hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
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
