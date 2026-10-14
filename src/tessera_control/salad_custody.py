"""Private external quarantine store; deploy outside control/proxy/workload.

The authenticated service composition alone issues tickets. Upload handlers must
bound request bodies before materializing bytes. Tickets are bearer secrets: never
log them. SQLite stores bytes atomically, not workload-selected filesystem paths.
Sealing revokes every writer before checking inventory; it is NOT proof that a
provider's node-local writers died, nor permission for scientific release.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


def private_database(path: Path) -> sqlite3.Connection:
    """Require pre-provisioned owner-private, non-symlink service state."""
    path = Path(path).absolute()
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise ValueError("unsafe_state_path")
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("state_directory_not_private")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError("unsafe_state_file") from None
    else:
        os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA journal_mode=DELETE")
    return db


@dataclass(frozen=True)
class CustodyBinding:
    tenant: str
    project: str
    operation: str
    epoch: int
    artifact: str
    kind: str = "output"

    def key(self) -> str:
        if (
            any(
                not isinstance(v, str) or not v or len(v) > 256
                for v in (self.tenant, self.project, self.operation, self.artifact)
            )
            or type(self.epoch) is not int
            or self.epoch < 0
            or self.kind not in {"output", "checkpoint"}
        ):
            raise ValueError("invalid_binding")
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


class SaladCustody:
    def __init__(self, path: Path, *, clock=time.time):
        self.db = private_database(path)
        self.clock = clock
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS inventories (
                binding TEXT PRIMARY KEY, byte_cap INTEGER, file_cap INTEGER,
                closed INTEGER NOT NULL DEFAULT 0, manifest TEXT);
            CREATE TABLE IF NOT EXISTS tickets (
                digest TEXT PRIMARY KEY, binding TEXT, name TEXT, checksum TEXT,
                size INTEGER, expires REAL, consumed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(binding, name));
            CREATE TABLE IF NOT EXISTS files (
                binding TEXT, name TEXT, checksum TEXT, size INTEGER, data BLOB,
                PRIMARY KEY(binding, name));
            CREATE TABLE IF NOT EXISTS closed_scopes (scope TEXT PRIMARY KEY);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(inventories)")}
        if "expected_files" not in columns:
            self.db.execute("ALTER TABLE inventories ADD COLUMN expected_files INTEGER DEFAULT 0")

    def register(self, binding: CustodyBinding, *, byte_cap: int, file_cap: int, expected_files=0):
        key = binding.key()
        if type(byte_cap) is not int or not 0 <= byte_cap <= 64 * 1024 * 1024:
            raise ValueError("invalid_byte_cap")
        if type(file_cap) is not int or not 0 <= file_cap <= 1024:
            raise ValueError("invalid_file_cap")
        if type(expected_files) is not int or not 0 <= expected_files <= file_cap:
            raise ValueError("invalid_expected_files")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT byte_cap,file_cap,expected_files FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if existing:
                if existing != (byte_cap, file_cap, expected_files):
                    raise ValueError("inventory_mismatch")
                return
            if self.db.execute(
                "SELECT 1 FROM closed_scopes WHERE scope=?", (self._scope(binding),)
            ).fetchone():
                raise ValueError("operation_closed")
            self.db.execute(
                "INSERT OR IGNORE INTO inventories(binding,byte_cap,file_cap,expected_files) "
                "VALUES(?,?,?,?)",
                (key, byte_cap, file_cap, expected_files),
            )
            if self.db.execute(
                "SELECT byte_cap,file_cap FROM inventories WHERE binding=?", (key,)
            ).fetchone() != (byte_cap, file_cap):
                raise ValueError("inventory_mismatch")

    def issue(
        self, binding: CustodyBinding, *, name: str, checksum: str, size: int, expires: float
    ) -> str:
        key = binding.key()
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 200
            or "\\" in name
            or "\x00" in name
            or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
            or type(size) is not int
            or size < 0
            or not self.clock() < expires <= self.clock() + 3600
        ):
            raise ValueError("invalid_ticket_scope")
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT byte_cap,file_cap,closed FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if not row or row[2]:
                raise ValueError("inventory_closed_or_missing")
            reserved = self.db.execute(
                "SELECT COALESCE(SUM(size),0),COUNT(*) FROM tickets WHERE binding=?", (key,)
            ).fetchone()
            if reserved[0] + size > row[0] or reserved[1] + 1 > row[1]:
                raise ValueError("output_cap")
            self.db.execute(
                "INSERT INTO tickets(digest,binding,name,checksum,size,expires) "
                "VALUES(?,?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), key, name, checksum, size, expires),
            )
        return token

    def upload(self, token: str, binding: CustodyBinding, data: bytes):
        if type(data) is not bytes or len(data) > 64 * 1024 * 1024:
            raise ValueError("invalid_body")
        key = binding.key()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT t.name,t.checksum,t.size,t.expires,t.consumed,i.closed "
                "FROM tickets t JOIN inventories i ON i.binding=t.binding "
                "WHERE t.digest=? AND t.binding=?",
                (hashlib.sha256(token.encode()).hexdigest(), key),
            ).fetchone()
            if not row or row[4] or row[5] or self.clock() >= row[3]:
                raise ValueError("ticket_unavailable")
            if len(data) != row[2] or hashlib.sha256(data).hexdigest() != row[1]:
                raise ValueError("integrity_mismatch")
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?)", (key, row[0], row[1], row[2], data)
            )
            self.db.execute(
                "UPDATE tickets SET consumed=1 WHERE digest=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    def close_writers(self, binding: CustodyBinding):
        """Close every output/checkpoint writer in the exact operation epoch."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            scope = self._scope(binding)
            self.db.execute("INSERT OR IGNORE INTO closed_scopes VALUES(?)", (scope,))
            for (key,) in self.db.execute("SELECT binding FROM inventories").fetchall():
                if self._scope(CustodyBinding(**json.loads(key))) == scope:
                    self.db.execute("UPDATE inventories SET closed=1 WHERE binding=?", (key,))

    @staticmethod
    def _scope(binding: CustodyBinding) -> str:
        binding.key()
        return json.dumps([binding.tenant, binding.project, binding.operation, binding.epoch])

    def seal_operation(self, binding: CustodyBinding) -> dict:
        self.close_writers(binding)
        scope = self._scope(binding)
        inventories = []
        keys = self.db.execute("SELECT binding FROM inventories ORDER BY binding").fetchall()
        for (key,) in keys:
            candidate = CustodyBinding(**json.loads(key))
            if self._scope(candidate) == scope:
                inventories.append(self.seal(candidate))
        if not inventories:
            raise ValueError("inventory_missing")
        manifest = dict(
            inventories=inventories,
            writers_closed=True,
            quarantine=True,
            complete=all(m["complete"] for m in inventories),
        )
        manifest["digest"] = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return manifest

    def seal(self, binding: CustodyBinding) -> dict:
        key = binding.key()
        # Closure is committed independently: a crash during verification cannot
        # reopen writers. Repeating verification after restart is safe.
        self.close_writers(binding)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT manifest,expected_files FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if not row:
                raise ValueError("inventory_missing")
            if row[0]:
                return json.loads(row[0])
            entries = []
            for name, checksum, size, data in self.db.execute(
                "SELECT name,checksum,size,data FROM files WHERE binding=? ORDER BY name", (key,)
            ):
                if len(data) != size or hashlib.sha256(data).hexdigest() != checksum:
                    raise ValueError("stored_integrity_mismatch")
                entries.append(dict(name=name, sha256=checksum, bytes=size))
            pending = self.db.execute(
                "SELECT COUNT(*) FROM tickets WHERE binding=? AND consumed=0", (key,)
            ).fetchone()[0]
            manifest = dict(
                binding=asdict(binding),
                files=entries,
                complete=pending == 0 and len(entries) >= row[1],
                writers_closed=True,
                quarantine=True,
            )
            manifest["digest"] = hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.db.execute(
                "UPDATE inventories SET manifest=? WHERE binding=?",
                (json.dumps(manifest, sort_keys=True), key),
            )
            return manifest

    def retained_metadata(self, binding: CustodyBinding, name: str) -> dict:
        """Read-only verified quarantine metadata; does not close any writer."""
        key = binding.key()
        row = self.db.execute(
            "SELECT f.data,f.checksum,f.size,i.byte_cap,i.file_cap FROM files f "
            "JOIN inventories i ON i.binding=f.binding WHERE f.binding=? AND f.name=?",
            (key, name),
        ).fetchone()
        result = dict(
            binding=asdict(binding),
            name=name,
            retained=False,
            sha256=None,
            bytes=None,
            quarantine=True,
        )
        if not row:
            return result
        totals = self.db.execute(
            "SELECT COALESCE(SUM(size),0),COUNT(*) FROM files WHERE binding=?", (key,)
        ).fetchone()
        data = bytes(row[0])
        if (
            len(data) != row[2]
            or hashlib.sha256(data).hexdigest() != row[1]
            or totals[0] > row[3]
            or totals[1] > row[4]
        ):
            raise ValueError("stored_integrity_mismatch")
        result.update(retained=True, sha256=row[1], bytes=row[2])
        return result

    def read_retained(self, binding: CustodyBinding, name: str) -> bytes:
        """Server-authenticated import seam; no ticket grants read/release authority."""
        key = binding.key()
        row = self.db.execute(
            "SELECT f.data,i.manifest FROM files f JOIN inventories i ON i.binding=f.binding "
            "WHERE f.binding=? AND f.name=?",
            (key, name),
        ).fetchone()
        if not row or not row[1]:
            raise ValueError("retained_file_unavailable")
        data = bytes(row[0])
        manifest = json.loads(row[1])
        entry = next(e for e in manifest["files"] if e["name"] == name)
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("stored_integrity_mismatch")
        return data
