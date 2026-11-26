"""Control-side verification of sealed Salad output custody (ADR 0036 phase 3).

The guardian's sealed manifest is a claim until control checks it: exact binding,
writers closed, quarantine, completeness, the declared output set, per-file and
total caps, and every file's sha256 recomputed from streamed import chunks. One
chunk is in memory at a time. Nothing here grants release or execution authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat

from .salad_workload_contract import OUTPUT_CHUNK_BYTES, OUTPUT_NAME_RE, output_chunk_count
from .store import canonical_digest

_HEX64 = re.compile(r"[a-f0-9]{64}")
_NAME = OUTPUT_NAME_RE
# Reply bound for one import chunk: base64 of OUTPUT_CHUNK_BYTES plus its envelope.
IMPORT_CHUNK_REPLY_BYTES = OUTPUT_CHUNK_BYTES * 4 // 3 + 4096


class OutputImportError(ValueError):
    """Detail-free: the sealed custody did not verify."""


def expected_binding(state):
    proposal = state["proposal"]
    return dict(
        tenant=state["org_id"],
        project=state["project_id"],
        operation=proposal["operation_id"],
        epoch=proposal["epoch"],
        artifact=proposal["selection"]["salad"]["execution"]["artifact_destination"],
        kind="output",
    )


def declared_outputs(execution):
    """workload_v1 declared outputs as {name: max_bytes}; None for the fixed smoke."""
    if execution.get("argv_binding") != "workload_v1":
        return None
    return {o["name"]: o["max_bytes"] for o in execution["workload"]["outputs"]}


def _signed(value):
    return type(value) is dict and canonical_digest(
        {k: v for k, v in value.items() if k != "digest"}
    ) == value.get("digest")


def verify_manifest(manifest, *, binding, declared, max_total, partial=False):
    """Verify a sealed chunked-import manifest; return its file entries.

    By default the manifest must be complete: exactly the declared files. With ``partial`` (an
    attempt that ended interrupted or retryable) the diagnostic and any other declared file
    may be missing: the manifest is still signed, sealed and bound, and lists only distinct
    declared files, each checked below and later re-hashed by ``stream_files``. A partial
    manifest is never a complete one: the caller keeps it out of any success verdict."""
    required = (
        ("writers_closed", "quarantine")
        if partial
        else ("writers_closed", "quarantine", "complete")
    )
    if (
        not _signed(manifest)
        or set(manifest) != {"inventories", "writers_closed", "quarantine", "complete", "digest"}
        or not all(manifest[k] is True for k in required)
        or type(manifest["complete"]) is not bool
        or type(manifest["inventories"]) is not list
        or len(manifest["inventories"]) != 1
    ):
        raise OutputImportError("sealed manifest required")
    inventory = manifest["inventories"][0]
    if (
        not _signed(inventory)
        or inventory.get("binding") != binding
        or not all(inventory.get(k) is True for k in required)
        or type(inventory.get("files")) is not list
    ):
        raise OutputImportError("exact custody binding required")
    files = inventory["files"]
    names = [f.get("name") if type(f) is dict else None for f in files]
    if declared is None or (
        names != sorted(declared)
        if not partial
        else names != sorted(set(names)) or not set(names) <= set(declared)
    ):
        raise OutputImportError("declared output set required")
    total = 0
    for entry in files:
        if (
            set(entry) != {"name", "sha256", "bytes"}
            or not _NAME.fullmatch(entry["name"])
            or type(entry["sha256"]) is not str
            or not _HEX64.fullmatch(entry["sha256"])
            or type(entry["bytes"]) is not int
            or not 0 <= entry["bytes"] <= declared[entry["name"]]
            or (entry["bytes"] == 0 and entry["sha256"] != hashlib.sha256(b"").hexdigest())
        ):
            raise OutputImportError("declared output cap")
        total += entry["bytes"]
    if total > max_total:
        raise OutputImportError("total output cap")
    return [dict(entry) for entry in files]


def stream_files(fetch_chunk, files, sink=None):
    """Pull every chunk of every file, verifying chunk and whole-file sha256.

    ``fetch_chunk(name, seq)`` returns the guardian's import_chunk reply.
    ``sink(name)`` (optional) returns a writable binary file for verified bytes.
    Returns the verified (name, sha256, bytes) entries.
    """
    verified = []
    for entry in files:
        digest, total = hashlib.sha256(), 0
        out = sink(entry["name"]) if sink else None
        for seq in range(output_chunk_count(entry["bytes"])):
            reply = fetch_chunk(entry["name"], seq)
            if (
                type(reply) is not dict
                or set(reply) != {"name", "seq", "sha256", "content_base64"}
                or reply["name"] != entry["name"]
                or reply["seq"] != seq
                or type(reply["content_base64"]) is not str
            ):
                raise OutputImportError("import chunk malformed")
            try:
                data = base64.b64decode(reply["content_base64"].encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError, ValueError):
                raise OutputImportError("import chunk malformed") from None
            expected = min(OUTPUT_CHUNK_BYTES, entry["bytes"] - seq * OUTPUT_CHUNK_BYTES)
            if len(data) != expected or hashlib.sha256(data).hexdigest() != reply["sha256"]:
                raise OutputImportError("import chunk integrity")
            digest.update(data)
            total += len(data)
            if out is not None:
                out.write(data)
        if total != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise OutputImportError("retained bytes integrity")
        verified.append(dict(entry))
    return verified


def _existing_digest(dir_fd, name):
    """(size, sha256) of an existing regular, single-link file, else None if absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OutputImportError("destination entry is not a plain file")
        digest, size = hashlib.sha256(), 0
        while chunk := handle.read(OUTPUT_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _drop_published_part(dir_fd, name):
    """Finish an interrupted publish: ``.<name>.part`` hard-linked as ``<name>``.

    A crash after os.link and before the unlink leaves two links to one inode. If
    the temporary name is that same inode, remove it; the digest is still verified.
    """
    temporary = "." + name + ".part"
    try:
        part = os.stat(temporary, dir_fd=dir_fd, follow_symlinks=False)
        final = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (part.st_dev, part.st_ino) == (final.st_dev, final.st_ino) and stat.S_ISREG(part.st_mode):
        os.unlink(temporary, dir_fd=dir_fd)


def write_verified_files(fetch_chunk, files, destination):
    """Stream verified files into an existing owner-only directory, never replacing.

    Each file is written to ``.<name>.part`` (a leftover one from an interrupted
    import is removed first), fsynced, and only after its whole-file sha256
    verified is it published with a hard link, which fails if the name exists,
    and the temporary name is removed. A retry after a partial import resumes:
    an already published file whose bytes match the manifest is accepted without
    refetching; one that differs fails. Every filesystem error is mapped to
    OutputImportError.
    """
    try:
        return _write_verified_files(fetch_chunk, files, destination)
    except OSError:
        raise OutputImportError("destination write failed") from None


def _write_verified_files(fetch_chunk, files, destination):
    dir_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OutputImportError("private destination required")
        verified = []
        for entry in files:
            name = entry["name"]
            _drop_published_part(dir_fd, name)
            existing = _existing_digest(dir_fd, name)
            if existing is not None:
                if existing != (entry["bytes"], entry["sha256"]):
                    raise OutputImportError("destination file differs")
                verified.append(dict(entry))  # resumed: already verified and published
                continue
            temporary = "." + name + ".part"
            try:
                leftover = os.stat(temporary, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(leftover.st_mode):
                    raise OutputImportError("destination entry is not a plain file")
                os.unlink(temporary, dir_fd=dir_fd)  # never published; refetch it
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    verified += stream_files(fetch_chunk, [entry], lambda _, h=handle: h)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except FileExistsError:
                    raise OutputImportError("destination file exists") from None
            finally:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
        os.fsync(dir_fd)
        return verified
    finally:
        os.close(dir_fd)
