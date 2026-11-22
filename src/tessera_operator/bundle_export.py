"""Unregistered exact-bundle export. No backup invocation, cleanup or transfer retry.

Local destination identity/readback is not authenticated off-host custody. Named
Pod exec checks before/after reads are snapshots, not continuous UID fencing.
"""

import base64
import hashlib
import inspect
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

from tessera_control.recovery import _validate_entry

from . import upgrade_support as target
from .maintenance_witness import read_json
from .schema_supervised import (
    BoundedKubernetes,
    KubernetesSupervisedProfile,
    SupervisedDenied,
    exec_command,
)

CHUNK = 49152
LIMIT = (1 << 30) + 524288


def read_chunk(root, relative, offset, count):
    """Fixed RO remote probe; no supplied executable or writable DB open."""
    import base64
    import os
    from pathlib import Path

    root = Path(root)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError("unsafe root")
    path = root
    for part in relative.split("/"):
        if not part or part in {".", ".."} or "\\" in part:
            raise ValueError("unsafe relative path")
        path = path / part
        if path.is_symlink():
            raise ValueError("link in source")
    if type(offset) is not int or offset < 0 or not 0 < count <= 49152:
        raise ValueError("invalid range")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("unsafe source file")
        stream.seek(offset)
        data = stream.read(count)
        after = os.fstat(stream.fileno())

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    if identity(before) != identity(after) or identity(path.stat()) != identity(after):
        raise ValueError("source changed")
    return {"data": base64.b64encode(data).decode(), "size": after.st_size}


def directory_identity(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise SupervisedDenied("noncanonical destination directory")
    info = path.stat()
    if info.st_mode & 0o077:
        raise SupervisedDenied("destination must be private")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


class PodBundleSource:
    """Concrete bounded exec adapter; fixed /scratch and old/new container only."""

    def __init__(self, operation, phase, pins, *, transport=None):
        if (
            phase not in {"backup", "validate"}
            or not operation
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in operation)
        ):
            raise SupervisedDenied("invalid source operation/phase")
        self.operation, self.phase, self.pins = operation, phase, pins
        self.transport = transport or BoundedKubernetes()
        self.container = "old" if phase == "backup" else "new"
        self.identity = {
            "context": target.CONTEXT,
            "namespace": target.NAMESPACE,
            "pod": operation,
            "operation": operation,
            "phase": phase,
            "container": self.container,
            "pins": pins,
        }

    def check(self):
        pod = self.transport.call(
            {
                "argv": [
                    *target.KUBECTL,
                    "-n",
                    target.NAMESPACE,
                    "get",
                    "pod",
                    self.operation,
                    "-o",
                    "json",
                ]
            }
        )
        status = next(s for s in pod["status"]["containerStatuses"] if s["name"] == self.container)
        if (
            pod["metadata"]["uid"] != self.pins["uid"]
            or status["containerID"] != self.pins["containerID"]
            or status["imageID"] != self.pins["imageID"]
            or "running" not in status["state"]
        ):
            raise SupervisedDenied("source runtime identity changed")

    def read(self, relative, offset, count):
        self.check()
        script = inspect.getsource(read_chunk) + (
            "\nimport json; print(json.dumps(read_chunk('/scratch',"
            f"{relative!r},{offset!r},{count!r})))"
        )
        value = self.transport.call(exec_command(self.operation, self.container, script))
        self.check()
        return value


def inventory(plan, phase, receipt):
    if phase not in {"backup", "validate"}:
        raise SupervisedDenied("export requires producing backup/validate phase")
    remote = receipt["remote"]
    KubernetesSupervisedProfile.validate_remote(
        {
            "phase": phase,
            "operation": plan["operation"],
            "target": plan["target"],
            "db": {"incarnation": plan["db_incarnation"], "sha256": remote["input_db_sha256"]},
        },
        remote,
    )
    custody = remote["custody"]
    manifest = custody["manifest"]
    prefix = "pre" if phase == "backup" else "post"
    files = {}
    for name, value, digest in (
        (prefix + "/manifest.json", manifest, custody["manifest_sha256"]),
        (
            phase + ".restore-proof.json",
            custody["restore_receipt"],
            custody["restore_receipt_sha256"],
        ),
    ):
        # Size/encoding are discovered from bounded source bytes AFTER the claim.
        # Parsed equality and the producing phase's exact-byte digest both bind it.
        files[name] = {"size": None, "sha256": digest, "json": value}
    if (
        manifest["format_version"] not in {1, 2}
        or manifest["schema_version"] != remote["db"]["schema"]
        or type(manifest["size"]) is not int
        or not 0 < manifest["size"] <= 1 << 30
    ):
        raise SupervisedDenied("unsupported backup format")
    files[prefix + "/snapshot.sqlite3"] = {"size": manifest["size"], "sha256": manifest["sha256"]}
    entries = manifest.get("objects", [])
    if len(entries) > 1024 or (entries and manifest["format_version"] != 2):
        raise SupervisedDenied("unsupported object inventory")
    for entry in entries:
        if not _validate_entry(entry):
            raise SupervisedDenied("unsafe object declaration")
        if entry["availability"] == "present":
            name = (
                prefix
                + "/objects/"
                + "/".join(
                    entry[k] for k in ("tenant_namespace", "project_namespace", "object_key")
                )
            )
            if name in files:
                raise SupervisedDenied("duplicate object path")
            files[name] = {k: entry[k] for k in ("size", "sha256")}
    if sum(v["size"] or 262144 for v in files.values()) > LIMIT:
        raise SupervisedDenied("bundle exceeds aggregate limit")
    for expected in files.values():
        sha = expected["sha256"]
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise SupervisedDenied("invalid checksum")
    return files


class BundleExport:
    """Entry: bound source/receipt/root. Export once; readback on reconnect; no cleanup."""

    def __init__(self, journal, destination, *, clock=time.time):
        self.journal, self.destination = Path(journal), Path(destination)
        self.clock = clock

    def check(self, claim):
        if self.clock() >= claim["deadline"]:
            raise SupervisedDenied("export deadline expired")
        if directory_identity(self.destination) != claim["destination"]:
            raise SupervisedDenied("destination identity changed")

    def export(self, plan, phase, receipt, source, *, seconds=30):
        if not 0 < seconds <= 30:
            raise SupervisedDenied("export duration must be within 30 seconds")
        files = inventory(plan, phase, receipt)
        if (
            source.identity.get("operation") != plan["operation"]
            or source.identity.get("phase") != phase
        ):
            raise SupervisedDenied("source operation/phase mismatch")
        claim = {
            "operation": plan["operation"],
            "phase": phase,
            "target": plan["target"],
            "source": source.identity,
            "destination": directory_identity(self.destination),
            "incarnation": plan["db_incarnation"],
            "schema": receipt["remote"]["db"]["schema"],
            "files": files,
            "deadline": min(plan["deadline"], self.clock() + seconds),
        }
        self.check(claim)
        self.journal.mkdir(mode=0o700, exist_ok=True)
        directory_identity(self.journal)
        target._write_once(self.journal / "claim.json", claim)
        try:
            output = self.destination / "export"
            output.mkdir(mode=0o700)  # exclusive; no adoption or merge
            for name, expected in files.items():
                self.check(claim)
                first = None
                if expected["size"] is None:
                    first = source.read(name, 0, CHUNK)
                    self.check(claim)
                    if type(first["size"]) is not int or not 0 < first["size"] <= 262144:
                        raise SupervisedDenied("metadata size exceeds bound")
                    expected["size"] = first["size"]
                path = output / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with path.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    while count < expected["size"] or count == 0:
                        self.check(claim)
                        value = (
                            first
                            if first is not None
                            else source.read(name, count, min(CHUNK, expected["size"] - count) or 1)
                        )
                        first = None
                        data = base64.b64decode(value["data"], validate=True)
                        self.check(claim)
                        if (
                            value["size"] != expected["size"]
                            or len(data) > CHUNK
                            or count + len(data) > expected["size"]
                            or (not data and count < expected["size"])
                        ):
                            raise SupervisedDenied("source length changed/truncated")
                        stream.write(data)
                        digest.update(data)
                        count += len(data)
                        if not data:
                            break
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest.hexdigest() != expected["sha256"]:
                    raise SupervisedDenied("source checksum drift")
                if "json" in expected and json.loads(path.read_bytes()) != expected["json"]:
                    raise SupervisedDenied("metadata differs from producing receipt")
                target._write_once(
                    self.journal / (hashlib.sha256(name.encode()).hexdigest() + ".json"),
                    {"path": name, **expected},
                )
            for root, _, _ in os.walk(output, topdown=False):
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            fd = os.open(self.destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            result = self.reconcile()
            target._write_once(self.journal / "result.json", result)
            return result
        except Exception as exc:
            target._write_once(
                self.journal / "failure.json",
                {"error_type": type(exc).__name__, "action": "readback only; never recopy"},
            )
            raise SupervisedDenied("export uncertain; claim/partial bytes preserved") from None

    def reconcile(self):
        claim, _ = read_json(self.journal / "claim.json")
        # Reconnect allowed after transfer deadline; new bounded read-only window.
        deadline = self.clock() + 30
        if directory_identity(self.destination) != claim["destination"]:
            raise SupervisedDenied("destination identity changed")
        root = self.destination / "export"
        directory_identity(root)
        seen = set()
        for parent, directories, names in os.walk(root, followlinks=False):
            if self.clock() >= deadline or len(directories) + len(names) > 2051:
                raise SupervisedDenied("readback traversal bounds exceeded")
            for directory in directories:
                directory_identity(Path(parent) / directory)
            for name in names:
                seen.add(str((Path(parent) / name).relative_to(root)))
                if len(seen) > 1027:
                    raise SupervisedDenied("unexpected destination inventory")
        if seen != set(claim["files"]):
            raise SupervisedDenied("partial/unexpected destination inventory; no recopy")
        for name, expected in claim["files"].items():
            digest, count = hashlib.sha256(), 0
            if expected["size"] is None:
                size = read_chunk(root, name, 0, 1)["size"]
                if type(size) is not int or not 0 < size <= 262144:
                    raise SupervisedDenied("metadata size exceeds bound")
                expected["size"] = size
            while count < expected["size"] or count == 0:
                if self.clock() >= deadline:
                    raise SupervisedDenied("readback deadline expired")
                value = read_chunk(root, name, count, min(CHUNK, expected["size"] - count) or 1)
                data = base64.b64decode(value["data"], validate=True)
                if value["size"] != expected["size"] or (not data and count < expected["size"]):
                    raise SupervisedDenied("destination length drift")
                digest.update(data)
                count += len(data)
                if not data:
                    break
            if digest.hexdigest() != expected["sha256"]:
                raise SupervisedDenied("destination checksum drift")
            if "json" in expected and json.loads((root / name).read_bytes()) != expected["json"]:
                raise SupervisedDenied("metadata differs from producing receipt")
        prefix = "pre" if claim["phase"] == "backup" else "post"
        path = root / prefix / "snapshot.sqlite3"
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(lambda: int(self.clock() >= deadline), 1000)
            schema = connection.execute("SELECT schema_version FROM schema_meta").fetchall()
            incarnation = connection.execute("SELECT incarnation FROM control_recovery").fetchall()
        finally:
            connection.close()
        if schema != [(claim["schema"],)] or incarnation != [(claim["incarnation"],)]:
            raise SupervisedDenied("exported snapshot lineage mismatch")
        return {
            "operation": claim["operation"],
            "phase": claim["phase"],
            "target": claim["target"],
            "destination": claim["destination"],
            "files": claim["files"],
            "observed_at": self.clock(),
            "coverage": "local exact-byte readback; not off-host custody or continuous UID fencing",
            "acknowledgement": "readback does not reconstruct original transfer acknowledgement",
        }
