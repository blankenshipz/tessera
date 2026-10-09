"""Hook-free, single-wheel materializer for an already verified capability.

No pip/uv execution, dependency discovery, import, shell, environment credentials,
or data/model download. This logic belongs INSIDE the reviewed runner image. Local
tests inject fixture streams; this module does not select a network transport.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import time
import zipfile
from email.parser import BytesParser
from pathlib import PurePosixPath

from tessera_control.store import canonical_digest

from .capability_effects import CHUNK_BYTES, BackendBlocked


class InstallDenied(ValueError):
    def __init__(self):
        super().__init__("bounded capability install rejected")


class CapabilityInstaller:
    """Private no-follow staging under the supervisor journal's lifetime owner lock.

    The only supported artifact is a pure-Python wheel with a complete RECORD and
    no dependencies, hooks, scripts, .data relocation or native code. Materializing
    files is not importing them or adding them to the host environment.

    Counters prevent byte/inode overshoot by this writer. They are NOT evidence of
    a filesystem/cgroup hard cap against another writer or blocked kernel calls;
    runner activation additionally requires independent project-quota/watchdog probes.
    """

    def __init__(self, *, journal, verifier, monotonic=time.monotonic):
        self.journal, self.verifier, self.clock = journal, verifier, monotonic
        journal.attach()
        with journal.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS capability_installs (
                    package TEXT PRIMARY KEY, operation TEXT UNIQUE NOT NULL,
                    nonce TEXT UNIQUE NOT NULL, state TEXT NOT NULL, logical_root TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capability_install_receipts (
                    package TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(package,sequence));
                CREATE TRIGGER IF NOT EXISTS install_receipts_no_update
                    BEFORE UPDATE ON capability_install_receipts
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS install_receipts_no_delete
                    BEFORE DELETE ON capability_install_receipts
                    BEGIN SELECT RAISE(ABORT,'retained install evidence'); END;
                CREATE TRIGGER IF NOT EXISTS installs_no_delete BEFORE DELETE ON capability_installs
                    BEGIN SELECT RAISE(ABORT,'retained install attempt'); END;
            """)
            # Crash/partial publication is never success and never becomes retryable.
            rows = db.execute(
                "SELECT package FROM capability_installs WHERE state='started'"
            ).fetchall()
            for row in rows:
                self._receipt(db, row[0], "unknown", 0, "0" * 64, None)

    def _receipt(self, db, package, state, size, digest, inventory):
        row = db.execute("SELECT * FROM capability_installs WHERE package=?", (package,)).fetchone()
        previous = db.execute(
            "SELECT payload FROM capability_install_receipts WHERE package=? "
            "ORDER BY sequence DESC LIMIT 1",
            (package,),
        ).fetchone()
        prior = json.loads(previous[0]) if previous else None
        result = {
            "package_digest": package,
            "operation_id": row["operation"],
            "nonce": row["nonce"],
            "target_digest": canonical_digest([package, row["logical_root"]]),
            "state": state,
            "bytes_downloaded": size,
            "downloaded_sha256": digest,
            "inventory": inventory,
            "inventory_digest": canonical_digest(inventory),
            "custody": "quarantined_unreleased",
            "sequence": prior["sequence"] + 1 if prior else 0,
            "previous_digest": canonical_digest(prior) if prior else "0" * 64,
        }
        db.execute("UPDATE capability_installs SET state=? WHERE package=?", (state, package))
        db.execute(
            "INSERT INTO capability_install_receipts VALUES (?,?,?)",
            (package, result["sequence"], json.dumps(result, sort_keys=True)),
        )
        return result

    def install(self, verified, *, plan, lock, transport):
        body = plan.validate_package(verified, self.verifier)
        action, m = body.binding.action, plan.activation
        package = verified.package.digest
        try:
            with self.journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM capability_installs WHERE state IN ('started','unknown')"
                ).fetchone():
                    raise ValueError
                db.execute(
                    "INSERT INTO capability_installs VALUES (?,?,?,'started',?)",
                    (package, body.operation_id, m.nonce, action.filesystem_root),
                )
                self._receipt(db, package, "started", 0, hashlib.sha256(b"").hexdigest(), None)
        except Exception:
            raise BackendBlocked("install attempt consumed or custody unavailable") from None
        start = self.clock()
        initial_wall = self.verifier.now()
        deadline = start + min(action.max_seconds, m.timeout_seconds)
        size, digest = 0, hashlib.sha256()
        output_bytes, inodes = 0, 0
        root_fd = stage_fd = None
        published = False
        inventory = None

        def check():
            plan.validate_package(verified, self.verifier)
            if (
                self.clock() < start
                or self.clock() >= deadline
                or self.verifier.now() < initial_wall
            ):
                raise InstallDenied

        def charge(byte_count=0, inode_count=0):
            nonlocal output_bytes, inodes
            check()
            if (
                output_bytes + byte_count > m.quota.output_bytes
                or inodes + inode_count > m.quota.output_inodes
            ):
                raise InstallDenied
            output_bytes += byte_count
            inodes += inode_count

        try:
            check()
            expected_lock = [
                {"name": action.name, "version": action.version, "artifact_sha256": action.sha256}
            ]
            if (
                action.kind != "package"
                or action.manager not in {"pip", "uv"}
                or not action.url.endswith(".whl")
                or not action.filesystem_root.startswith("/workspace/packages/")
                or type(lock) is not bytes
                or len(lock) > 65536
                or hashlib.sha256(lock).hexdigest() != action.lockfile_sha256
                or json.loads(lock) != expected_lock
            ):
                raise InstallDenied
            # Journal root is private, ancestor-checked, no-follow, and owner-locked.
            root_fd = os.open(self.journal.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stage = "install-" + m.nonce + ".partial"
            final = "install-" + m.nonce
            charge(inode_count=1)
            os.mkdir(stage, mode=0o700, dir_fd=root_fd)
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            response = transport.open(timeout=min(1.0, deadline - self.clock()))
            check()
            if (
                response.url,
                response.addresses,
                response.status,
                response.content_type,
                response.size,
            ) != (
                action.url,
                m.destination.addresses,
                200,
                action.content_type,
                action.expected_size,
            ):
                raise InstallDenied
            charge(inode_count=1)
            fd = os.open(
                "artifact.whl",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=stage_fd,
            )
            with os.fdopen(fd, "wb") as output:
                while True:
                    check()
                    limit = min(CHUNK_BYTES, action.expected_size - size + 1)
                    chunk = transport.read(
                        size, limit=limit, timeout=min(1.0, deadline - self.clock())
                    )
                    if (
                        type(chunk) is not bytes
                        or len(chunk) > limit
                        or size + len(chunk) > action.expected_size
                    ):
                        raise InstallDenied
                    check()
                    if not chunk:
                        break
                    charge(byte_count=len(chunk))
                    output.write(chunk)
                    size += len(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != action.expected_size or digest.hexdigest() != action.sha256:
                raise InstallDenied
            transport.close()
            check()
            fd = os.open("artifact.whl", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd)
            with os.fdopen(fd, "rb") as source, zipfile.ZipFile(source) as archive:
                entries = self._wheel(
                    archive,
                    action,
                    m.quota.output_bytes - output_bytes,
                    m.quota.output_inodes - inodes,
                    check,
                )
                # Verify the COMPLETE wheel and RECORD before any expanded file is staged.
                charge(inode_count=1)
                os.mkdir("tree", mode=0o700, dir_fd=stage_fd)
                tree_fd = os.open(
                    "tree", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage_fd
                )
                try:
                    for name, content in entries.items():
                        self._write(tree_fd, name, content, charge)
                    os.fsync(tree_fd)
                finally:
                    os.close(tree_fd)
                actual_files = self._inventory(stage_fd, entries, check)
                inventory = [
                    {
                        "name": action.name,
                        "version": action.version,
                        "artifact_sha256": action.sha256,
                        "files_digest": canonical_digest(actual_files),
                        "file_count": len(actual_files),
                    }
                ]
            check()
            os.fsync(stage_fd)
            # No overwriting an earlier install. Private parent + lifetime owner lock
            # excludes concurrent writers; no workload-selected destination is used.
            try:
                os.stat(final, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise InstallDenied
            published = True  # rename may apply and lose its reply; never downgrade to failed
            os.rename(stage, final, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
            check()
            with self.journal.connect() as db:
                return self._receipt(db, package, "installed", size, digest.hexdigest(), inventory)
        except Exception:
            # Failed staging is retained quarantined. Rename/receipt uncertainty stays unknown.
            with self.journal.connect() as db:
                return self._receipt(
                    db,
                    package,
                    "unknown" if published else "failed",
                    size,
                    digest.hexdigest(),
                    None,
                )
        finally:
            for fd in (stage_fd, root_fd):
                if fd is not None:
                    os.close(fd)
            try:
                transport.close()
            except Exception:
                pass

    @staticmethod
    def _wheel(archive, action, byte_limit, inode_limit, check):
        infos = archive.infolist()
        if not infos or len(infos) > min(inode_limit, 4096):
            raise InstallDenied
        entries, total = {}, 0
        for info in infos:
            check()
            name = info.filename
            path = PurePosixPath(name)
            mode = info.external_attr >> 16
            if (
                str(path) != name
                or path.is_absolute()
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
                or any(part in {".", ".."} for part in path.parts)
                or name in entries
                or info.is_dir()
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                or mode & 0o111
                or info.flag_bits & 1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or name.endswith((".pth", ".so", ".dll", ".exe", ".pyc", "/entry_points.txt"))
                or any(part.endswith(".data") for part in path.parts)
                or total + info.file_size > byte_limit
            ):
                raise InstallDenied
            with archive.open(info) as source:
                content = bytearray()
                while True:
                    check()
                    chunk = source.read(min(CHUNK_BYTES, info.file_size - len(content) + 1))
                    if len(content) + len(chunk) > info.file_size:
                        raise InstallDenied
                    if not chunk:
                        break
                    content.extend(chunk)
            if len(content) != info.file_size:
                raise InstallDenied
            entries[name] = bytes(content)
            total += len(content)
        metadata_paths = [name for name in entries if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise InstallDenied
        prefix = metadata_paths[0].removesuffix("METADATA")
        if any(".dist-info/" in name and not name.startswith(prefix) for name in entries):
            raise InstallDenied
        metadata = BytesParser().parsebytes(entries[prefix + "METADATA"])
        wheel = BytesParser().parsebytes(entries[prefix + "WHEEL"])
        if (
            metadata.get_all("Name") != [action.name]
            or metadata.get_all("Version") != [action.version]
            or metadata.get_all("Requires-Dist")
            or wheel.get_all("Root-Is-Purelib") != ["true"]
            or wheel.get_all("Tag") != ["py3-none-any"]
        ):
            raise InstallDenied
        record = prefix + "RECORD"
        rows = list(csv.reader(io.StringIO(entries[record].decode("utf-8"))))
        if len(rows) != len(entries) or {row[0] for row in rows} != set(entries):
            raise InstallDenied
        for name, hashed, size in rows:
            if name == record:
                if hashed or size:
                    raise InstallDenied
            elif hashed != "sha256=" + base64.urlsafe_b64encode(
                hashlib.sha256(entries[name]).digest()
            ).rstrip(b"=").decode() or size != str(len(entries[name])):
                raise InstallDenied
        return entries

    @staticmethod
    def _inventory(stage_fd, entries, check):
        """Read actual staged files, not the intended write inventory."""
        actual = {}

        def walk(directory, prefix=""):
            for name in os.listdir(directory):
                check()
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                path = prefix + name
                if stat.S_ISDIR(info.st_mode):
                    fd = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    try:
                        walk(fd, path + "/")
                    finally:
                        os.close(fd)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and path in entries:
                    expected = entries[path]
                    if info.st_size != len(expected):
                        raise InstallDenied
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    digest, size = hashlib.sha256(), 0
                    with os.fdopen(fd, "rb") as source:
                        while True:
                            check()
                            chunk = source.read(min(CHUNK_BYTES, len(expected) - size + 1))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > len(expected):
                                raise InstallDenied
                            digest.update(chunk)
                    actual[path] = digest.hexdigest()
                else:
                    raise InstallDenied

        tree = os.open("tree", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage_fd)
        try:
            walk(tree)
        finally:
            os.close(tree)
        if actual != {
            name: hashlib.sha256(content).hexdigest() for name, content in entries.items()
        }:
            raise InstallDenied
        return actual

    @staticmethod
    def _write(root_fd, name, content, charge):
        current = os.dup(root_fd)
        try:
            parts = PurePosixPath(name).parts
            for part in parts[:-1]:
                try:
                    os.stat(part, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    charge(inode_count=1)
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    os.fsync(current)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
            charge(byte_count=len(content), inode_count=1)
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=current,
            )
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.fsync(current)
        finally:
            os.close(current)
