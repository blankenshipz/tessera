"""Quarantined, immutable worker artifact uploads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .migrations import plan_digest, plan_payload
from .models import ExecutionSpec
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    utc_now,
)

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_ARTIFACT_BYTES + 2) // 3)
MAX_CHUNK_BYTES = 256 * 1024
MAX_CHUNK_BASE64_CHARS = 4 * ((MAX_CHUNK_BYTES + 2) // 3)


def create_artifact_schema(connection: sqlite3.Connection) -> None:
    """Install the artifact table using individually transactional DDL."""
    connection.execute(
        """CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        size INTEGER NOT NULL CHECK (size >= 0),
        content BLOB NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status = 'quarantined'),
        classification TEXT NOT NULL CHECK (classification = 'unclassified'),
        late INTEGER NOT NULL CHECK (late IN (0, 1)),
        UNIQUE (lease_id, relative_path)
        )"""
    )
    connection.execute("CREATE INDEX artifacts_by_project ON artifacts(project_id, created_at, id)")
    connection.execute("CREATE INDEX artifacts_by_lease ON artifacts(lease_id, created_at, id)")


def create_artifact_upload_schema(connection: sqlite3.Connection) -> None:
    """Install bounded resumable staging records separately from completed artifacts."""
    connection.execute(
        """CREATE TABLE artifact_uploads (
        id TEXT PRIMARY KEY,
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0 AND size <= 16777216),
        received_bytes INTEGER NOT NULL DEFAULT 0 CHECK (
            received_bytes >= 0 AND received_bytes <= size
        ),
        staged_content BLOB,
        status TEXT NOT NULL CHECK (status IN ('uploading', 'completed')),
        created_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE (lease_id, relative_path)
        )"""
    )
    connection.execute(
        "CREATE INDEX artifact_uploads_by_lease ON artifact_uploads(lease_id, status, id)"
    )


PortablePath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class UploadArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    relative_path: PortablePath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    content_base64: str = Field(min_length=0, max_length=MAX_BASE64_CHARS)


class BeginArtifactUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    relative_path: PortablePath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    size: int = Field(strict=True, ge=0, le=MAX_ARTIFACT_BYTES)


class UploadChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    offset: int = Field(strict=True, ge=0, le=MAX_ARTIFACT_BYTES)
    content_base64: str = Field(min_length=1, max_length=MAX_CHUNK_BASE64_CHARS)


class EmptyUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _portable_path(value: str) -> str:
    if len(value.encode("utf-8")) > 4096 or "\x00" in value or "\\" in value:
        raise StoreError("Artifact path is not portable.")
    if len(value.split("/")) > 64 or value.startswith("/"):
        raise StoreError("Artifact path is not portable.")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise StoreError("Artifact path is not portable.")
    if len(value) >= 2 and value[1] == ":":
        raise StoreError("Artifact path is not portable.")
    return value


def _metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.pop("content", None)
    value.pop("staged_content", None)
    if "late" in value:
        value["late"] = bool(value["late"])
    return value


def _upload_metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = _metadata(row)
    return {
        key: value[key]
        for key in ("id", "lease_id", "relative_path", "sha256", "size", "received_bytes", "status")
    }


class ArtifactStore(Store):
    """Storage boundary for worker uploads and control metadata reads."""

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _decode(encoded: str, limit: int = MAX_ARTIFACT_BYTES, *, nonempty: bool = False) -> bytes:
        # This check intentionally precedes decoding, which bounds allocation.
        if len(encoded) > 4 * ((limit + 2) // 3):
            raise StoreError("Artifact content exceeds its bound.")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as error:
            raise StoreError("Artifact content is not valid base64.") from error
        if len(content) > limit:
            raise StoreError("Artifact content exceeds its bound.")
        if nonempty and not content:
            raise StoreError("Artifact chunk is empty.")
        return content

    @staticmethod
    def _grant_for_upload(connection: sqlite3.Connection, worker: dict[str, Any], lease_id: str):
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id = ?", (lease_id,)
        ).fetchone()
        if lease is None or lease["worker_principal_id"] != worker["id"]:
            raise NotFound("Execution lease was not found.")
        grant = connection.execute(
            """SELECT g.* FROM execution_grants g JOIN projects p ON p.id = g.project_id
            WHERE g.id = ? AND p.org_id = ?""",
            (lease["grant_id"], worker["org_id"]),
        ).fetchone()
        if grant is None:
            raise NotFound("Execution grant was not found.")
        if (
            grant["worker_principal_id"] != worker["id"]
            or worker.get("project_id") != grant["project_id"]
        ):
            raise Forbidden("Worker is not assigned to this execution grant.")
        return lease, grant

    @staticmethod
    def _plan(connection: sqlite3.Connection, grant: sqlite3.Row) -> tuple[dict[str, Any], int]:
        revision = connection.execute(
            """SELECT * FROM task_revisions WHERE task_id = ? AND plan_digest = ?
            AND revision < ? ORDER BY revision DESC LIMIT 1""",
            (grant["task_id"], grant["plan_digest"], grant["task_revision"]),
        ).fetchone()
        if revision is None or revision["execution_spec_json"] is None:
            raise Conflict("Approved execution plan is missing.")
        if (
            connection.execute(
                """SELECT 1 FROM task_approvals WHERE task_id = ? AND plan_revision = ?
            AND plan_digest = ?""",
                (grant["task_id"], revision["revision"], grant["plan_digest"]),
            ).fetchone()
            is None
        ):
            raise Conflict("Approved execution plan is missing.")
        try:
            spec = json.loads(revision["execution_spec_json"])
            ExecutionSpec.model_validate(spec)
            expected = plan_digest(
                revision["title"],
                revision["purpose"],
                revision["budget_cents"],
                revision["max_duration_seconds"],
                spec,
            )
            if expected != grant["plan_digest"]:
                raise ValueError
            plan = plan_payload(
                revision["title"],
                revision["purpose"],
                revision["budget_cents"],
                revision["max_duration_seconds"],
                spec,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise Conflict("Approved execution plan checksum is invalid.") from error
        return plan, revision["revision"]

    @staticmethod
    def _expected_operation(lease_id: str) -> str:
        return hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]

    @staticmethod
    def _artifact_id(lease_id: str, relative_path: str) -> str:
        return "artifact_" + hashlib.sha256(f"{lease_id}:{relative_path}".encode()).hexdigest()[:32]

    @staticmethod
    def _upload_id(lease_id: str, relative_path: str) -> str:
        return (
            "artifact_upload_"
            + hashlib.sha256(f"{lease_id}:{relative_path}".encode()).hexdigest()[:32]
        )

    def _validate_operation(
        self,
        connection: sqlite3.Connection,
        grant: sqlite3.Row,
        lease: sqlite3.Row,
        body: dict[str, Any],
    ) -> None:
        if body["expected_epoch"] != lease["epoch"]:
            raise Conflict("Artifact epoch does not match the issued lease.")
        # Once control has issued an operation, its durable identity supersedes
        # the old local-CPU lease-derived value.  The latter remains only for a
        # genuinely legacy CPU lease with no operation-intent record at all.
        operation = connection.execute(
            """SELECT o.*,p.org_id AS operation_project_org_id FROM external_operations o
            JOIN projects p ON p.id=o.project_id WHERE o.lease_id=?""",
            (lease["id"],),
        ).fetchone()
        if operation is None:
            if body["operation_id"] != self._expected_operation(lease["id"]):
                raise Conflict("Artifact operation does not match the legacy CPU lease.")
        elif (
            operation["org_id"] != operation["operation_project_org_id"]
            or operation["project_id"] != grant["project_id"]
            or operation["task_id"] != grant["task_id"]
            or operation["grant_id"] != grant["id"]
            or operation["worker_principal_id"] != lease["worker_principal_id"]
            or operation["epoch"] != lease["epoch"]
            or body["operation_id"] != operation["operation_id"]
        ):
            raise Conflict("Artifact operation does not match the control-issued lease operation.")

    def _validate_declaration(
        self,
        connection: sqlite3.Connection,
        grant: sqlite3.Row,
        lease: sqlite3.Row,
        body: dict[str, Any],
    ) -> int:
        self._validate_operation(connection, grant, lease, body)
        _portable_path(body["relative_path"])
        plan, _ = self._plan(connection, grant)
        spec = plan["execution_spec"]
        if body["relative_path"] not in spec["output_paths"]:
            raise Conflict("Artifact path is not declared by the approved plan.")
        return min(spec["max_output_bytes"], MAX_ARTIFACT_BYTES)

    @staticmethod
    def _reserved_bytes(connection: sqlite3.Connection, lease_id: str) -> int:
        artifact_bytes = connection.execute(
            "SELECT coalesce(sum(size), 0) FROM artifacts WHERE lease_id = ?", (lease_id,)
        ).fetchone()[0]
        staged_bytes = connection.execute(
            """SELECT coalesce(sum(size), 0) FROM artifact_uploads
            WHERE lease_id = ? AND status = 'uploading'""",
            (lease_id,),
        ).fetchone()[0]
        return artifact_bytes + staged_bytes

    def _late(self, connection: sqlite3.Connection, grant: sqlite3.Row, lease: sqlite3.Row) -> int:
        task = connection.execute(
            "SELECT revision, status, current_plan_digest FROM tasks WHERE id = ?",
            (grant["task_id"],),
        ).fetchone()
        try:
            return int(
                grant["status"] != "active"
                or _parse_utc(grant["expires_at"]) <= self._now()
                or _parse_utc(lease["expires_at"]) <= self._now()
                or grant["epoch"] != lease["epoch"]
                or task is None
                or task["revision"] != grant["task_revision"]
                or task["status"] != "approved"
                or task["current_plan_digest"] != grant["plan_digest"]
            )
        except (TypeError, ValueError, KeyError):
            return 1

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        lease: sqlite3.Row,
        grant: sqlite3.Row,
        body: dict[str, Any],
        content: bytes,
    ) -> dict[str, Any]:
        limit = self._validate_declaration(connection, grant, lease, body)
        if (
            connection.execute(
                "SELECT 1 FROM artifacts WHERE lease_id = ? AND relative_path = ?",
                (lease["id"], body["relative_path"]),
            ).fetchone()
            is not None
        ):
            raise Conflict("Artifact path is immutable.")
        if self._reserved_bytes(connection, lease["id"]) + len(content) > limit:
            raise Conflict("Artifact output budget is exceeded.")
        row = {
            "id": self._artifact_id(lease["id"], body["relative_path"]),
            "project_id": grant["project_id"],
            "task_id": grant["task_id"],
            "grant_id": grant["id"],
            "lease_id": lease["id"],
            "worker_principal_id": actor["id"],
            "epoch": lease["epoch"],
            "operation_id": body["operation_id"],
            "relative_path": body["relative_path"],
            "sha256": body["sha256"],
            "size": len(content),
            "content": content,
            "created_at": utc_now(),
            "status": "quarantined",
            "classification": "unclassified",
            "late": self._late(connection, grant, lease),
        }
        connection.execute(
            """INSERT INTO artifacts (id, project_id, task_id, grant_id, lease_id,
            worker_principal_id, epoch, operation_id, relative_path, sha256, size, content,
            created_at, status, classification, late)
            VALUES (:id,:project_id,:task_id,:grant_id,:lease_id,:worker_principal_id,:epoch,
            :operation_id,:relative_path,:sha256,:size,:content,:created_at,:status,
            :classification,:late)""",
            row,
        )
        metadata = _metadata(row)
        self._event(
            connection,
            actor,
            grant["project_id"],
            grant["task_id"],
            "worker.artifact_quarantined",
            grant["revision"],
            metadata,
        )
        return metadata

    def upload_artifact(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = UploadArtifact.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact upload request.") from error
        path = f"/worker/leases/{lease_id}/artifacts"
        content = self._decode(body["content_base64"])
        if hashlib.sha256(content).hexdigest() != body["sha256"]:
            raise Conflict("Artifact checksum does not match content.")

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            if (
                connection.execute(
                    "SELECT 1 FROM artifact_uploads WHERE lease_id=? AND relative_path=?",
                    (lease_id, body["relative_path"]),
                ).fetchone()
                is not None
            ):
                raise Conflict("Artifact path is immutable.")
            return 201, self._insert_artifact(connection, actor, lease, grant, body, content)

        def replay_guard(
            connection: sqlite3.Connection, actor: dict[str, Any], response: dict[str, Any]
        ):
            self._grant_for_upload(connection, actor, lease_id)

        return self._mutate(
            worker, key, "POST", path, digest, command, audience="worker", replay_guard=replay_guard
        )

    def begin_upload(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = BeginArtifactUpload.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact upload request.") from error
        path = f"/worker/leases/{lease_id}/artifact-uploads"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            limit = self._validate_declaration(connection, grant, lease, body)
            existing = connection.execute(
                """SELECT id, lease_id, worker_principal_id, epoch, operation_id, relative_path,
                sha256, size, received_bytes, status FROM artifact_uploads
                WHERE lease_id=? AND relative_path=?""",
                (lease_id, body["relative_path"]),
            ).fetchone()
            if existing is not None:
                if any(
                    existing[field] != body["expected_epoch" if field == "epoch" else field]
                    for field in ("epoch", "operation_id", "sha256", "size")
                ):
                    raise Conflict("Artifact path is immutable.")
                return 200, _upload_metadata(existing)
            if (
                connection.execute(
                    "SELECT 1 FROM artifacts WHERE lease_id=? AND relative_path=?",
                    (lease_id, body["relative_path"]),
                ).fetchone()
                is not None
            ):
                raise Conflict("Artifact path is immutable.")
            if self._reserved_bytes(connection, lease_id) + body["size"] > limit:
                raise Conflict("Artifact output budget is exceeded.")
            row = {
                "id": self._upload_id(lease_id, body["relative_path"]),
                "lease_id": lease_id,
                "worker_principal_id": actor["id"],
                "epoch": lease["epoch"],
                "operation_id": body["operation_id"],
                "relative_path": body["relative_path"],
                "sha256": body["sha256"],
                "size": body["size"],
                "received_bytes": 0,
                "staged_content": b"",
                "status": "uploading",
                "created_at": utc_now(),
                "completed_at": None,
            }
            connection.execute(
                """INSERT INTO artifact_uploads
                (id, lease_id, worker_principal_id, epoch, operation_id, relative_path, sha256,
                 size, received_bytes, staged_content, status, created_at, completed_at)
                VALUES (:id,:lease_id,:worker_principal_id,:epoch,:operation_id,:relative_path,
                :sha256,
                :size,:received_bytes,:staged_content,:status,:created_at,:completed_at)""",
                row,
            )
            metadata = _upload_metadata(row)
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "worker.artifact_upload_begun",
                grant["revision"],
                metadata,
            )
            return 201, metadata

        return self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=lambda connection, actor, _: self._grant_for_upload(
                connection, actor, lease_id
            ),
        )

    def append_chunk(
        self,
        worker: dict[str, Any],
        lease_id: str,
        upload_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = UploadChunk.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact chunk request.") from error
        content = self._decode(body["content_base64"], MAX_CHUNK_BYTES, nonempty=True)
        path = f"/worker/leases/{lease_id}/artifact-uploads/{upload_id}/chunks"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            self._grant_for_upload(connection, actor, lease_id)
            row = connection.execute(
                """SELECT id, lease_id, worker_principal_id, size, received_bytes, status
                FROM artifact_uploads
                WHERE id=? AND lease_id=? AND worker_principal_id=?""",
                (upload_id, lease_id, actor["id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Artifact upload was not found.")
            if row["status"] != "uploading" or body["offset"] != row["received_bytes"]:
                raise Conflict("Artifact chunk offset is stale.")
            if body["offset"] + len(content) > row["size"]:
                raise Conflict("Artifact chunk exceeds its declared size.")
            connection.execute(
                """UPDATE artifact_uploads SET staged_content=CAST(staged_content || ? AS BLOB),
                received_bytes=received_bytes + ? WHERE id=?""",
                (content, len(content), upload_id),
            )
            updated = connection.execute(
                """SELECT id, lease_id, relative_path, sha256, size, received_bytes, status
                FROM artifact_uploads WHERE id=?""",
                (upload_id,),
            ).fetchone()
            metadata = _upload_metadata(updated)
            grant = self._grant_for_upload(connection, actor, lease_id)[1]
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "worker.artifact_upload_progressed",
                grant["revision"],
                metadata,
            )
            return 200, metadata

        return self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=lambda connection, actor, _: self._grant_for_upload(
                connection, actor, lease_id
            ),
        )

    def complete_upload(
        self, worker: dict[str, Any], lease_id: str, upload_id: str, key: str, digest: str
    ) -> tuple[int, dict[str, Any]]:
        path = f"/worker/leases/{lease_id}/artifact-uploads/{upload_id}/complete"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            row = connection.execute(
                """SELECT * FROM artifact_uploads
                WHERE id=? AND lease_id=? AND worker_principal_id=?""",
                (upload_id, lease_id, actor["id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Artifact upload was not found.")
            existing = connection.execute(
                """SELECT id, project_id, task_id, grant_id, lease_id, worker_principal_id,
                epoch, operation_id, relative_path, sha256, size, created_at, status,
                classification, late FROM artifacts WHERE lease_id=? AND relative_path=?""",
                (lease_id, row["relative_path"]),
            ).fetchone()
            if row["status"] == "completed":
                if existing is None:
                    raise Conflict("Completed artifact upload is inconsistent.")
                return 200, _metadata(existing)
            if (
                row["received_bytes"] != row["size"]
                or row["staged_content"] is None
                or len(row["staged_content"]) != row["size"]
                or hashlib.sha256(row["staged_content"]).hexdigest() != row["sha256"]
            ):
                raise Conflict("Artifact upload size or checksum does not match.")
            body = {
                "expected_epoch": row["epoch"],
                "operation_id": row["operation_id"],
                "relative_path": row["relative_path"],
                "sha256": row["sha256"],
            }
            # Remove the reservation immediately before shared finalization. Both writes are atomic.
            connection.execute(
                """UPDATE artifact_uploads SET status='completed', completed_at=?,
                staged_content=NULL WHERE id=?""",
                (utc_now(), upload_id),
            )
            return 201, self._insert_artifact(
                connection, actor, lease, grant, body, row["staged_content"]
            )

        return self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=lambda connection, actor, _: self._grant_for_upload(
                connection, actor, lease_id
            ),
        )

    def upload_status(
        self, worker: dict[str, Any], lease_id: str, upload_id: str, key: str, digest: str
    ) -> tuple[int, dict[str, Any]]:
        path = f"/worker/leases/{lease_id}/artifact-uploads/{upload_id}/status"

        def response(connection: sqlite3.Connection, actor: dict[str, Any]) -> dict[str, Any]:
            self._grant_for_upload(connection, actor, lease_id)
            row = connection.execute(
                """SELECT id, lease_id, relative_path, sha256, size, received_bytes, status
                FROM artifact_uploads WHERE id=? AND lease_id=? AND worker_principal_id=?""",
                (upload_id, lease_id, actor["id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Artifact upload was not found.")
            return _upload_metadata(row)

        refreshed: dict[str, Any] = {}

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            refreshed["response"] = response(connection, actor)
            return 200, refreshed["response"]

        def replay_guard(
            connection: sqlite3.Connection, actor: dict[str, Any], _: dict[str, Any]
        ) -> None:
            refreshed["response"] = response(connection, actor)

        status, _ = self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=replay_guard,
        )
        return status, refreshed["response"]

    def list_artifacts(self, actor: dict[str, Any], project_id: str, offset: int, limit: int):
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            rows = connection.execute(
                """SELECT id, project_id, task_id, grant_id, lease_id,
                worker_principal_id, epoch, operation_id, relative_path, sha256, size,
                created_at, status, classification, late
                FROM artifacts WHERE project_id = ?
                ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [_metadata(row) for row in rows]
        finally:
            connection.close()
