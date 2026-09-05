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


PortablePath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class UploadArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    relative_path: PortablePath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    content_base64: str = Field(min_length=0, max_length=MAX_BASE64_CHARS)


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
    value["late"] = bool(value["late"])
    return value


class ArtifactStore(Store):
    """Storage boundary for worker uploads and control metadata reads."""

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _decode(payload: dict[str, Any]) -> bytes:
        encoded = payload["content_base64"]
        # This check intentionally precedes decoding, which bounds allocation.
        if len(encoded) > MAX_BASE64_CHARS:
            raise StoreError("Artifact content exceeds its bound.")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as error:
            raise StoreError("Artifact content is not valid base64.") from error
        if len(content) > MAX_ARTIFACT_BYTES:
            raise StoreError("Artifact content exceeds its bound.")
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

    def upload_artifact(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = UploadArtifact.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact upload request.") from error
        path = f"/worker/leases/{lease_id}/artifacts"
        content = self._decode(body)
        if hashlib.sha256(content).hexdigest() != body["sha256"]:
            raise Conflict("Artifact checksum does not match content.")
        _portable_path(body["relative_path"])
        expected_operation = hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
        if body["operation_id"] != expected_operation:
            raise Conflict("Artifact operation does not match the lease.")

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            if body["expected_epoch"] != lease["epoch"]:
                raise Conflict("Artifact epoch does not match the issued lease.")
            plan, _ = self._plan(connection, grant)
            spec = plan["execution_spec"]
            if body["relative_path"] not in spec["output_paths"]:
                raise Conflict("Artifact path is not declared by the approved plan.")
            limit = min(spec["max_output_bytes"], MAX_ARTIFACT_BYTES)
            used = connection.execute(
                "SELECT coalesce(sum(size), 0) FROM artifacts WHERE lease_id = ?", (lease_id,)
            ).fetchone()[0]
            existing = connection.execute(
                "SELECT 1 FROM artifacts WHERE lease_id = ? AND relative_path = ? LIMIT 1",
                (lease_id, body["relative_path"]),
            ).fetchone()
            if existing is not None:
                raise Conflict("Artifact path is immutable.")
            if used + len(content) > limit:
                raise Conflict("Artifact output budget is exceeded.")
            task = connection.execute(
                "SELECT revision, status, current_plan_digest FROM tasks WHERE id = ?",
                (grant["task_id"],),
            ).fetchone()
            late = True
            try:
                late = (
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
                late = True
            row = {
                "id": "artifact_"
                + hashlib.sha256(f"{lease_id}:{body['relative_path']}".encode()).hexdigest()[:32],
                "project_id": grant["project_id"],
                "task_id": grant["task_id"],
                "grant_id": grant["id"],
                "lease_id": lease_id,
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
                "late": int(late),
            }
            connection.execute(
                """INSERT INTO artifacts
                (id, project_id, task_id, grant_id, lease_id, worker_principal_id, epoch,
                 operation_id, relative_path, sha256, size, content, created_at, status,
                 classification, late)
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
            return 201, metadata

        def replay_guard(
            connection: sqlite3.Connection, actor: dict[str, Any], response: dict[str, Any]
        ):
            self._grant_for_upload(connection, actor, lease_id)

        return self._mutate(
            worker, key, "POST", path, digest, command, audience="worker", replay_guard=replay_guard
        )

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
