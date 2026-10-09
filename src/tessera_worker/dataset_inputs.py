"""Bounded immutable ConfigMap custody; caller owns fresh authority checks/RBAC.

No ambient client, credentials, hostPath, cluster-wide cleanup or authority issuer.
The approved package must carry the returned descriptor before Job admission.
"""

import base64
import hashlib
import json
import re

from tessera_control.dataset_snapshot_models import DatasetManifest
from tessera_control.dataset_snapshots import manifest_bytes, verify_dataset_bytes

from .inputs import InputIntegrityError, _verify

CHUNK_BYTES = 640 * 1024  # raw <700 KiB; base64 plus metadata <1 MiB


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prepare_dataset_inputs(
    bundle,
    *,
    snapshot_digest,
    manifest_digest,
    lease_id,
    epoch,
    worker_id,
    operation_id,
    namespace,
    grant_id,
    project_id,
):
    if (
        bundle.get("receipt", {}).get("grant_id") != grant_id
        or not isinstance(grant_id, str)
        or not isinstance(project_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", project_id)
    ):
        raise InputIntegrityError("Dataset delivery does not match the approved grant/project")
    files = dict(_verify(bundle, snapshot_digest, lease_id, epoch, worker_id))
    if set(files) != {"dataset.csv", "dataset-manifest.json"}:
        raise InputIntegrityError("Dataset delivery must contain exactly two fixed files")
    encoded = files["dataset-manifest.json"]
    if hashlib.sha256(encoded).hexdigest() != manifest_digest or len(encoded) > 65536:
        raise InputIntegrityError("Dataset manifest target mismatch")
    observed = DatasetManifest.model_validate(json.loads(encoded))
    declaration = {k: getattr(observed, k) for k in ("version", "profile", "columns", "source")}
    verified = verify_dataset_bytes(
        files["dataset.csv"],
        declaration,
        expected_sha256=observed.data_sha256,
        expected_size=observed.data_size,
    )
    if manifest_bytes(verified) != encoded:
        raise InputIntegrityError("Dataset manifest is not canonical observed bytes")
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", namespace
    ):
        raise InputIntegrityError("Invalid resource scope")
    resources, parts = [], []
    for filename, content in sorted(files.items()):
        for offset in range(0, len(content), CHUNK_BYTES):
            chunk = content[offset : offset + CHUNK_BYTES]
            index = len(parts)
            name = f"tessera-data-{operation_id}-{index}"
            key = f"part-{index}"
            part = {
                "name": name,
                "key": key,
                "file": filename,
                "size": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            }
            parts.append(part)
            resources.append(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "immutable": True,
                    "metadata": {
                        "name": name,
                        "namespace": namespace,
                        "labels": {
                            "app.kubernetes.io/managed-by": "tessera",
                            "tessera.dev/operation": operation_id,
                            "tessera.dev/project": project_id,
                        },
                        "annotations": {
                            "tessera.dev/lease-id": lease_id,
                            "tessera.dev/grant-id": grant_id,
                            "tessera.dev/worker-id": worker_id,
                            "tessera.dev/epoch": str(epoch),
                            "tessera.dev/snapshot-sha256": snapshot_digest,
                            "tessera.dev/dataset-manifest-sha256": manifest_digest,
                        },
                    },
                    "binaryData": {key: base64.b64encode(chunk).decode("ascii")},
                }
            )
    descriptor = {
        "version": "tessera-job-dataset-v1",
        "snapshot_digest": snapshot_digest,
        "manifest_digest": manifest_digest,
        "parts": parts,
    }
    validate_descriptor(descriptor, operation_id)
    return descriptor, resources


def validate_descriptor(value, operation_id):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "snapshot_digest",
        "manifest_digest",
        "parts",
    }:
        raise ValueError("Malformed dataset descriptor")
    if value["version"] != "tessera-job-dataset-v1" or any(
        not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k])
        for k in ("snapshot_digest", "manifest_digest")
    ):
        raise ValueError("Invalid dataset identities")
    parts = value["parts"]
    # Seven data pieces plus one manifest cover the complete 4-MiB profile.
    if not isinstance(parts, list) or not 2 <= len(parts) <= 8:
        raise ValueError("Invalid chunk count")
    sizes = {"dataset.csv": 0, "dataset-manifest.json": 0}
    for i, part in enumerate(parts):
        if not isinstance(part, dict) or set(part) != {"name", "key", "file", "size", "sha256"}:
            raise ValueError("Invalid chunk fields")
        if (
            part["name"] != f"tessera-data-{operation_id}-{i}"
            or part["key"] != f"part-{i}"
            or part["file"] not in sizes
            or type(part["size"]) is not int
            or not 1 <= part["size"] <= CHUNK_BYTES
            or not isinstance(part["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", part["sha256"])
        ):
            raise ValueError("Invalid chunk identity or bound")
        sizes[part["file"]] += part["size"]
    if (
        not 1 <= sizes["dataset.csv"] <= 4 * 1024 * 1024
        or not 1 <= sizes["dataset-manifest.json"] <= 65536
    ):
        raise ValueError("Dataset sizes outside profile")


def create_inputs(resources, *, check_current_authority, create, read, record_receipt):
    """Synchronous compatibility wrapper for the cooperative custody protocol."""

    def create_steps(resource):
        return (yield create, (resource,))

    steps = create_input_steps(
        resources,
        check_current_authority=check_current_authority,
        create_steps=create_steps,
        read=read,
        record_receipt=record_receipt,
    )
    result = None
    while True:
        try:
            callback, arguments = steps.send(result)
        except StopIteration as done:
            return done.value
        result = callback(*arguments)


def create_input_steps(resources, *, check_current_authority, create_steps, read, record_receipt):
    """Fresh caller fence before every effect; retain UID receipts on partial failure.

    Caller durably retains each exact UID before validation/next side effect.
    """
    # Validate the entire set BEFORE any effect, even if a caller mutates the
    # adapter's dictionaries. This function can never create a Secret/Pod/etc.
    if not isinstance(resources, (tuple, list)) or not 2 <= len(resources) <= 8:
        raise InputIntegrityError("Immutable dataset resource count exceeds bounds")
    total = 0
    scope = None
    for index, resource in enumerate(resources):
        if (
            not isinstance(resource, dict)
            or set(resource) != {"apiVersion", "kind", "immutable", "metadata", "binaryData"}
            or resource["apiVersion"] != "v1"
            or resource["kind"] != "ConfigMap"
            or resource["immutable"] is not True
        ):
            raise InputIntegrityError("Only closed immutable ConfigMaps are supported")
        metadata = resource["metadata"]
        if not isinstance(metadata, dict) or set(metadata) != {
            "name",
            "namespace",
            "labels",
            "annotations",
        }:
            raise InputIntegrityError("Invalid immutable resource metadata")
        labels, annotations = metadata["labels"], metadata["annotations"]
        if (
            not isinstance(labels, dict)
            or set(labels)
            != {"app.kubernetes.io/managed-by", "tessera.dev/operation", "tessera.dev/project"}
            or labels["app.kubernetes.io/managed-by"] != "tessera"
            or not isinstance(labels["tessera.dev/operation"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", labels["tessera.dev/operation"])
            or not isinstance(labels["tessera.dev/project"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", labels["tessera.dev/project"])
            or not isinstance(annotations, dict)
            or set(annotations)
            != {
                "tessera.dev/lease-id",
                "tessera.dev/grant-id",
                "tessera.dev/worker-id",
                "tessera.dev/epoch",
                "tessera.dev/snapshot-sha256",
                "tessera.dev/dataset-manifest-sha256",
            }
            or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in annotations.values())
        ):
            raise InputIntegrityError("Invalid immutable resource ownership")
        if (
            metadata["name"] != f"tessera-data-{labels['tessera.dev/operation']}-{index}"
            or not isinstance(metadata["namespace"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", metadata["namespace"])
        ):
            raise InputIntegrityError("Invalid immutable resource name/namespace")
        current_scope = (metadata["namespace"], labels, annotations)
        if scope is not None and current_scope != scope:
            raise InputIntegrityError("Dataset resources span different custody")
        scope = current_scope
        binary = resource["binaryData"]
        if not isinstance(binary, dict) or set(binary) != {f"part-{index}"}:
            raise InputIntegrityError("Invalid immutable chunk keys")
        encoded = binary[f"part-{index}"]
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= ((CHUNK_BYTES + 2) // 3) * 4:
            raise InputIntegrityError("Immutable chunk exceeds bounds")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise InputIntegrityError("Immutable chunk encoding is malformed") from error
        if not 1 <= len(content) <= CHUNK_BYTES or base64.b64encode(content).decode() != encoded:
            raise InputIntegrityError("Immutable chunk is empty or noncanonical")
        total += len(content)
        if total > 4 * 1024 * 1024 + 65536 or len(json.dumps(resource).encode()) >= 1024 * 1024:
            raise InputIntegrityError("Immutable dataset resource exceeds profile")
    receipts = []
    for resource in resources:
        check_current_authority()
        observed = yield from create_steps(resource)
        metadata = observed.get("metadata", {})
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise InputIntegrityError(
                "Created ConfigMap UID unavailable; reconcile, never recreate"
            )
        receipt = {
            "name": resource["metadata"]["name"],
            "uid": uid,
            "namespace": resource["metadata"]["namespace"],
            "project_id": resource["metadata"]["labels"]["tessera.dev/project"],
            "grant_id": resource["metadata"]["annotations"]["tessera.dev/grant-id"],
        }
        record_receipt(receipt)
        check_current_authority()
        current = yield read, (resource["metadata"]["name"],)
        if (
            current.get("immutable") is not True
            or current.get("binaryData") != resource["binaryData"]
            or current.get("metadata", {}).get("uid") != uid
            or any(
                current.get("metadata", {}).get(k) != resource["metadata"][k]
                for k in ("name", "namespace", "labels", "annotations")
            )
        ):
            raise InputIntegrityError("Observed immutable resource conflicts; reconcile")
        receipts.append(receipt)
    return receipts


def delete_input(receipt, *, operation_id, read, delete_exact_uid):
    current = read(receipt["name"])
    metadata = current.get("metadata", {})
    if (
        metadata.get("uid") != receipt["uid"]
        or metadata.get("namespace") != receipt["namespace"]
        or metadata.get("labels", {}).get("tessera.dev/operation") != operation_id
        or metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != "tessera"
        or metadata.get("labels", {}).get("tessera.dev/project") != receipt.get("project_id")
        or metadata.get("annotations", {}).get("tessera.dev/grant-id") != receipt.get("grant_id")
    ):
        raise InputIntegrityError("Refuse cleanup of mismatched resource custody")
    delete_exact_uid(receipt["name"], receipt["uid"])
