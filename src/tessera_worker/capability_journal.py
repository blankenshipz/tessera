"""Private durable effect intents, append-only receipts and quarantined byte custody.

SQLite is the atomic no-overwrite publication boundary here, not an agent-selected
filesystem root. The control reconciler publishes to the existing project backend.
One lifetime file lock prevents overlapping owners and permits crash recovery.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field

from tessera_control.capability_models import Digest, Identifier, InstalledPackage, Strict
from tessera_control.capability_package import SignedCapabilityPackage
from tessera_control.store import canonical_digest

TERMINAL = {"completed", "failed", "cancelled", "unknown", "blocked"}


class BrokerReceipt(Strict):
    sequence: int = Field(ge=0, le=1024)
    package_digest: Digest
    previous_digest: Digest
    state: Literal[
        "requested",
        "approved",
        "started",
        "progress",
        "completed",
        "failed",
        "cancel_requested",
        "cancelled",
        "unknown",
        "blocked",
    ]
    phase: Literal["admission", "download", "install", "publication", "recovery"]
    reason: Literal[
        "none",
        "policy",
        "backend_unavailable",
        "mismatch",
        "timeout",
        "cancel",
        "lost_reply",
        "partial_install",
        "crash",
        "custody_conflict",
    ] = "none"
    timestamp: str = Field(max_length=40)
    elapsed_ms: int = Field(ge=0, le=2**53 - 1)
    bytes_retained: int = Field(ge=0, le=16 * 1024 * 1024)
    request_digest: Digest
    response_digest: Digest | None = None
    response_status: int | None = Field(default=None, ge=100, le=599)
    effect_digest: Digest
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    termination_confirmed: bool = False
    usage: None = None
    cost: None = None
    evidence: Literal["synthetic", "isolated"]
    # Exact approved inventory only; unexpected output is represented by a digest.
    inventory_digest: Digest | None = None
    installed_packages: list[InstalledPackage] = Field(default_factory=list, max_length=128)
    max_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    max_seconds: int = Field(ge=1, le=3600)
    max_concurrency: Literal[1] = 1
    action_id: Identifier
    artifact_name: Identifier
    artifact_version: Identifier
    artifact_sha256: Digest
    lockfile_sha256: Digest | None = None


def validate_bundle(bundle, package_digest):
    if set(bundle) != {"package_digest", "receipts", "content_sha256", "size", "state"}:
        raise ValueError("invalid journal bundle")
    if bundle["package_digest"] != package_digest or not 1 <= len(bundle["receipts"]) <= 1024:
        raise ValueError("invalid journal identity")
    previous = "0" * 64
    if len(json.dumps(bundle)) > 2 * 1024 * 1024:
        raise ValueError("journal bundle exceeds reconciliation bound")
    for i, receipt in enumerate(bundle["receipts"]):
        parsed = BrokerReceipt.model_validate(receipt)
        if (parsed.sequence, parsed.package_digest, parsed.previous_digest) != (
            i,
            package_digest,
            previous,
        ):
            raise ValueError("broken receipt chain")
        previous = canonical_digest(receipt)
        if (i == 0 and parsed.state != "requested") or (
            i < len(bundle["receipts"]) - 1 and parsed.state in TERMINAL
        ):
            raise ValueError("invalid lifecycle sequence")
    last = bundle["receipts"][-1]
    if (
        bundle["state"] not in TERMINAL
        or bundle["state"] != last["state"]
        or bundle["content_sha256"] != last["effect_digest"]
        or bundle["size"] != last["bytes_retained"]
    ):
        raise ValueError("incomplete journal bundle")


class JournalConflict(ValueError):
    pass


class CapabilityJournal:
    def __init__(self, root: Path):
        self.root = Path(root)
        # Deployment-owned parent; never accept a workload-supplied directory.
        if self.root.absolute() != self.root.resolve():
            raise JournalConflict("journal ancestors must not be symlinks")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self._attached = threading.Lock()
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise JournalConflict("journal requires private owner custody")
        self._lock = os.open(
            self.root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock)
            raise JournalConflict("journal already supervised") from None
        self.path = self.root / "broker.sqlite3"
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
        else:
            info = None
        if info is not None and (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            self.close()
            raise JournalConflict("unsafe journal file")
        try:
            if info is None:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if (info is not None and version != 1) or version not in (0, 1):
                    raise JournalConflict("unsupported worker journal schema")
                db.execute("PRAGMA user_version=1")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS identity (id TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS packages (
                        digest TEXT PRIMARY KEY, payload BLOB NOT NULL, signature TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS operations (
                        operation_id TEXT PRIMARY KEY, package_digest TEXT UNIQUE NOT NULL,
                        scope TEXT NOT NULL, state TEXT NOT NULL,
                        cancelled INTEGER NOT NULL DEFAULT 0, acknowledged TEXT);
                    CREATE TABLE IF NOT EXISTS receipts (
                        operation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                        payload TEXT NOT NULL, PRIMARY KEY(operation_id,sequence));
                    CREATE TABLE IF NOT EXISTS chunks (
                        operation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                        content BLOB NOT NULL, PRIMARY KEY(operation_id,sequence));
                    CREATE TABLE IF NOT EXISTS custody (
                        package_digest TEXT PRIMARY KEY, scope TEXT NOT NULL,
                        sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                        classification TEXT NOT NULL
                            CHECK(classification='quarantined_unreleased'));
                    CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts
                        BEGIN SELECT RAISE(ABORT,'append only'); END;
                    CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts
                        BEGIN SELECT RAISE(ABORT,'append only'); END;
                    CREATE TRIGGER IF NOT EXISTS chunks_no_update BEFORE UPDATE ON chunks
                        BEGIN SELECT RAISE(ABORT,'immutable bytes'); END;
                    CREATE TRIGGER IF NOT EXISTS chunks_no_delete BEFORE DELETE ON chunks
                        BEGIN SELECT RAISE(ABORT,'retained custody'); END;
                    CREATE TRIGGER IF NOT EXISTS custody_no_update BEFORE UPDATE ON custody
                        BEGIN SELECT RAISE(ABORT,'immutable custody'); END;
                    CREATE TRIGGER IF NOT EXISTS custody_no_delete BEFORE DELETE ON custody
                        BEGIN SELECT RAISE(ABORT,'retained custody'); END;
                    CREATE TRIGGER IF NOT EXISTS packages_no_update BEFORE UPDATE ON packages
                        BEGIN SELECT RAISE(ABORT,'immutable package'); END;
                    CREATE TRIGGER IF NOT EXISTS packages_no_delete BEFORE DELETE ON packages
                        BEGIN SELECT RAISE(ABORT,'retained package'); END;
                """)
                if not db.execute("SELECT id FROM identity").fetchone():
                    db.execute(
                        "INSERT INTO identity VALUES (?)", (uuid.uuid4().hex + uuid.uuid4().hex,)
                    )
                self.identity = db.execute("SELECT id FROM identity").fetchone()[0]
            os.chmod(self.path, 0o600)
            for directory in (self.root, self.root.parent):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            self.close()
            raise

    @contextmanager
    def connect(self):
        if self._lock is None:
            raise JournalConflict("journal closed")
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def attach(self):
        if not self._attached.acquire(blocking=False):
            raise JournalConflict("journal already attached to a broker")

    def claim(self, body, package, receipt):
        package_digest = package.digest
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM operations WHERE operation_id=? OR package_digest=?",
                (body.operation_id, package_digest),
            ).fetchone():
                raise JournalConflict("replay: reconcile existing operation; never re-effect")
            # A single journal per enrolled broker/target; unknown is not a free slot.
            if db.execute(
                "SELECT 1 FROM operations WHERE state NOT IN "
                "('completed','failed','cancelled','blocked')"
            ).fetchone():
                raise JournalConflict("concurrency or unresolved effect")
            scope = canonical_digest(
                [body.org_id, body.project_id, body.worker_id, body.binding.environment_id]
            )
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,'requested',0,NULL)",
                (body.operation_id, package_digest, scope),
            )
            db.execute(
                "INSERT INTO packages VALUES (?,?,?)",
                (package.digest, package.payload, package.signature),
            )
            self._append(db, body.operation_id, receipt)

    def retained_package(self, operation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT p.* FROM packages p JOIN operations o "
                "ON o.package_digest=p.digest WHERE o.operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise JournalConflict("unknown operation")
        return SignedCapabilityPackage(row["payload"], row["digest"], row["signature"])

    def _append(self, db, operation_id, receipt):
        row = db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None or row["state"] in TERMINAL:
            raise JournalConflict("late receipt or unknown operation")
        previous = db.execute(
            "SELECT sequence,payload FROM receipts WHERE operation_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        value = receipt | dict(
            sequence=previous["sequence"] + 1 if previous else 0,
            previous_digest=canonical_digest(json.loads(previous["payload"]))
            if previous
            else "0" * 64,
            package_digest=row["package_digest"],
        )
        value = BrokerReceipt.model_validate(value).model_dump(mode="json")
        db.execute(
            "INSERT INTO receipts VALUES (?,?,?)",
            (operation_id, value["sequence"], json.dumps(value, sort_keys=True)),
        )
        db.execute(
            "UPDATE operations SET state=? WHERE operation_id=?", (value["state"], operation_id)
        )

    def append(self, operation_id, receipt, chunk=None, publish=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._append(db, operation_id, receipt)
            if chunk is not None:
                index = db.execute(
                    "SELECT count(*) FROM chunks WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
                db.execute("INSERT INTO chunks VALUES (?,?,?)", (operation_id, index, chunk))
            if publish:
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                db.execute(
                    "INSERT INTO custody VALUES (?,?,?,?,'quarantined_unreleased')",
                    (
                        row["package_digest"],
                        row["scope"],
                        receipt["effect_digest"],
                        receipt["bytes_retained"],
                    ),
                )

    def cancel(self, operation_id):
        """Durable desired state, not termination evidence. Safe from another thread."""
        with self.connect() as db:
            db.execute("UPDATE operations SET cancelled=1 WHERE operation_id=?", (operation_id,))

    def cancelled(self, operation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT cancelled FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return row is not None and row[0] == 1

    def bundle(self, operation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            receipts = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM receipts WHERE operation_id=? ORDER BY sequence",
                    (operation_id,),
                )
            ]
            content = b"".join(
                r[0]
                for r in db.execute(
                    "SELECT content FROM chunks WHERE operation_id=? ORDER BY sequence",
                    (operation_id,),
                )
            )
        bundle = dict(
            package_digest=row["package_digest"],
            receipts=receipts,
            content_sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            state=row["state"],
        )
        return bundle, content

    def recover(self, timestamp):
        """Call only after obtaining exclusive ownership; never resume effects."""
        with self.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT operation_id FROM operations WHERE state NOT IN "
                    "('completed','failed','cancelled','unknown','blocked')"
                )
            ]
        for operation_id in ids:
            bundle, _ = self.bundle(operation_id)
            previous = bundle["receipts"][-1]
            self.append(
                operation_id,
                previous
                | dict(
                    state="unknown",
                    phase="recovery",
                    reason="crash",
                    timestamp=timestamp,
                    termination_confirmed=False,
                    effect_digest=bundle["content_sha256"],
                    bytes_retained=bundle["size"],
                ),
            )

    def reconcile(self, package, sink):
        body = package.decode()
        bundle, content = self.bundle(body.operation_id)
        validate_bundle(bundle, package.digest)
        # No download/install retry. A lost sink reply only repeats evidence transfer.
        result = sink(package, bundle, content)
        if result.get("bundle_digest") != canonical_digest(bundle):
            raise JournalConflict("sink acknowledgement differs")
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET acknowledged=? WHERE operation_id=?",
                (result["bundle_digest"], body.operation_id),
            )
        return result
