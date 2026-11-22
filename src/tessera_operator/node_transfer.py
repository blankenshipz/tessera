"""Fixed local-path PVC import over private Docker stdin; never a networked Secret Pod."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

from mcp.types import ToolAnnotations

from tessera_control.recovery import _read_manifest

from . import kubernetes as target
from . import private_import as binder

RECEIPT = target.SOURCE / "private/local-persistent-node-import.json"
COMPLETE = target.SOURCE / "private/local-persistent-node-import-complete.json"
NODE = "desktop-control-plane"
DOCKER = ("docker", "--context", "desktop-linux", "exec", "-i", NODE)


def _fixed_path() -> tuple[dict, str]:
    mapping = binder.pv_inspect(require_binder=False)
    release = binder._receipt(binder.RELEASE)
    if release.get("pv_uid") != mapping["pv_uid"]:
        raise target.AdmissionError("binder release PV UID drifted")
    pods = target._kubectl("-n", target.NAMESPACE, "get", "pods", "-o", "json")
    if pods.get("items") != []:
        raise target.AdmissionError("target Pod present; refuse import")
    pvc = target._kubectl(
        "-n", target.NAMESPACE, "get", "pvc", "tessera-control-state", "-o", "json"
    )
    if pvc.get("metadata", {}).get("uid") != mapping["pvc_uid"]:
        raise target.AdmissionError("target claim UID changed")
    pv_name = pvc["spec"]["volumeName"]
    pv = target._kubectl("get", "pv", pv_name, "-o", "json")
    path = f"/var/local-path-provisioner/{pv_name}_{target.NAMESPACE}_tessera-control-state"
    if (
        pv.get("metadata", {}).get("uid") != mapping["pv_uid"]
        or pv.get("spec", {}).get("hostPath", {}).get("path") != path
    ):
        raise target.AdmissionError("node path or PV identity changed")
    return mapping, path


def _node(*args: str) -> bytes:
    try:
        result = subprocess.run([*DOCKER, *args], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise target.AdmissionError("fixed node inspection unavailable") from error
    if result.returncode or len(result.stdout) > 8192:
        raise target.AdmissionError("fixed node check failed")
    return result.stdout.strip()


def _regular(path: Path, limit: int) -> io.BufferedReader:
    if path.parent.is_symlink() or path.is_symlink():
        raise target.AdmissionError("private import source path unsafe")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or info.st_size > limit
    ):
        os.close(fd)
        raise target.AdmissionError("private import source file unsafe")
    return os.fdopen(fd, "rb")


def _archive(stream, manifest: dict) -> None:
    """Emit only fixed and manifest-validated regular entries to a streaming tar."""
    with tarfile.open(fileobj=stream, mode="w|") as tar:

        def add_file(name: str, path: Path, size_limit: int) -> None:
            with _regular(path, size_limit) as source:
                info = tarfile.TarInfo(name)
                info.size = os.fstat(source.fileno()).st_size
                info.mode = 0o600
                info.uid = info.gid = 65532
                tar.addfile(info, source)

        add_file("tessera.sqlite3", target.FENCED_BACKUP / "snapshot.sqlite3", manifest["size"])
        add_file("owner.header", target.QUARANTINE / "owner.header", 8192)
        for entry in manifest.get("objects", []):
            if entry["availability"] == "present":
                name = "/".join(
                    (
                        "project-artifacts",
                        entry["tenant_namespace"],
                        entry["project_namespace"],
                        entry["object_key"],
                    )
                )
                path = (
                    target.FENCED_BACKUP
                    / "objects"
                    / entry["tenant_namespace"]
                    / entry["project_namespace"]
                    / entry["object_key"]
                )
                add_file(name, path, entry["size"])


def import_private() -> dict:
    """One attempt only: no arbitrary path, Pod, command or credential output."""
    mapping, root = _fixed_path()
    if RECEIPT.exists() or RECEIPT.is_symlink() or COMPLETE.exists() or COMPLETE.is_symlink():
        raise target.AdmissionError("prior node import retained; no retry or overwrite")
    if (
        not target.FENCE_MARKER.is_file()
        or target.FENCE_MARKER.is_symlink()
        or target.FENCE_MARKER.stat().st_mode & 0o077
    ):
        raise target.AdmissionError("source authority not quarantined")
    if target.QUARANTINE.is_symlink() or target.FENCED_BACKUP.is_symlink():
        raise target.AdmissionError("private source directory unsafe")
    try:
        marker = json.loads(target.FENCE_MARKER.read_bytes())
    except (OSError, ValueError) as error:
        raise target.AdmissionError("source fence marker invalid") from error
    if (
        marker.get("namespace_uid") != mapping["namespace_uid"]
        or not isinstance(marker.get("names"), list)
        or not {"tessera.sqlite3", "owner.header", "project-artifacts"}.issubset(marker["names"])
    ):
        raise target.AdmissionError("source fence identity or custody invalid")
    target._source_listener_absent()
    if (target.SOURCE / "tessera.sqlite3").exists() or (
        target.SOURCE / "tessera.sqlite3"
    ).is_symlink():
        raise target.AdmissionError("old source database reappeared")
    verified = target.validate_backup(target.FENCED_BACKUP)
    manifest = _read_manifest(target.FENCED_BACKUP)
    if _node("readlink", "-f", root).decode() != root:
        raise target.AdmissionError("PVC path symlink or escape")
    if _node("stat", "-c", "%F:%u:%g", root) != b"directory:0:0":
        raise target.AdmissionError("PVC root identity unsafe")
    # test -e must fail for an absent destination; no 'mkdir -p', no overwrite.
    status = subprocess.run(
        [*DOCKER, "test", "-e", root + "/control"], capture_output=True, timeout=10
    )
    if status.returncode != 1:
        raise target.AdmissionError("target state already exists or cannot be checked")
    intent = {
        "namespace_uid": mapping["namespace_uid"],
        "pvc_uid": mapping["pvc_uid"],
        "pv_uid": mapping["pv_uid"],
        "snapshot_sha256": verified["snapshot_sha256"],
    }
    fd = os.open(RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(intent).encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(RECEIPT.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if _fixed_path() != (mapping, root):
        raise target.AdmissionError("PV changed before private import")
    # This fixed node script admits only the exact empty child. tar receives only
    # operator-built regular-file entries on stdin; no secrets in argv/output.
    script = (
        'set -eu; test ! -e "$1/control"; test ! -L "$1/control"; '
        'mkdir -m 700 "$1/control"; '
        'tar -x --keep-old-files --no-same-owner --no-same-permissions -f - -C "$1/control"; '
        'chown -R 65532:65532 "$1/control"; '
        'find "$1/control" -type d -exec chmod 700 {} +; '
        'find "$1/control" -type f -exec chmod 600 {} +'
    )
    process = subprocess.Popen(
        [*DOCKER, "sh", "-ceu", script, "--", root],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if process.stdin is None:
            raise target.AdmissionError("private node stream unavailable")
        _archive(process.stdin, manifest)
        process.stdin.close()
        if process.wait(timeout=60):
            raise target.AdmissionError("private node import partial; retained for reconciliation")
    except BaseException as error:
        if process.poll() is None:
            process.kill()
            process.wait()
        if isinstance(error, target.AdmissionError):
            raise
        raise target.AdmissionError(
            "private node import failed; retained for reconciliation"
        ) from error
    after, check_root = _fixed_path()
    if after != mapping or check_root != root:
        raise target.AdmissionError("node identity changed after import")
    target._source_listener_absent()
    if (
        _node("sha256sum", root + "/control/tessera.sqlite3").decode().split()[0]
        != verified["snapshot_sha256"]
    ):
        raise target.AdmissionError("imported snapshot hash mismatch")
    owner = target.QUARANTINE / "owner.header"
    with _regular(owner, 8192) as stream:
        owner_hash = hashlib.sha256(stream.read()).hexdigest()
    if _node("sha256sum", root + "/control/owner.header").decode().split()[0] != owner_hash:
        raise target.AdmissionError("imported credential hash mismatch")
    for entry in manifest.get("objects", []):
        if entry["availability"] == "present":
            path = (
                root
                + "/control/project-artifacts/"
                + "/".join(
                    (entry["tenant_namespace"], entry["project_namespace"], entry["object_key"])
                )
            )
            if _node("sha256sum", path).decode().split()[0] != entry["sha256"]:
                raise target.AdmissionError("imported object hash mismatch")
            if _node("stat", "-c", "%F:%a:%u:%g", path) != b"regular file:600:65532:65532":
                raise target.AdmissionError("imported object type/mode/owner mismatch")
    if (
        _node("stat", "-c", "%F:%a:%u:%g", root + "/control/owner.header")
        != b"regular file:600:65532:65532"
    ):
        raise target.AdmissionError("credential mode or owner mismatch")
    if (
        _node("stat", "-c", "%F:%a:%u:%g", root + "/control/tessera.sqlite3")
        != b"regular file:600:65532:65532"
    ):
        raise target.AdmissionError("database mode or owner mismatch")
    if _node("stat", "-c", "%F:%a:%u:%g", root + "/control") != b"directory:700:65532:65532":
        raise target.AdmissionError("state directory mode or owner mismatch")
    fd = os.open(COMPLETE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps({**intent, "owner_sha256": owner_hash}).encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(COMPLETE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {
        "state": "private_import_verified_target_inactive",
        "pv_uid": mapping["pv_uid"],
        "snapshot_sha256": verified["snapshot_sha256"],
        "object_count": len(manifest.get("objects", [])),
    }


def register(server) -> None:
    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_private_import() -> dict:
        """Import fixed quarantined state via node stdin; never expose owner bytes."""
        return import_private()
