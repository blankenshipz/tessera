"""Control-plane placement of validated quarantine bytes into project storage."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifact_manifests import ArtifactManifestStore
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
        artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('staged', 'committed')),
        sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        staged_at TEXT NOT NULL,
        committed_at TEXT,
        reconciled_at TEXT
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
    artifacts: list[ProjectPlacementEntry] = Field(min_length=1, max_length=128)


class ProjectArtifactStore(ArtifactManifestStore):
    """Place verified quarantine BLOBs under a project binding, never a worker path."""

    def __init__(self, state_dir, backend: ProjectArtifactBackend | None = None) -> None:
        super().__init__(state_dir)
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
        path = f"/worker/leases/{lease_id}/project-artifacts"

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
            response = []
            for entry in sorted(body["artifacts"], key=lambda item: item["id"]):
                artifact = self._artifact(connection, lease_id, actor, entry)
                operation_key = self._operation_key(
                    grant["project_id"], artifact["id"], artifact["sha256"]
                )
                existing = connection.execute(
                    "SELECT * FROM project_artifact_placements WHERE artifact_id=?",
                    (artifact["id"],),
                ).fetchone()
                placement: ProjectArtifactPlacement | None = self.backend.reconcile(
                    actor["org_id"],
                    grant["project_id"],
                    artifact["id"],
                    artifact["sha256"],
                    artifact["size"],
                )
                if placement is None:
                    try:
                        placement = self.backend.commit(
                            self.backend.stage(
                                actor["org_id"],
                                grant["project_id"],
                                artifact["id"],
                                artifact["sha256"],
                                artifact["content"],
                            )
                        )
                    except ProjectArtifactStorageError as error:
                        raise Conflict(
                            "Project artifact storage could not commit bytes."
                        ) from error
                if existing is not None:
                    if any(
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
                    connection.execute(
                        "UPDATE project_artifact_placements SET reconciled_at=? "
                        "WHERE artifact_id=?",
                        (now, artifact["id"]),
                    )
                else:
                    connection.execute(
                        "INSERT INTO project_artifact_placements "
                        "(artifact_id,project_id,backend_kind,tenant_namespace,project_namespace,"
                        "object_key,sha256,size,retention_class,committed_at,reconciled_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            artifact["id"],
                            grant["project_id"],
                            self.backend.kind,
                            placement.tenant_namespace,
                            placement.project_namespace,
                            placement.object_key,
                            placement.sha256,
                            placement.size,
                            retention,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    "INSERT INTO project_artifact_storage_operations "
                    "(id,artifact_id,project_id,backend_kind,operation_key,state,sha256,size,"
                    "retention_class,staged_at,committed_at,reconciled_at) "
                    "VALUES (?,?,?,?,?,'committed',?,?,?,?,?,?) ON CONFLICT(artifact_id) "
                    "DO UPDATE SET state='committed', committed_at=excluded.committed_at, "
                    "reconciled_at=excluded.reconciled_at",
                    (
                        "project_storage_operation_"
                        + hashlib.sha256(operation_key.encode()).hexdigest()[:32],
                        artifact["id"],
                        grant["project_id"],
                        self.backend.kind,
                        operation_key,
                        artifact["sha256"],
                        artifact["size"],
                        retention,
                        now,
                        now,
                        now,
                    ),
                )
                response.append(
                    {
                        "artifact_id": artifact["id"],
                        "sha256": placement.sha256,
                        "size": placement.size,
                        "state": "committed",
                        "retention_class": retention,
                        "committed_at": now,
                    }
                )
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "worker.project_artifacts_committed",
                grant["revision"],
                {"lease_id": lease_id, "artifact_ids": [item["artifact_id"] for item in response]},
            )
            return 201, {"placements": response, "storage_backend": self.backend.kind}

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
