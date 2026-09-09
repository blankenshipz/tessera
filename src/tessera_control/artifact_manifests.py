"""Immutable, quarantined manifests over completed worker artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from .store import Conflict, NotFound, StoreError, _connect, canonical_digest, utc_now

MAX_MANIFEST_ENTRIES = 128
ArtifactId = Annotated[str, StringConstraints(min_length=1, max_length=200)]
PortablePath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


def create_artifact_manifest_schema(connection: sqlite3.Connection) -> None:
    """Install immutable manifest metadata; completed bytes remain in artifacts."""
    connection.execute(
        """CREATE TABLE artifact_manifests (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id) UNIQUE,
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        artifacts_json TEXT NOT NULL,
        total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0 AND total_bytes <= 16777216),
        manifest_digest TEXT NOT NULL CHECK (
            length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
        ),
        created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status = 'quarantined'),
        classification TEXT NOT NULL CHECK (classification = 'unclassified'),
        late INTEGER NOT NULL CHECK (late IN (0, 1))
        )"""
    )
    connection.execute(
        """CREATE INDEX artifact_manifests_by_project
        ON artifact_manifests(project_id, created_at, id)"""
    )


class ManifestArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: ArtifactId
    relative_path: PortablePath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    size: int = Field(strict=True, ge=0, le=MAX_ARTIFACT_BYTES)


class CreateArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    artifacts: list[ManifestArtifact] = Field(max_length=MAX_MANIFEST_ENTRIES)


def _metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["late"] = bool(value["late"])
    value["artifacts"] = json.loads(value.pop("artifacts_json"))
    return value


class ArtifactManifestStore(ArtifactStore):
    """Worker manifest admission and tenant-scoped metadata listing."""

    @staticmethod
    def _manifest_id(lease_id: str) -> str:
        return "artifact_manifest_" + hashlib.sha256(lease_id.encode()).hexdigest()[:32]

    @staticmethod
    def _canonical_payload(
        lease: sqlite3.Row, grant: sqlite3.Row, actor: dict[str, Any], entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "project_id": grant["project_id"],
            "task_id": grant["task_id"],
            "grant_id": grant["id"],
            "lease_id": lease["id"],
            "worker_principal_id": actor["id"],
            "epoch": lease["epoch"],
            "operation_id": ArtifactStore._expected_operation(lease["id"]),
            "plan_digest": grant["plan_digest"],
            "artifacts": entries,
        }

    def create_manifest(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = CreateArtifactManifest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact manifest request.") from error
        path = f"/worker/leases/{lease_id}/artifact-manifest"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            if body["expected_epoch"] != lease["epoch"]:
                raise Conflict("Artifact manifest epoch does not match the issued lease.")
            if body["operation_id"] != self._expected_operation(lease_id):
                raise Conflict("Artifact manifest operation does not match the lease.")
            plan, _ = self._plan(connection, grant)
            spec = plan["execution_spec"]
            paths = [entry["relative_path"] for entry in body["artifacts"]]
            if len(paths) != len(set(paths)) or set(paths) != set(spec["output_paths"]):
                raise Conflict("Artifact manifest does not match declared output paths.")
            entries = sorted(body["artifacts"], key=lambda entry: entry["relative_path"])
            total = sum(entry["size"] for entry in entries)
            limit = min(spec["max_output_bytes"], MAX_ARTIFACT_BYTES)
            if total > limit:
                raise Conflict("Artifact manifest output budget is exceeded.")

            verified: list[dict[str, Any]] = []
            any_late = False
            remaining = limit
            for entry in entries:
                row = connection.execute(
                    """SELECT id, relative_path, sha256, size, length(content) AS content_length,
                    typeof(content) AS content_type, late FROM artifacts
                    WHERE id=? AND lease_id=? AND worker_principal_id=? AND relative_path=?""",
                    (entry["id"], lease_id, actor["id"], entry["relative_path"]),
                ).fetchone()
                if row is None:
                    raise NotFound("Quarantined artifact was not found.")
                if (
                    row["sha256"] != entry["sha256"]
                    or row["size"] != entry["size"]
                    or row["content_type"] != "blob"
                    or row["content_length"] != row["size"]
                    or row["size"] > remaining
                ):
                    raise Conflict("Artifact manifest entry does not match quarantined artifact.")
                content = connection.execute(
                    """SELECT content FROM artifacts WHERE id=? AND lease_id=?
                    AND worker_principal_id=? AND relative_path=?""",
                    (entry["id"], lease_id, actor["id"], entry["relative_path"]),
                ).fetchone()["content"]
                if (
                    len(content) != row["size"]
                    or hashlib.sha256(content).hexdigest() != row["sha256"]
                ):
                    raise Conflict("Quarantined artifact integrity check failed.")
                remaining -= row["size"]
                any_late = any_late or bool(row["late"])
                verified.append(
                    {
                        "id": row["id"],
                        "relative_path": row["relative_path"],
                        "sha256": row["sha256"],
                        "size": row["size"],
                    }
                )
            # v22 only requires durable placement after a project binding exists.
            # Older SQLite-only artifacts intentionally remain legacy/unplaced.
            binding = connection.execute(
                "SELECT 1 FROM project_storage_bindings WHERE project_id=?", (grant["project_id"],)
            ).fetchone()
            if binding is not None:
                for entry in verified:
                    placement = connection.execute(
                        """SELECT sha256,size FROM project_artifact_placements
                        WHERE artifact_id=? AND project_id=?""",
                        (entry["id"], grant["project_id"]),
                    ).fetchone()
                    if (
                        placement is None
                        or placement["sha256"] != entry["sha256"]
                        or placement["size"] != entry["size"]
                    ):
                        raise Conflict(
                            "Artifact manifest requires committed project artifact placement."
                        )
            canonical = self._canonical_payload(lease, grant, actor, verified)
            manifest_digest = canonical_digest(canonical)
            existing = connection.execute(
                """SELECT id, project_id, task_id, grant_id, lease_id, worker_principal_id,
                epoch, operation_id, plan_digest, artifacts_json, total_bytes, manifest_digest,
                created_at, status, classification, late FROM artifact_manifests
                WHERE lease_id=?""",
                (lease_id,),
            ).fetchone()
            if existing is not None:
                if existing["manifest_digest"] != manifest_digest:
                    raise Conflict("Artifact manifest is immutable.")
                return 200, _metadata(existing)
            row = {
                "id": self._manifest_id(lease_id),
                "project_id": grant["project_id"],
                "task_id": grant["task_id"],
                "grant_id": grant["id"],
                "lease_id": lease_id,
                "worker_principal_id": actor["id"],
                "epoch": lease["epoch"],
                "operation_id": body["operation_id"],
                "plan_digest": grant["plan_digest"],
                "artifacts_json": json.dumps(verified, separators=(",", ":"), sort_keys=True),
                "total_bytes": total,
                "manifest_digest": manifest_digest,
                "created_at": utc_now(),
                "status": "quarantined",
                "classification": "unclassified",
                "late": int(bool(self._late(connection, grant, lease)) or any_late),
            }
            connection.execute(
                """INSERT INTO artifact_manifests
                (id, project_id, task_id, grant_id, lease_id, worker_principal_id, epoch,
                 operation_id, plan_digest, artifacts_json, total_bytes, manifest_digest,
                 created_at, status, classification, late)
                VALUES (:id,:project_id,:task_id,:grant_id,:lease_id,:worker_principal_id,:epoch,
                :operation_id,:plan_digest,:artifacts_json,:total_bytes,:manifest_digest,
                :created_at,:status,:classification,:late)""",
                row,
            )
            metadata = _metadata(row)
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "worker.artifact_manifest_quarantined",
                grant["revision"],
                {
                    "id": row["id"],
                    "lease_id": lease_id,
                    "manifest_digest": manifest_digest,
                    "artifact_ids": [entry["id"] for entry in verified],
                },
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

    def list_manifests(self, actor: dict[str, Any], project_id: str, offset: int, limit: int):
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            rows = connection.execute(
                """SELECT id, project_id, task_id, grant_id, lease_id, worker_principal_id,
                epoch, operation_id, plan_digest, artifacts_json, total_bytes, manifest_digest,
                created_at, status, classification, late FROM artifact_manifests
                WHERE project_id=? ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [_metadata(row) for row in rows]
        finally:
            connection.close()
