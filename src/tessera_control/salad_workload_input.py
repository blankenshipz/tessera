"""Owner-side staged input for a workload_v1 route (ADR 0036 phase 2, ADR 0037).

Generic and offline. Inventories an explicit input directory into the chunked
snapshot manifest control stores (fixed root label, so the digest does not
depend on where the directory lives), and checks it against the reviewed
contract before anything is uploaded: the digest must equal
``input.snapshot_digest`` (which the selection binds to ``manifests.data``), the
total must fit ``input.max_bytes``, and a gpu_runtime_v1 manifest carried in the
snapshot must hash to the argv MANIFEST_SHA256 with rows equal to
``egress.fetches``. Every input-sourced manifest item (inline or carried) must
name a file of the directory.
"""

import hashlib
import json
import os
from dataclasses import asdict

from .salad_workload_contract import (
    GPU_RUNTIME_V1,
    MAX_INPUT_BYTES,
    check_gpu_runtime_inline_rules,
    check_gpu_runtime_manifest,
    check_gpu_runtime_manifest_rules,
    check_input_items_staged,
    gpu_runtime_input_manifest_path,
)
from .workspace import WorkspaceError, inventory_workspace

INPUT_ROOT_LABEL = "input"
MAX_INPUT_FILES = 10_000
CHUNK_BYTES = 4 * 1024 * 1024  # equals tessera_worker.chunked_inputs.CHUNK_BYTES
MAX_RUNTIME_MANIFEST_BYTES = 262144  # gpu_runtime_v1 file-manifest cap


def staged_input_manifest(directory, max_bytes=MAX_INPUT_BYTES):
    """Return ``(manifest, digest)`` for ``directory`` as a chunked snapshot inventory.

    The walk never follows links. Any exclusion (credential-like names, special
    files) is refused rather than silently dropped: the reviewed digest must
    describe exactly the directory the operator staged.
    """
    try:
        inventory = inventory_workspace(
            directory,
            max_files=MAX_INPUT_FILES,
            max_file_bytes=max_bytes,
            max_total_bytes=max_bytes,
        )
    except WorkspaceError as error:
        raise ValueError(f"input directory refused: {error}") from None
    if inventory.exclusions:
        raise ValueError("input directory has excluded entries; stage only the input files")
    manifest = dict(
        root=INPUT_ROOT_LABEL,
        entries=[asdict(entry) for entry in inventory.entries],
        exclusions=[],
        total_bytes=inventory.total_bytes,
    )
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return manifest, hashlib.sha256(raw.encode()).hexdigest()


def read_staged_file(directory, relative, cap):
    """Read one regular file below ``directory`` without following any link."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(directory, flags | os.O_DIRECTORY)
    try:
        for part in relative.split("/")[:-1]:
            next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        handle = os.open(relative.split("/")[-1], flags, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(handle, "rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError("staged file exceeds its cap")
    return data


def check_staged_input(contract, directory, *, approved_code=None):
    """Validate ``directory`` against the contract's input declaration; return the manifest.

    ``approved_code`` is the set of paths the owner approves as private code (ADR 0039,
    amendment 1); when given, an input-sourced entry must be one of them. Returns
    ``(manifest, digest)``. Raises ValueError with an operator-readable reason.
    """
    declared = contract.input
    if declared is None:
        raise ValueError("the workload contract declares no input")
    manifest, digest = staged_input_manifest(directory, declared.max_bytes)
    if digest != declared.snapshot_digest:
        raise ValueError(
            "input directory digest does not equal the contract's input.snapshot_digest"
        )
    if manifest["total_bytes"] > declared.max_bytes:
        raise ValueError("input directory exceeds the contract's input.max_bytes")
    from .salad_workload_admission import input_manifest_reply_bytes
    from .salad_workload_contract import INPUT_REPLY_BYTES

    # Snapshot ids are "snapshot_" + 32 hex, so this placeholder has the stored size.
    if input_manifest_reply_bytes("snapshot_" + "0" * 32, digest, manifest) > INPUT_REPLY_BYTES:
        raise ValueError("too many input files: the manifest and chunk index exceed one reply")
    if contract.runtime != GPU_RUNTIME_V1:
        return manifest, digest
    path = gpu_runtime_input_manifest_path(contract)
    runtime_manifest = check_gpu_runtime_inline_rules(contract)
    if path is not None:
        entry = next((e for e in manifest["entries"] if e["relative_path"] == path), None)
        if entry is None:
            raise ValueError("the runtime manifest named by argv is not in the input")
        raw = read_staged_file(directory, path, MAX_RUNTIME_MANIFEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("the runtime manifest changed while being checked")
        runtime_manifest = check_gpu_runtime_manifest(contract, raw, contract.argv_template[8])
        if runtime_manifest is not None:
            check_gpu_runtime_manifest_rules(runtime_manifest, True, contract.egress.redirect_hosts)
    if runtime_manifest is not None:
        check_input_items_staged(runtime_manifest, manifest["entries"])
        if approved_code:
            from .code_approval import check_entry_items_approved

            check_entry_items_approved(runtime_manifest, approved_code)
    return manifest, digest


def staged_chunks(directory, manifest):
    """Yield ``(seq, content)`` for every CHUNK_BYTES chunk in manifest order.

    Each file is re-hashed as it is read; a file that changed since inventory
    raises before its last chunk is yielded (control re-verifies everything on
    complete as well).
    """
    seq = 0
    for entry in manifest["entries"]:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        fd = os.open(directory, flags | os.O_DIRECTORY)
        try:
            parts = entry["relative_path"].split("/")
            for part in parts[:-1]:
                next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            handle = os.open(parts[-1], flags, dir_fd=fd)
        finally:
            os.close(fd)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(handle, "rb") as stream:
            while size < entry["size"]:
                content = stream.read(min(CHUNK_BYTES, entry["size"] - size))
                if not content:
                    break
                size += len(content)
                digest.update(content)
                if size == entry["size"] and (
                    stream.read(1) or digest.hexdigest() != entry["sha256"]
                ):
                    raise ValueError("an input file changed after it was inventoried")
                yield seq, content
                seq += 1
        if size != entry["size"]:
            raise ValueError("an input file changed after it was inventoried")
