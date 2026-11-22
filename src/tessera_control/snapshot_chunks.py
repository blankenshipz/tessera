"""Chunked input snapshots (ADR 0037 s1): fixed 4 MiB chunks in SQLite, same digest identity.

A chunked snapshot is an ordinary ``source_snapshots`` row (same id, digest, release gate).
Its bytes live in ``snapshot_chunks``; ``snapshot_chunk_uploads`` records the human upload
that produced it.  Chunk hashes are integrity aids; file hashes and the inventory digest bind.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifact_limits import CHUNK_BYTES as _LIMIT_CHUNK_BYTES
from .artifact_limits import operator_limits
from .code_approval import (
    CodeApproval,
    declare_pending_code,
    discard_pending_code,
    record_code_approval,
)
from .data_class import (
    DataClassDeclaration,
    declare_pending,
    discard_pending,
    record_snapshot_data_class,
    refuse_relabelled_files,
)
from .snapshots import SnapshotStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    canonical_digest,
    utc_now,
)

CHUNK_BYTES = _LIMIT_CHUNK_BYTES
MAX_CHUNKED_BYTES = operator_limits().max_artifact_bytes
MAX_CHUNKED_FILES = 10_000
MAX_CHUNKED_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_EXCLUSIONS = 10_000
MAX_PROJECT_CHUNK_BYTES = operator_limits().max_project_bytes
UPLOAD_TTL = timedelta(hours=24)
_HEX64 = r"^[0-9a-f]{64}$"


def create_snapshot_chunk_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS snapshot_chunk_uploads (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        expected_digest TEXT NOT NULL, manifest_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('uploading','completed')),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL, completed_at TEXT, expires_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS snapshot_chunks (
        owner_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK(seq>=0),
        relative_path TEXT NOT NULL, offset INTEGER NOT NULL CHECK(offset>=0),
        size INTEGER NOT NULL CHECK(size BETWEEN 1 AND 4194304),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64), content BLOB NOT NULL,
        PRIMARY KEY (owner_id, seq)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS provider_input_tickets (
        token_hash TEXT PRIMARY KEY CHECK(length(token_hash)=64),
        operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
        snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        snapshot_digest TEXT NOT NULL, lease_id TEXT NOT NULL, epoch INTEGER NOT NULL,
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        expires_at TEXT NOT NULL, revoked_at TEXT,
        chunks_served INTEGER NOT NULL DEFAULT 0, last_served_at TEXT,
        created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS provider_input_tickets_by_operation "
        "ON provider_input_tickets(operation_id)"
    )


def _field(entry: Any, name: str) -> Any:
    return entry[name] if isinstance(entry, dict) else getattr(entry, name)


def chunk_plan(entries: Any) -> list[dict[str, Any]]:
    """Deterministic plan: entries in manifest order; seq counts across the whole snapshot."""
    plan: list[dict[str, Any]] = []
    for entry in entries:
        path, size = _field(entry, "relative_path"), _field(entry, "size")
        offset = 0
        while offset < size:
            length = min(CHUNK_BYTES, size - offset)
            plan.append({"seq": len(plan), "relative_path": path, "offset": offset, "size": length})
            offset += length
    return plan


def is_chunked(connection: sqlite3.Connection, snapshot_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM snapshot_chunk_uploads WHERE id = ? AND status = 'completed'",
            (snapshot_id,),
        ).fetchone()
        is not None
    )


def chunk_index(connection: sqlite3.Connection, snapshot_id: str) -> list[dict[str, Any]]:
    return [
        {key: row[key] for key in ("seq", "relative_path", "offset", "size", "sha256")}
        for row in connection.execute(
            """SELECT seq, relative_path, offset, size, sha256 FROM snapshot_chunks
            WHERE owner_id = ? ORDER BY seq""",
            (snapshot_id,),
        )
    ]


def read_chunk(
    connection: sqlite3.Connection, snapshot_id: str, seq: int
) -> tuple[dict[str, Any], bytes]:
    row = connection.execute(
        """SELECT seq, relative_path, offset, size, sha256, content FROM snapshot_chunks
        WHERE owner_id = ? AND seq = ?""",
        (snapshot_id, seq),
    ).fetchone()
    if row is None:
        raise NotFound("Snapshot chunk was not found.")
    content = row["content"]
    if (
        not isinstance(content, bytes)
        or len(content) != row["size"]
        or len(content) > CHUNK_BYTES
        or hashlib.sha256(content).hexdigest() != row["sha256"]
    ):
        raise Conflict("Snapshot chunk integrity check failed.")
    meta = {key: row[key] for key in ("seq", "relative_path", "offset", "size", "sha256")}
    return meta, content


def check_chunk_metadata(
    connection: sqlite3.Connection, owner_id: str, entries: list[dict[str, Any]]
) -> None:
    """Rows must equal the manifest's chunk plan exactly; no content is read or hashed."""
    plan = chunk_plan(entries)
    count = 0
    for row in connection.execute(
        """SELECT seq, relative_path, offset, size, sha256, length(content) AS stored
        FROM snapshot_chunks WHERE owner_id = ? ORDER BY seq""",
        (owner_id,),
    ):
        if (
            count >= len(plan)
            or row["seq"] != plan[count]["seq"]
            or row["relative_path"] != plan[count]["relative_path"]
            or row["offset"] != plan[count]["offset"]
            or row["size"] != plan[count]["size"]
            or row["stored"] != row["size"]
            or re.fullmatch(r"[0-9a-f]{64}", row["sha256"] or "") is None
        ):
            raise Conflict("Snapshot chunk set integrity check failed.")
        count += 1
    if count != len(plan):
        raise Conflict("Snapshot chunk set integrity check failed.")


def verify_chunk_content(
    connection: sqlite3.Connection, owner_id: str, entries: list[dict[str, Any]]
) -> None:
    """Full re-hash: every chunk, then every file streamed across its chunks."""
    check_chunk_metadata(connection, owner_id, entries)
    seq = 0
    for entry in entries:
        digest, total = hashlib.sha256(), 0
        while total < entry["size"]:
            meta, content = read_chunk(connection, owner_id, seq)
            if meta["relative_path"] != entry["relative_path"] or meta["offset"] != total:
                raise Conflict("Snapshot chunk set integrity check failed.")
            digest.update(content)
            total += len(content)
            seq += 1
        if total != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise Conflict("Snapshot file integrity check failed.")


def validate_chunked_manifest(manifest: dict[str, Any], expected_digest: str) -> dict[str, Any]:
    """Same rules as snapshots._validate_capture, with chunked bounds."""
    try:
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise StoreError("Snapshot inventory is not valid JSON.") from None
    if hashlib.sha256(canonical.encode()).hexdigest() != expected_digest:
        raise Conflict("Inventory does not match the expected digest; inventory it again.")
    entries = manifest["entries"]
    if len(entries) > MAX_CHUNKED_FILES or len(manifest["exclusions"]) > MAX_EXCLUSIONS:
        raise StoreError("Snapshot file count exceeds bounds.")
    if len({entry["relative_path"] for entry in entries}) != len(entries):
        raise StoreError("Snapshot paths must be unique.")
    total = 0
    for entry in entries:
        path = entry["relative_path"]
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise StoreError("Snapshot contains an unsafe relative path.")
        if (
            entry["provenance"] != "imported_unverified"
            or entry["classification"] != "unclassified"
        ):
            raise StoreError("Snapshot provenance or classification is not eligible.")
        total += entry["size"]
        if total > MAX_CHUNKED_BYTES:
            raise StoreError("Snapshot exceeds its byte bound.")
    if total != manifest["total_bytes"]:
        raise StoreError("Snapshot total does not match the inventory.")
    if len(json.dumps(manifest).encode()) > MAX_CHUNKED_MANIFEST_BYTES:
        raise StoreError("Snapshot manifest exceeds its byte bound.")
    return json.loads(json.dumps(manifest))


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    relative_path: str = Field(min_length=1, max_length=4096)
    size: int = Field(ge=0, le=MAX_CHUNKED_BYTES)
    sha256: str = Field(pattern=_HEX64)
    provenance: str = Field(max_length=100)
    classification: str = Field(max_length=100)


class _Exclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    relative_path: str = Field(max_length=4096)
    reason: str = Field(max_length=1000)


class _Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    root: str = Field(max_length=4096)
    entries: list[_Entry] = Field(max_length=MAX_CHUNKED_FILES)
    exclusions: list[_Exclusion] = Field(max_length=MAX_EXCLUSIONS)
    total_bytes: int = Field(ge=0, le=MAX_CHUNKED_BYTES)


class BeginChunkedSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_digest: str = Field(pattern=_HEX64)
    inventory: _Inventory
    # ADR 0039: optional; without it the snapshot is private_derived.
    data_class: DataClassDeclaration | None = None
    # ADR 0039 amendment 1: exact paths a human approves as private code or config.
    code_approval: CodeApproval | None = None


class CompleteChunkedSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # ADR 0039: optional; must equal a declaration made at begin, if any.
    data_class: DataClassDeclaration | None = None
    # ADR 0039 amendment 1: optional; must equal an approval made at begin, if any.
    code_approval: CodeApproval | None = None


def _without_absent_class(body: dict[str, Any]) -> dict[str, Any]:
    """Requests without a declaration or code approval keep their earlier idempotency
    digest."""
    return {
        k: v for k, v in body.items() if k not in {"data_class", "code_approval"} or v is not None
    }


class PutSnapshotChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content_base64: str = Field(max_length=(4 * 1024 * 1024 // 3 + 1) * 4 + 16)
    sha256: str = Field(pattern=_HEX64)


def enforce_project_chunk_cap(
    connection: sqlite3.Connection, project_id: str, incoming_bytes: int
) -> None:
    """Refuse if completed chunked snapshots plus unexpired uploads would exceed the cap."""
    held = connection.execute(
        """SELECT COALESCE(SUM(json_extract(manifest_json, '$.total_bytes')), 0)
        FROM snapshot_chunk_uploads WHERE project_id = ?""",
        (project_id,),
    ).fetchone()[0]
    if held + incoming_bytes > MAX_PROJECT_CHUNK_BYTES:
        raise Conflict("Project chunked snapshot storage cap would be exceeded.")


def insert_chunked_snapshot_rows(
    connection: sqlite3.Connection,
    snapshot_id: str,
    project_id: str,
    digest: str,
    manifest: dict[str, Any],
    actor_id: str,
    now: str,
    *,
    upload_actor_id: str | None = None,
) -> None:
    """Snapshot row + completed upload record; chunk rows are inserted by the caller."""
    connection.execute(
        "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?, ?)",
        (snapshot_id, project_id, digest, json.dumps(manifest), now, actor_id),
    )
    connection.execute(
        """INSERT INTO snapshot_chunk_uploads
        (id, project_id, expected_digest, manifest_json, status, actor_principal_id,
         created_at, completed_at, expires_at)
        VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET status='completed', completed_at=excluded.completed_at""",
        (
            snapshot_id,
            project_id,
            digest,
            json.dumps(manifest),
            upload_actor_id or actor_id,
            now,
            now,
            now,
        ),
    )


def _expired(upload: sqlite3.Row) -> bool:
    return _parse_utc(upload["expires_at"]) <= datetime.now(UTC)


def collect_expired_uploads(connection: sqlite3.Connection, project_id: str) -> tuple[int, int]:
    """Delete expired 'uploading' uploads and their chunks; completed snapshots are kept."""
    stale = [
        row["id"]
        for row in connection.execute(
            "SELECT id, expires_at FROM snapshot_chunk_uploads "
            "WHERE project_id = ? AND status = 'uploading'",
            (project_id,),
        )
        if _expired(row)
    ]
    chunks = 0
    for upload_id in stale:
        chunks += connection.execute(
            "DELETE FROM snapshot_chunks WHERE owner_id = ?", (upload_id,)
        ).rowcount
        connection.execute("DELETE FROM snapshot_chunk_uploads WHERE id = ?", (upload_id,))
        discard_pending(connection, upload_id)
        discard_pending_code(connection, upload_id)
    return len(stale), chunks


def insert_chunks(
    connection: sqlite3.Connection, snapshot_id: str, path: str, content: bytes, seq: int
) -> int:
    """Insert one file's chunks starting at ``seq``; return the next seq."""
    for offset in range(0, len(content), CHUNK_BYTES):
        part = content[offset : offset + CHUNK_BYTES]
        connection.execute(
            "INSERT INTO snapshot_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, seq, path, offset, len(part), hashlib.sha256(part).hexdigest(), part),
        )
        seq += 1
    return seq


def source_chunk_rows(connection: sqlite3.Connection, owner: str, path: str) -> list[sqlite3.Row]:
    """Chunk metadata (no content) of one file under an owner, in offset order."""
    return connection.execute(
        "SELECT seq, offset, size, sha256 FROM snapshot_chunks "
        "WHERE owner_id = ? AND relative_path = ? ORDER BY offset",
        (owner, path),
    ).fetchall()


def chunk_rows_fingerprint(rows: list[sqlite3.Row]) -> str:
    """Hash over (offset, size, sha256) of chunk rows; reads no chunk content."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{row['offset']}:{row['size']}:{row['sha256']}\n".encode())
    return digest.hexdigest()


def copy_file_chunks_batched(
    database_path: Any,
    source_owner: str,
    source_path: str,
    dest_owner: str,
    dest_path: str,
    size: int,
    sha256: str,
    dest_seq: int,
    mismatch: str,
    *,
    batch: int = 8,
) -> tuple[int, str]:
    """Copy one file's chunk rows to another owner without holding the write lock for the copy.

    Chunks move ``batch`` at a time, each batch in its own short write transaction, one chunk
    in memory at a time, every chunk re-hashed on the way, the file's sha256 and size checked
    at the end. Returns the next free seq and the source's chunk fingerprint (see
    ``chunk_rows_fingerprint``). Any mismatch raises ``Conflict(mismatch)``; the caller owns
    cleaning up the partial destination.
    """
    reader = _connect(database_path)
    try:
        rows = source_chunk_rows(reader, source_owner, source_path)
        fingerprint = chunk_rows_fingerprint(rows)
        digest, total = hashlib.sha256(), 0
        for start in range(0, len(rows), batch):
            staged = []
            for row in rows[start : start + batch]:
                meta, content = read_chunk(reader, source_owner, row["seq"])
                if meta["offset"] != total:
                    raise Conflict(mismatch)
                digest.update(content)
                total += len(content)
                staged.append((dest_seq, meta["offset"], len(content), meta["sha256"], content))
                dest_seq += 1
            writer = _connect(database_path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                for seq, offset, length, chunk_sha, content in staged:
                    writer.execute(
                        "INSERT INTO snapshot_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (dest_owner, seq, dest_path, offset, length, chunk_sha, content),
                    )
                writer.execute("COMMIT")
            except BaseException:
                if writer.in_transaction:
                    writer.execute("ROLLBACK")
                raise
            finally:
                writer.close()
        if total != size or digest.hexdigest() != sha256:
            raise Conflict(mismatch)
        return dest_seq, fingerprint
    finally:
        reader.close()


class ChunkedSnapshotStore(SnapshotStore):
    def _human(self, connection, live, project_id):
        self._project(connection, live, project_id)
        if live["role"] != "human":
            raise Forbidden("A human must upload and adopt chunked snapshots.")

    def _upload(self, connection, project_id, upload_id) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM snapshot_chunk_uploads WHERE id = ? AND project_id = ?",
            (upload_id, project_id),
        ).fetchone()
        # A worker's large artifact upload shares this table; it is not a snapshot upload.
        if row is None or json.loads(row["manifest_json"]).get("kind") == "artifact":
            raise NotFound("Chunked snapshot upload was not found.")
        return row

    def begin_upload(
        self, actor: dict[str, Any], project_id: str, key: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            model = BeginChunkedSnapshot.model_validate(payload)
        except ValidationError as error:
            raise StoreError("Invalid chunked snapshot request.") from error
        body = _without_absent_class(model.model_dump(mode="json"))

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._human(connection, live, project_id)
            manifest = validate_chunked_manifest(body["inventory"], body["expected_digest"])
            ident, now = "snapshot_" + uuid.uuid4().hex, utc_now()
            collect_expired_uploads(connection, project_id)
            enforce_project_chunk_cap(connection, project_id, manifest["total_bytes"])
            connection.execute(
                """INSERT INTO snapshot_chunk_uploads
                (id, project_id, expected_digest, manifest_json, status, actor_principal_id,
                 created_at, completed_at, expires_at)
                VALUES (?, ?, ?, ?, 'uploading', ?, ?, NULL, ?)""",
                (
                    ident,
                    project_id,
                    body["expected_digest"],
                    json.dumps(manifest),
                    live["id"],
                    now,
                    _utc_string(datetime.now(UTC) + UPLOAD_TTL),
                ),
            )
            declare_pending(
                connection, live, ident, model.data_class, now, digest=body["expected_digest"]
            )
            code = declare_pending_code(
                connection, live, ident, model.code_approval, manifest["entries"], now
            )
            refuse_relabelled_files(
                connection,
                live["org_id"],
                ident,
                entries=manifest["entries"],
                digest=body["expected_digest"],
            )
            plan = chunk_plan(manifest["entries"])
            response = {
                "upload_id": ident,
                "project_id": project_id,
                "expected_digest": body["expected_digest"],
                "chunk_bytes": CHUNK_BYTES,
                "chunk_plan": plan,
                "total_bytes": manifest["total_bytes"],
            }
            if code is not None:
                response["code_approval"] = code
            return 201, response

        return self._mutate(
            actor,
            key,
            "POST",
            f"/projects/{project_id}/chunked-snapshots",
            canonical_digest(body),
            command,
        )

    def put_chunk(
        self,
        actor: dict[str, Any],
        project_id: str,
        upload_id: str,
        seq: int,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Naturally idempotent write: no idempotency record, so no bytes are replayed or kept."""
        try:
            body = PutSnapshotChunk.model_validate(payload)
        except ValidationError as error:
            raise StoreError("Invalid snapshot chunk request.") from error
        path = f"/projects/{project_id}/chunked-snapshots/{upload_id}/chunks/{seq}"
        connection = _connect(self.database_path)
        live: dict[str, Any] | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            live = self._live_principal(connection, actor)
            state = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if state is None or state["state"] != "active":
                raise Forbidden("Control state is held after restore; mutations are disabled.")
            self._human(connection, live, project_id)
            upload = self._upload(connection, project_id, upload_id)
            if upload["status"] != "uploading":
                raise Conflict("Chunked snapshot upload is already complete.")
            if _expired(upload):
                raise Conflict("Chunked snapshot upload has expired.")
            plan = chunk_plan(json.loads(upload["manifest_json"])["entries"])
            if not 0 <= seq < len(plan):
                raise NotFound("Chunk sequence is not part of the upload plan.")
            planned = plan[seq]
            try:
                content = base64.b64decode(body.content_base64, validate=True)
            except (binascii.Error, ValueError):
                raise StoreError("Chunk content is not valid base64.") from None
            digest = hashlib.sha256(content).hexdigest()
            if len(content) != planned["size"] or digest != body.sha256:
                raise Conflict("Chunk size or sha256 does not match the upload plan.")
            existing = connection.execute(
                "SELECT sha256 FROM snapshot_chunks WHERE owner_id = ? AND seq = ?",
                (upload_id, seq),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != digest:
                    raise Conflict("Chunk was already uploaded with different bytes.")
                status = 200
            else:
                connection.execute(
                    "INSERT INTO snapshot_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        upload_id,
                        seq,
                        planned["relative_path"],
                        planned["offset"],
                        planned["size"],
                        digest,
                        content,
                    ),
                )
                status = 201
            connection.execute("COMMIT")
            return status, {
                "upload_id": upload_id,
                "seq": seq,
                "size": planned["size"],
                "sha256": digest,
            }
        except Exception as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, Forbidden) and live is not None:
                self._record_denial(live, "PUT", path)
            raise
        finally:
            connection.close()

    def complete_upload(
        self,
        actor: dict[str, Any],
        project_id: str,
        upload_id: str,
        key: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            model = CompleteChunkedSnapshot.model_validate(payload or {})
        except ValidationError as error:
            raise StoreError("Invalid chunked snapshot completion.") from error
        request = _without_absent_class({"upload_id": upload_id, **model.model_dump(mode="json")})

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._human(connection, live, project_id)
            upload = self._upload(connection, project_id, upload_id)
            if upload["status"] != "uploading":
                raise Conflict("Chunked snapshot upload is already complete.")
            if _expired(upload):
                raise Conflict("Chunked snapshot upload has expired.")
            manifest = json.loads(upload["manifest_json"])
            missing = connection.execute(
                "SELECT count(*) FROM snapshot_chunks WHERE owner_id = ?", (upload_id,)
            ).fetchone()[0]
            if missing != len(chunk_plan(manifest["entries"])):
                raise Conflict("Chunked snapshot upload is missing chunks.")
            verify_chunk_content(connection, upload_id, manifest["entries"])
            now = utc_now()
            insert_chunked_snapshot_rows(
                connection,
                upload_id,
                project_id,
                upload["expected_digest"],
                manifest,
                live["id"],
                now,
                upload_actor_id=upload["actor_principal_id"],
            )
            record = record_snapshot_data_class(
                connection, live, upload_id, model.data_class, now=now
            )
            code = record_code_approval(connection, live, upload_id, model.code_approval, now=now)
            refuse_relabelled_files(connection, live["org_id"], upload_id)
            chunks = missing
            adopted = {
                "snapshot_id": upload_id,
                "digest": upload["expected_digest"],
                "file_count": len(manifest["entries"]),
                "total_bytes": manifest["total_bytes"],
                "provenance": "imported_unverified",
                "classification": "unclassified",
                "chunked": True,
                "chunk_count": chunks,
                "data_class": record,
            }
            if code is not None:
                adopted["code_approval"] = code
            self._event(
                connection, live, project_id, None, "workspace.snapshot_adopted", 1, adopted
            )
            response = {
                "id": upload_id,
                "project_id": project_id,
                "digest": upload["expected_digest"],
                "chunked": True,
                "file_count": len(manifest["entries"]),
                "total_bytes": manifest["total_bytes"],
                "chunk_count": chunks,
                "created_at": now,
                "actor_principal_id": live["id"],
                "transfer_authorized": False,
                "data_class": record,
            }
            if code is not None:
                response["code_approval"] = code
            return 201, response

        return self._mutate(
            actor,
            key,
            "POST",
            f"/projects/{project_id}/chunked-snapshots/{upload_id}/complete",
            canonical_digest(request),
            command,
        )

    def collect_garbage(
        self, actor: dict[str, Any], project_id: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        """Explicit human GC of expired, unfinished uploads; metadata-only response."""

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._human(connection, live, project_id)
            uploads, chunks = collect_expired_uploads(connection, project_id)
            return 200, {"expired_uploads_deleted": uploads, "chunks_deleted": chunks}

        return self._mutate(
            actor,
            key,
            "POST",
            f"/projects/{project_id}/chunked-snapshots/gc",
            canonical_digest({"gc": project_id}),
            command,
        )
