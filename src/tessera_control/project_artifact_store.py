"""Control-plane placement of validated quarantine bytes into project storage."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifact_manifests import ArtifactManifestStore
from .project_artifact_retention import require_project_artifact_reference_admissible
from .project_artifacts import (
    FilesystemProjectArtifactBackend,
    ProjectArtifactBackend,
    ProjectArtifactPlacement,
    ProjectArtifactStorageError,
)
from .store import Conflict, NotFound, StoreError, utc_now


def create_project_artifact_storage_schema(connection: sqlite3.Connection) -> None:
    """Install empty placement metadata; existing SQLite BLOBs stay legacy."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS project_storage_bindings (
        project_id TEXT PRIMARY KEY REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        bound_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS project_artifact_placements (
        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        tenant_namespace TEXT NOT NULL,
        project_namespace TEXT NOT NULL,
        object_key TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        committed_at TEXT NOT NULL,
        reconciled_at TEXT NOT NULL,
        UNIQUE(project_id, backend_kind, object_key)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS project_artifact_placements_by_project "
        "ON project_artifact_placements(project_id, committed_at, artifact_id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS project_artifact_storage_operations (
        id TEXT PRIMARY KEY,
        artifact_id TEXT UNIQUE REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('intent', 'reconciling', 'committed')),
        sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        intent_at TEXT NOT NULL,
        reconcile_started_at TEXT,
        committed_at TEXT,
        reconciled_at TEXT,
        last_error TEXT
        )"""
    )


class ProjectPlacementEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    size: int = Field(strict=True, ge=0, le=16 * 1024 * 1024)


class CommitProjectArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    artifacts: list[ProjectPlacementEntry] = Field(max_length=128)


class ProjectArtifactStore(ArtifactManifestStore):
    """Place verified quarantine BLOBs under a project binding, never a worker path."""

    def __init__(self, state_dir, backend: ProjectArtifactBackend | None = None) -> None:
        super().__init__(state_dir, backend)
        self.backend = backend or FilesystemProjectArtifactBackend(
            self.database_path.parent / "project-artifacts"
        )

    @staticmethod
    def _operation_key(project_id: str, artifact_id: str, sha256: str) -> str:
        return (
            "project-artifact-"
            + hashlib.sha256(f"v1:{project_id}:{artifact_id}:{sha256}".encode()).hexdigest()
        )

    @staticmethod
    def _placement(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _operation_id(operation_key: str) -> str:
        return (
            "project_storage_operation_" + hashlib.sha256(operation_key.encode()).hexdigest()[:32]
        )

    def _prepare(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Record authority intent only; no backend call may occur in this transaction."""
        path = f"/worker/leases/{lease_id}/project-artifacts/intents"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            if body["expected_epoch"] != lease["epoch"] or body[
                "operation_id"
            ] != self._expected_operation(lease_id):
                raise Conflict("Project artifact placement does not match the lease.")
            now = utc_now()
            binding = connection.execute(
                "SELECT * FROM project_storage_bindings WHERE project_id=?", (grant["project_id"],)
            ).fetchone()
            if binding is None:
                connection.execute(
                    "INSERT INTO project_storage_bindings "
                    "(project_id,backend_kind,retention_class,bound_at) VALUES (?,?,?,?)",
                    (grant["project_id"], self.backend.kind, "project_default", now),
                )
                retention = "project_default"
            elif binding["backend_kind"] != self.backend.kind:
                raise Conflict("Project storage binding does not match this backend.")
            else:
                retention = binding["retention_class"]
            prepared = []
            entries = sorted(body["artifacts"], key=lambda item: item["id"])
            if not entries:
                entries = [{"id": None, "sha256": hashlib.sha256(b"").hexdigest(), "size": 0}]
            for entry in entries:
                artifact = (
                    None
                    if entry["id"] is None
                    else self._artifact(connection, lease_id, actor, entry)
                )
                artifact_id = None if artifact is None else artifact["id"]
                sha256 = entry["sha256"] if artifact is None else artifact["sha256"]
                size = entry["size"] if artifact is None else artifact["size"]
                if artifact_id is not None:
                    retention_record = connection.execute(
                        "SELECT state FROM project_artifact_retention WHERE artifact_id=?",
                        (artifact_id,),
                    ).fetchone()
                    if retention_record is not None:
                        require_project_artifact_reference_admissible(
                            connection, grant["project_id"], artifact_id
                        )
                operation_key = self._operation_key(
                    grant["project_id"], artifact_id or f"zero:{lease_id}", sha256
                )
                existing = connection.execute(
                    "SELECT * FROM project_artifact_storage_operations WHERE operation_key=?",
                    (operation_key,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO project_artifact_storage_operations
                        (id,artifact_id,project_id,backend_kind,operation_key,state,sha256,size,
                         retention_class,intent_at,reconcile_started_at,committed_at,reconciled_at,last_error)
                        VALUES (?,?,?,?,?,'intent',?,?,?,?,NULL,NULL,NULL,NULL)""",
                        (
                            self._operation_id(operation_key),
                            artifact_id,
                            grant["project_id"],
                            self.backend.kind,
                            operation_key,
                            sha256,
                            size,
                            retention,
                            now,
                        ),
                    )
                elif (
                    existing["artifact_id"] != artifact_id
                    or existing["project_id"] != grant["project_id"]
                    or existing["backend_kind"] != self.backend.kind
                    or existing["sha256"] != sha256
                    or existing["size"] != size
                ):
                    raise Conflict(
                        "Project artifact storage intent conflicts with committed identity."
                    )
                prepared.append(
                    {
                        "artifact_id": artifact_id,
                        "sha256": sha256,
                        "size": size,
                        "operation_key": operation_key,
                        "retention_class": retention,
                    }
                )
            return 202, {
                "project_id": grant["project_id"],
                "task_id": grant["task_id"],
                "retention_class": retention,
                "operations": prepared,
            }

        return self._mutate(worker, key, "POST", path, digest, command, audience="worker")

    def _mark_reconciling(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, operations: list[dict]
    ) -> None:
        """Durably expose that an external object operation may have happened."""

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            self._grant_for_upload(connection, actor, lease_id)
            now = utc_now()
            for operation in operations:
                connection.execute(
                    """UPDATE project_artifact_storage_operations
                    SET state=CASE WHEN state='committed' THEN state ELSE 'reconciling' END,
                    reconcile_started_at=COALESCE(reconcile_started_at, ?)
                    WHERE operation_key=?""",
                    (now, operation["operation_key"]),
                )
            return 200, {"state": "reconciling"}

        self._mutate(
            worker,
            f"placement-reconcile:{key}",
            "POST",
            f"/worker/leases/{lease_id}/project-artifacts/reconcile",
            digest,
            command,
            audience="worker",
        )

    def _artifact(
        self,
        connection: sqlite3.Connection,
        lease_id: str,
        actor: dict[str, Any],
        entry: dict[str, Any],
    ):
        row = connection.execute(
            """SELECT a.*, p.org_id FROM artifacts a JOIN projects p ON p.id=a.project_id
            WHERE a.id=? AND a.lease_id=? AND a.worker_principal_id=?""",
            (entry["id"], lease_id, actor["id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Quarantined artifact was not found.")
        if (
            row["status"] != "quarantined"
            or row["classification"] != "unclassified"
            or row["sha256"] != entry["sha256"]
            or row["size"] != entry["size"]
            or type(row["content"]) is not bytes
            or len(row["content"]) != row["size"]
            or hashlib.sha256(row["content"]).hexdigest() != row["sha256"]
        ):
            raise Conflict("Quarantined artifact integrity check failed.")
        return row

    def commit_project_artifacts(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ):
        try:
            body = CommitProjectArtifacts.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid project artifact placement request.") from error
        if len({item["id"] for item in body["artifacts"]}) != len(body["artifacts"]):
            raise StoreError("Project artifact placement contains duplicate artifacts.")
        prepared = self._prepare(worker, lease_id, f"placement-intent:{key}", digest, body)[1]
        # Backend calls deliberately happen after the intent transaction commits.
        # A crash at any point leaves an intent that this same request can reconcile.
        self._mark_reconciling(worker, lease_id, key, digest, prepared["operations"])
        placements: dict[str, ProjectArtifactPlacement] = {}
        for operation in prepared["operations"]:
            if operation["artifact_id"] is None:
                continue
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
            try:
                artifact = connection.execute(
                    "SELECT content FROM artifacts WHERE id=?", (operation["artifact_id"],)
                ).fetchone()
                if artifact is None or type(artifact["content"]) is not bytes:
                    raise Conflict("Quarantined artifact was not found.")
                placement = self.backend.reconcile(
                    worker["org_id"],
                    prepared["project_id"],
                    operation["artifact_id"],
                    operation["sha256"],
                    operation["size"],
                )
                if placement is None:
                    placement = self.backend.commit(
                        self.backend.stage(
                            worker["org_id"],
                            prepared["project_id"],
                            operation["artifact_id"],
                            operation["sha256"],
                            artifact["content"],
                        )
                    )
                placements[operation["artifact_id"]] = placement
            except ProjectArtifactStorageError as error:
                raise Conflict("Project artifact storage could not commit bytes.") from error
            finally:
                connection.close()
        path = f"/worker/leases/{lease_id}/project-artifacts"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            if body["expected_epoch"] != lease["epoch"] or body[
                "operation_id"
            ] != self._expected_operation(lease_id):
                raise Conflict("Project artifact placement does not match the lease.")
            now, response, inserted = utc_now(), [], False
            for operation in prepared["operations"]:
                artifact_id = operation["artifact_id"]
                if artifact_id is None:
                    placement = None
                else:
                    placement = placements[artifact_id]
                    existing = connection.execute(
                        "SELECT * FROM project_artifact_placements WHERE artifact_id=?",
                        (artifact_id,),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """INSERT INTO project_artifact_placements
                            (artifact_id,project_id,backend_kind,tenant_namespace,project_namespace,object_key,sha256,size,retention_class,committed_at,reconciled_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                artifact_id,
                                grant["project_id"],
                                self.backend.kind,
                                placement.tenant_namespace,
                                placement.project_namespace,
                                placement.object_key,
                                placement.sha256,
                                placement.size,
                                operation["retention_class"],
                                now,
                                now,
                            ),
                        )
                        connection.execute(
                            """INSERT INTO project_artifact_retention
                            (artifact_id,project_id,retention_class,state,created_at,activity_at,
                             tombstoned_at,delete_intent_at,deleted_at,incident_at,incident_reason)
                            VALUES (?,? ,?,'active',?,?,NULL,NULL,NULL,NULL,NULL)""",
                            (
                                artifact_id,
                                grant["project_id"],
                                operation["retention_class"],
                                now,
                                now,
                            ),
                        )
                        inserted = True
                    elif any(
                        existing[name] != getattr(placement, name)
                        for name in (
                            "tenant_namespace",
                            "project_namespace",
                            "object_key",
                            "sha256",
                            "size",
                        )
                    ):
                        raise Conflict(
                            "Project artifact placement conflicts with committed identity."
                        )
                    else:
                        require_project_artifact_reference_admissible(
                            connection, grant["project_id"], artifact_id
                        )
                        connection.execute(
                            "UPDATE project_artifact_placements SET reconciled_at=? "
                            "WHERE artifact_id=?",
                            (now, artifact_id),
                        )
                connection.execute(
                    """UPDATE project_artifact_storage_operations
                    SET state='committed', reconcile_started_at=COALESCE(reconcile_started_at, ?),
                    committed_at=COALESCE(committed_at, ?), reconciled_at=?, last_error=NULL
                    WHERE operation_key=? AND state IN ('intent','reconciling','committed')""",
                    (now, now, now, operation["operation_key"]),
                )
                response.append(
                    {
                        "artifact_id": artifact_id,
                        "sha256": operation["sha256"],
                        "size": operation["size"],
                        "state": "committed",
                        "retention_class": operation["retention_class"],
                        "committed_at": now,
                    }
                )
            if inserted or any(item["artifact_id"] is None for item in response):
                self._event(
                    connection,
                    actor,
                    grant["project_id"],
                    grant["task_id"],
                    "worker.project_artifacts_committed",
                    grant["revision"],
                    {
                        "lease_id": lease_id,
                        "artifact_ids": [item["artifact_id"] for item in response],
                        "explicit_zero_output": not body["artifacts"],
                    },
                )
            return (201 if inserted or not body["artifacts"] else 200), {
                "placements": response,
                "storage_backend": self.backend.kind,
            }

        return self._mutate(worker, key, "POST", path, digest, command, audience="worker")
