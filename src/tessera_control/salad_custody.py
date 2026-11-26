"""Private external quarantine store; deploy outside control/proxy/workload.

The authenticated service composition alone issues tickets. Upload handlers must
bound request bodies before materializing bytes. Tickets are bearer secrets: never
log them. SQLite stores bytes atomically, not workload-selected filesystem paths.
Sealing revokes every writer before checking inventory; it is NOT proof that a
provider's node-local writers died, nor permission for scientific release.

Two writer shapes share one inventory. The original single-object path issues a
bearer ticket and stores one blob. Chunked output custody (ADR 0036 phase 3)
binds each upload to an inventory's declared outputs: the first chunk of a name
reserves its declared size and sha256 against the per-file and total caps, every
chunk has a fixed offset, length and sha256, and a file is complete only when its
streamed whole-file sha256 matches. Chunks are stored as parts; nothing is ever
assembled in memory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .salad_workload_contract import (
    CHECKPOINT_OUTPUT_NAME,
    MAX_TOTAL_OUTPUT_BYTES,
    OUTPUT_CHUNK_BYTES,
    OUTPUT_NAME_RE,
    output_call_budget,
    output_chunk_count,
)

# Chunk calls an inventory accepts: twice its planned chunks plus a fixed slack.
CHUNK_RETRY_SLACK = 16


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


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


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
            CREATE TABLE IF NOT EXISTS parts (
                binding TEXT, name TEXT, seq INTEGER, checksum TEXT, data BLOB,
                PRIMARY KEY(binding, name, seq));
        """)
        # Additive, idempotent upgrades of an existing private store.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(inventories)")}
        if "expected_files" not in columns:
            self.db.execute("ALTER TABLE inventories ADD COLUMN expected_files INTEGER DEFAULT 0")
        if "outputs" not in columns:
            self.db.execute("ALTER TABLE inventories ADD COLUMN outputs TEXT")
        if "chunk_calls" not in columns:
            self.db.execute("ALTER TABLE inventories ADD COLUMN chunk_calls INTEGER DEFAULT 0")
        if "chunked" not in {r[1] for r in self.db.execute("PRAGMA table_info(files)")}:
            self.db.execute("ALTER TABLE files ADD COLUMN chunked INTEGER NOT NULL DEFAULT 0")

    def register(
        self,
        binding: CustodyBinding,
        *,
        byte_cap: int,
        file_cap: int,
        expected_files=0,
        outputs=None,
    ):
        """Create the inventory once. ``outputs`` declares exact names and per-file caps.

        With declared outputs every file must be one of them, within its own cap,
        and the inventory is complete only when all of them are retained.
        """
        key = binding.key()
        # A Salad stage's own outputs stay within the platform custody total (README):
        # the store refuses a larger inventory even if a caller were to ask for one.
        if type(byte_cap) is not int or not 0 <= byte_cap <= MAX_TOTAL_OUTPUT_BYTES:
            raise ValueError("invalid_byte_cap")
        if type(file_cap) is not int or not 0 <= file_cap <= 1024:
            raise ValueError("invalid_file_cap")
        if type(expected_files) is not int or not 0 <= expected_files <= file_cap:
            raise ValueError("invalid_expected_files")
        declared = None
        if outputs is not None:
            declared = self._declared(outputs)
            if (
                expected_files != len(declared)
                or file_cap != len(declared)
                or sum(declared.values()) > byte_cap
            ):
                raise ValueError("invalid_declared_outputs")
            declared = json.dumps(sorted(declared.items()), separators=(",", ":"))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT byte_cap,file_cap,expected_files,outputs FROM inventories WHERE binding=?",
                (key,),
            ).fetchone()
            if existing:
                if existing != (byte_cap, file_cap, expected_files, declared):
                    raise ValueError("inventory_mismatch")
                return
            if self.db.execute(
                "SELECT 1 FROM closed_scopes WHERE scope=?", (self._scope(binding),)
            ).fetchone():
                raise ValueError("operation_closed")
            self.db.execute(
                "INSERT OR IGNORE INTO inventories(binding,byte_cap,file_cap,expected_files,"
                "outputs) VALUES(?,?,?,?,?)",
                (key, byte_cap, file_cap, expected_files, declared),
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
                "SELECT byte_cap,file_cap,closed,outputs FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if not row or row[2]:
                raise ValueError("inventory_closed_or_missing")
            if row[3] is not None:
                declared = dict(json.loads(row[3]))
                if name not in declared or size > declared[name]:
                    raise ValueError("output_undeclared_or_oversize")
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
                "INSERT INTO files(binding,name,checksum,size,data) VALUES(?,?,?,?,?)",
                (key, row[0], row[1], row[2], data),
            )
            self.db.execute(
                "UPDATE tickets SET consumed=1 WHERE digest=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    @staticmethod
    def _declared(outputs) -> dict:
        declared = {}
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= 16:
            raise ValueError("invalid_declared_outputs")
        for item in outputs:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "max_bytes"}
                or not isinstance(item["name"], str)
                or not OUTPUT_NAME_RE.fullmatch(item["name"])
                or type(item["max_bytes"]) is not int
                or not 1 <= item["max_bytes"] <= MAX_TOTAL_OUTPUT_BYTES
                or item["name"] in declared
            ):
                raise ValueError("invalid_declared_outputs")
            declared[item["name"]] = item["max_bytes"]
        return declared

    @staticmethod
    def _upload_digest(key: str, name: str) -> str:
        """Ticket-table key of a chunked reservation. No bearer token exists for it."""
        return hashlib.sha256(f"chunked\0{key}\0{name}".encode()).hexdigest()

    def put_chunk(
        self,
        binding: CustodyBinding,
        *,
        name: str,
        checksum: str,
        size: int,
        seq: int,
        chunk_sha256: str,
        data: bytes,
        expires: float,
    ) -> dict:
        """Store one chunk of a declared output; idempotent per (name, seq).

        The caller has already authenticated the bound workload and checked its
        deadline. The first chunk of a name reserves its declared size and sha256
        against the per-file and total caps; later chunks must repeat them exactly.
        A chunk is exactly the declared slice length with a matching sha256, so no
        byte beyond the declared caps is ever stored. When the last chunk lands, the
        whole file is re-hashed from its parts; a mismatch discards that file's parts
        (the reservation stays, so the same declaration can be retried within the
        chunk budget). Returns completion of the file and of all declared outputs.
        """
        key = binding.key()
        if (
            not isinstance(name, str)
            or not isinstance(checksum, str)
            or not _hex64(checksum)
            or not isinstance(chunk_sha256, str)
            or not _hex64(chunk_sha256)
            or type(size) is not int
            or size < 0
            or (size == 0 and checksum != _EMPTY_SHA256)  # impossible: fail fast
            or type(seq) is not int
            or type(data) is not bytes
            or len(data) > OUTPUT_CHUNK_BYTES
            or type(expires) not in {int, float}
        ):
            raise ValueError("invalid_chunk")
        upload = self._upload_digest(key, name)
        # The retry budget is charged in its own committed transaction, before any
        # validation, so refused calls (conflicts, replays) still consume it.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT closed,outputs,chunk_calls FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if not row or row[0] or row[1] is None:
                raise ValueError("inventory_closed_or_missing")
            declared = dict(json.loads(row[1]))
            budget = output_call_budget(declared)
            if row[2] >= budget + CHUNK_RETRY_SLACK:
                raise ValueError("chunk_budget_exhausted")
            self.db.execute(
                "UPDATE inventories SET chunk_calls=chunk_calls+1 WHERE binding=?", (key,)
            )
        failed = False
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT byte_cap,file_cap,closed,outputs FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if not row or row[2] or row[3] is None:
                raise ValueError("inventory_closed_or_missing")
            declared = dict(json.loads(row[3]))
            if name not in declared or size > declared[name]:
                raise ValueError("output_undeclared_or_oversize")
            ticket = self.db.execute(
                "SELECT digest,checksum,size,consumed FROM tickets WHERE binding=? AND name=?",
                (key, name),
            ).fetchone()
            if (
                ticket is not None
                and name == CHECKPOINT_OUTPUT_NAME
                and ticket[:3] != (upload, checksum, size)
            ):
                # The one superseding output: a newer checkpoint replaces the previous
                # one (complete or not) under the same operation. The chunk-call budget
                # bounds how often; every other output stays write-once.
                for table in ("parts", "files", "tickets"):
                    self.db.execute(f"DELETE FROM {table} WHERE binding=? AND name=?", (key, name))
                ticket = None
            if ticket is None:
                reserved = self.db.execute(
                    "SELECT COALESCE(SUM(size),0),COUNT(*) FROM tickets WHERE binding=?", (key,)
                ).fetchone()
                if reserved[0] + size > row[0] or reserved[1] + 1 > row[1]:
                    raise ValueError("output_cap")
                self.db.execute(
                    "INSERT INTO tickets(digest,binding,name,checksum,size,expires) "
                    "VALUES(?,?,?,?,?,?)",
                    (upload, key, name, checksum, size, float(expires)),
                )
                ticket = (upload, checksum, size, 0)
            if ticket[:3] != (upload, checksum, size):
                raise ValueError("upload_mismatch")
            chunks = output_chunk_count(size)
            if not 0 <= seq < chunks:
                raise ValueError("chunk_out_of_range")
            offset = seq * OUTPUT_CHUNK_BYTES
            if (
                len(data) != min(OUTPUT_CHUNK_BYTES, size - offset)
                or hashlib.sha256(data).hexdigest() != chunk_sha256
            ):
                raise ValueError("chunk_integrity_mismatch")
            stored = self.db.execute(
                "SELECT checksum FROM parts WHERE binding=? AND name=? AND seq=?",
                (key, name, seq),
            ).fetchone()
            if stored is not None and stored[0] != chunk_sha256:
                raise ValueError("chunk_conflict")
            if stored is None and not ticket[3]:
                self.db.execute(
                    "INSERT INTO parts(binding,name,seq,checksum,data) VALUES(?,?,?,?,?)",
                    (key, name, seq, chunk_sha256, data),
                )
                have = self.db.execute(
                    "SELECT COUNT(*) FROM parts WHERE binding=? AND name=?", (key, name)
                ).fetchone()[0]
                if have == chunks:
                    if self._parts_digest(key, name, size) == checksum:
                        self.db.execute(
                            "INSERT INTO files(binding,name,checksum,size,data,chunked) "
                            "VALUES(?,?,?,?,?,1)",
                            (key, name, checksum, size, b""),
                        )
                        self.db.execute("UPDATE tickets SET consumed=1 WHERE digest=?", (upload,))
                    else:
                        self.db.execute("DELETE FROM parts WHERE binding=? AND name=?", (key, name))
                        failed = True
            done = self.db.execute("SELECT COUNT(*) FROM files WHERE binding=?", (key,))
            done = done.fetchone()[0]
            file_done = self.db.execute(
                "SELECT 1 FROM files WHERE binding=? AND name=?", (key, name)
            ).fetchone()
        if failed:
            raise ValueError("integrity_mismatch")
        return dict(file_complete=bool(file_done), outputs_complete=done == len(declared))

    def chunk_ack(
        self,
        binding: CustodyBinding,
        *,
        name: str,
        checksum: str,
        size: int,
        seq: int,
        chunk_sha256: str,
        data: bytes,
    ) -> dict:
        """Read-only acknowledgement of an exact replay of an already stored chunk.

        For a lost final response only: the file must be complete under the same
        declaration and the replayed bytes must equal the stored part. Nothing is
        written, reopened or charged here; works on a closed inventory.
        """
        key = binding.key()
        if (
            not isinstance(name, str)
            or not isinstance(checksum, str)
            or not isinstance(chunk_sha256, str)
            or type(size) is not int
            or type(seq) is not int
            or type(data) is not bytes
            or not 0 <= seq < output_chunk_count(max(size, 0))
            or len(data) != min(OUTPUT_CHUNK_BYTES, size - seq * OUTPUT_CHUNK_BYTES)
            or hashlib.sha256(data).hexdigest() != chunk_sha256
        ):
            raise ValueError("chunk_unavailable")
        row = self.db.execute("SELECT outputs FROM inventories WHERE binding=?", (key,)).fetchone()
        ticket = self.db.execute(
            "SELECT digest,checksum,size,consumed FROM tickets WHERE binding=? AND name=?",
            (key, name),
        ).fetchone()
        part = self.db.execute(
            "SELECT checksum FROM parts WHERE binding=? AND name=? AND seq=?", (key, name, seq)
        ).fetchone()
        if (
            not row
            or row[0] is None
            or ticket != (self._upload_digest(key, name), checksum, size, 1)
            or part != (chunk_sha256,)
        ):
            raise ValueError("chunk_unavailable")
        done = self.db.execute("SELECT COUNT(*) FROM files WHERE binding=?", (key,)).fetchone()[0]
        return dict(file_complete=True, outputs_complete=done == len(json.loads(row[0])))

    def _parts_digest(self, key: str, name: str, size: int):
        """Stream a chunked file's parts in order; None unless every part is intact."""
        digest, total, expected_seq = hashlib.sha256(), 0, 0
        for seq, checksum, data in self.db.execute(
            "SELECT seq,checksum,data FROM parts WHERE binding=? AND name=? ORDER BY seq",
            (key, name),
        ):
            data = bytes(data)
            if seq != expected_seq or hashlib.sha256(data).hexdigest() != checksum:
                return None
            expected_seq += 1
            total += len(data)
            digest.update(data)
        if expected_seq != output_chunk_count(size) or total != size:
            return None
        return digest.hexdigest()

    def _file_intact(self, key: str, name: str, checksum: str, size: int, data, chunked: int):
        if chunked:
            return self._parts_digest(key, name, size) == checksum
        data = bytes(data)
        return len(data) == size and hashlib.sha256(data).hexdigest() == checksum

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
                "SELECT manifest,expected_files,outputs FROM inventories WHERE binding=?", (key,)
            ).fetchone()
            if not row:
                raise ValueError("inventory_missing")
            if row[0]:
                return json.loads(row[0])
            entries = []
            rows = self.db.execute(
                "SELECT name,checksum,size,chunked FROM files WHERE binding=? ORDER BY name", (key,)
            ).fetchall()
            for name, checksum, size, chunked in rows:
                data = None
                if not chunked:
                    data = self.db.execute(
                        "SELECT data FROM files WHERE binding=? AND name=?", (key, name)
                    ).fetchone()[0]
                if not self._file_intact(key, name, checksum, size, data, chunked):
                    raise ValueError("stored_integrity_mismatch")
                entries.append(dict(name=name, sha256=checksum, bytes=size))
            pending = self.db.execute(
                "SELECT COUNT(*) FROM tickets WHERE binding=? AND consumed=0", (key,)
            ).fetchone()[0]
            complete = pending == 0 and len(entries) >= row[1]
            if row[2] is not None:
                # Declared outputs: complete only with exactly the declared set.
                complete = complete and [e["name"] for e in entries] == sorted(
                    dict(json.loads(row[2]))
                )
            manifest = dict(
                binding=asdict(binding),
                files=entries,
                complete=complete,
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
            "SELECT f.data,f.checksum,f.size,i.byte_cap,i.file_cap,f.chunked FROM files f "
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
        if (
            not self._file_intact(key, name, row[1], row[2], row[0], row[5])
            or totals[0] > row[3]
            or totals[1] > row[4]
        ):
            raise ValueError("stored_integrity_mismatch")
        result.update(retained=True, sha256=row[1], bytes=row[2])
        return result

    def retained_outputs(self, binding: CustodyBinding):
        """Read-only per-declared-output progress; None for an undeclared inventory.

        Reports completed files (name, sha256, bytes) and the declared names. It
        does not rehash: seal and import verify the bytes. No writer is closed.
        """
        key = binding.key()
        row = self.db.execute("SELECT outputs FROM inventories WHERE binding=?", (key,)).fetchone()
        if not row or row[0] is None:
            return None
        declared = sorted(dict(json.loads(row[0])))
        files = [
            dict(name=name, sha256=checksum, bytes=size)
            for name, checksum, size in self.db.execute(
                "SELECT name,checksum,size FROM files WHERE binding=? ORDER BY name", (key,)
            )
        ]
        return dict(
            binding=asdict(binding),
            declared=declared,
            files=files,
            complete=[f["name"] for f in files] == declared,
            quarantine=True,
        )

    def uploaded_chunked(self, binding: CustodyBinding) -> bool:
        """Whether any retained file arrived through chunked upload."""
        return bool(
            self.db.execute(
                "SELECT 1 FROM files WHERE binding=? AND chunked=1", (binding.key(),)
            ).fetchone()
        )

    def read_chunk(self, binding: CustodyBinding, name: str, seq: int) -> dict:
        """Server-authenticated chunked import of one sealed file; never a release.

        Serves only a file listed in the sealed manifest, one OUTPUT_CHUNK_BYTES
        slice at a time, with its sha256. The importer verifies the whole file.
        """
        key = binding.key()
        row = self.db.execute(
            "SELECT f.data,f.chunked,f.size,f.checksum,i.manifest FROM files f "
            "JOIN inventories i ON i.binding=f.binding WHERE f.binding=? AND f.name=?",
            (key, name),
        ).fetchone()
        if not row or not row[4] or type(seq) is not int:
            raise ValueError("retained_file_unavailable")
        entry = next((e for e in json.loads(row[4])["files"] if e["name"] == name), None)
        if entry is None or (entry["sha256"], entry["bytes"]) != (row[3], row[2]):
            raise ValueError("stored_integrity_mismatch")
        if not 0 <= seq < output_chunk_count(row[2]):
            raise ValueError("retained_file_unavailable")
        if row[1]:
            part = self.db.execute(
                "SELECT checksum,data FROM parts WHERE binding=? AND name=? AND seq=?",
                (key, name, seq),
            ).fetchone()
            if not part:
                raise ValueError("stored_integrity_mismatch")
            data = bytes(part[1])
            if hashlib.sha256(data).hexdigest() != part[0]:
                raise ValueError("stored_integrity_mismatch")
        else:
            offset = seq * OUTPUT_CHUNK_BYTES
            data = bytes(row[0])[offset : offset + OUTPUT_CHUNK_BYTES]
        if len(data) != min(OUTPUT_CHUNK_BYTES, row[2] - seq * OUTPUT_CHUNK_BYTES):
            raise ValueError("stored_integrity_mismatch")
        return dict(
            name=name,
            seq=seq,
            sha256=hashlib.sha256(data).hexdigest(),
            content_base64=base64.b64encode(data).decode("ascii"),
        )

    def read_retained(self, binding: CustodyBinding, name: str) -> bytes:
        """Server-authenticated import seam; no ticket grants read/release authority."""
        key = binding.key()
        row = self.db.execute(
            "SELECT f.data,i.manifest,f.chunked FROM files f "
            "JOIN inventories i ON i.binding=f.binding WHERE f.binding=? AND f.name=?",
            (key, name),
        ).fetchone()
        if not row or not row[1] or row[2]:
            raise ValueError("retained_file_unavailable")
        data = bytes(row[0])
        manifest = json.loads(row[1])
        entry = next(e for e in manifest["files"] if e["name"] == name)
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("stored_integrity_mismatch")
        return data


def _hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)
