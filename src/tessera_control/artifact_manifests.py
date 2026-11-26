"""Immutable, quarantined manifests over completed worker artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .artifact_limits import ABSOLUTE_MAX_ARTIFACT_BYTES, operator_limits
from .artifacts import ArtifactStore
from .project_artifact_retention import require_project_artifact_reference_admissible
from .project_artifacts import (
    FilesystemProjectArtifactBackend,
    ProjectArtifactBackend,
    ProjectArtifactPlacement,
    ProjectArtifactStorageError,
    verify_project_artifact_placement,
)
from .snapshot_chunks import check_chunk_metadata
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
    size: int = Field(strict=True, ge=0, le=ABSOLUTE_MAX_ARTIFACT_BYTES)


class CreateArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    artifacts: list[ManifestArtifact] = Field(max_length=MAX_MANIFEST_ENTRIES)
    # An attempt that ended interrupted or retryable lists only the declared outputs that
    # reached custody (the diagnostic may be missing). Admission still hash-verifies each one.
    partial: bool = False


def manifest_is_partial(artifacts: list[dict[str, Any]], output_paths: Any) -> bool:
    """True when a manifest lists fewer files than the plan declares.

    The signal is the content itself, not a stored flag: a manifest that lacks any declared
    output can never stand for a complete run, whoever wrote it."""
    return {item["relative_path"] for item in artifacts} != set(output_paths)


def _attempt_interrupted(connection: sqlite3.Connection, lease_id: str, operation_id: str) -> bool:
    """True when the Salad attempt holding this lease and operation ended interrupted: its
    route is flagged reallocated and the provider's own reads confirmed the stop. A lease with
    no Salad attempt (any ordinary run) is never interrupted."""
    from .salad_termination import provider_termination

    attempt = connection.execute(
        "SELECT route_id FROM campaign_salad_attempts WHERE lease_id=? AND operation_id=?",
        (lease_id, operation_id),
    ).fetchone()
    if attempt is None or attempt["route_id"] is None:
        return False
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type='remote_execution.state'
        AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC LIMIT 1""",
        (attempt["route_id"],),
    ).fetchone()
    if row is None:
        return False
    route = json.loads(row["payload_json"])
    return route.get("reallocated") is True and provider_termination(route).confirmed


def grant_manifest_is_partial(
    connection: sqlite3.Connection, grant: sqlite3.Row | dict[str, Any], artifacts_json: str
) -> bool:
    """Whether the manifest stored for ``grant`` lists fewer files than its approved plan
    declares. An unreadable plan counts as partial: nothing then passes as complete."""
    revision = connection.execute(
        """SELECT execution_spec_json FROM task_revisions WHERE task_id = ? AND plan_digest = ?
        AND revision < ? ORDER BY revision DESC LIMIT 1""",
        (grant["task_id"], grant["plan_digest"], grant["task_revision"]),
    ).fetchone()
    try:
        declared = json.loads(revision["execution_spec_json"])["output_paths"]
        return manifest_is_partial(json.loads(artifacts_json), declared)
    except (TypeError, KeyError, ValueError):
        return True


def _metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["late"] = bool(value["late"])
    value["artifacts"] = json.loads(value.pop("artifacts_json"))
    return value


class ArtifactManifestStore(ArtifactStore):
    """Worker manifest admission and tenant-scoped metadata listing."""

    def __init__(self, state_dir, backend: ProjectArtifactBackend | None = None) -> None:
        super().__init__(state_dir)
        self.project_artifact_backend = backend or FilesystemProjectArtifactBackend(
            self.database_path.parent / "project-artifacts"
        )

    def _verify_placement(
        self,
        connection: sqlite3.Connection,
        grant: sqlite3.Row,
        artifact_id: str,
        sha256: str,
        size: int,
        *,
        inspect_object: bool = False,
    ) -> None:
        row = connection.execute(
            """SELECT tenant_namespace,project_namespace,object_key,sha256,size
            FROM project_artifact_placements
            WHERE artifact_id=? AND project_id=? AND backend_kind=?""",
            (artifact_id, grant["project_id"], self.project_artifact_backend.kind),
        ).fetchone()
        if row is None or row["sha256"] != sha256 or row["size"] != size:
            raise Conflict("Artifact manifest requires committed project artifact placement.")
        if inspect_object:
            try:
                verify_project_artifact_placement(
                    self.project_artifact_backend,
                    connection.execute(
                        "SELECT org_id FROM projects WHERE id=?", (grant["project_id"],)
                    ).fetchone()["org_id"],
                    grant["project_id"],
                    artifact_id,
                    ProjectArtifactPlacement(**dict(row)),
                )
            except ProjectArtifactStorageError as error:
                raise Conflict(
                    "Artifact manifest project storage integrity check failed."
                ) from error

    def _inspect_requested_placements(
        self, worker: dict[str, Any], lease_id: str, body: dict[str, Any]
    ) -> None:
        """Inspect project objects without holding a SQLite authority transaction."""
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, worker, audience="worker")
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            self._validate_operation(connection, grant, lease, body)
            if (
                connection.execute(
                    "SELECT 1 FROM project_storage_bindings WHERE project_id=?",
                    (grant["project_id"],),
                ).fetchone()
                is None
            ):
                return
            for entry in body["artifacts"]:
                self._verify_placement(
                    connection,
                    grant,
                    entry["id"],
                    entry["sha256"],
                    entry["size"],
                    inspect_object=True,
                )
        finally:
            connection.close()

    @staticmethod
    def _manifest_id(lease_id: str) -> str:
        return "artifact_manifest_" + hashlib.sha256(lease_id.encode()).hexdigest()[:32]

    @staticmethod
    def _canonical_payload(
        lease: sqlite3.Row,
        grant: sqlite3.Row,
        actor: dict[str, Any],
        entries: list[dict[str, Any]],
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "project_id": grant["project_id"],
            "task_id": grant["task_id"],
            "grant_id": grant["id"],
            "lease_id": lease["id"],
            "worker_principal_id": actor["id"],
            "epoch": lease["epoch"],
            "operation_id": operation_id or ArtifactStore._expected_operation(lease["id"]),
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
        self._inspect_requested_placements(worker, lease_id, body)
        path = f"/worker/leases/{lease_id}/artifact-manifest"

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            self._validate_operation(connection, grant, lease, body)
            plan, _ = self._plan(connection, grant)
            spec = plan["execution_spec"]
            paths = [entry["relative_path"] for entry in body["artifacts"]]
            declared = set(spec["output_paths"])
            if len(paths) != len(set(paths)) or (
                not (set(paths) <= declared and paths)
                if body["partial"]
                else set(paths) != declared
            ):
                raise Conflict("Artifact manifest does not match declared output paths.")
            entries = sorted(body["artifacts"], key=lambda entry: entry["relative_path"])
            total = sum(entry["size"] for entry in entries)
            limit = min(spec["max_output_bytes"], operator_limits().max_artifact_bytes)
            if total > limit:
                raise Conflict("Artifact manifest output budget is exceeded.")

            verified: list[dict[str, Any]] = []
            large_ids: set[str] = set()
            any_late = False
            remaining = limit
            for entry in entries:
                row = connection.execute(
                    """SELECT id, relative_path, operation_id, epoch, sha256, size,
                    length(content) AS content_length,
                    typeof(content) AS content_type, late FROM artifacts
                    WHERE id=? AND lease_id=? AND worker_principal_id=? AND relative_path=?""",
                    (entry["id"], lease_id, actor["id"], entry["relative_path"]),
                ).fetchone()
                if row is None:
                    raise NotFound("Quarantined artifact was not found.")
                # A large artifact keeps an empty ``content``; its bytes are chunk rows that
                # complete verified end to end. Like chunked snapshots (ADR 0037) this path
                # checks chunk metadata, not gigabytes of content.
                large = row["content_length"] == 0 and row["size"] > 0
                if (
                    row["operation_id"] != body["operation_id"]
                    or row["epoch"] != lease["epoch"]
                    or row["sha256"] != entry["sha256"]
                    or row["size"] != entry["size"]
                    or row["content_type"] != "blob"
                    or not (large or row["content_length"] == row["size"])
                    or row["size"] > remaining
                ):
                    raise Conflict("Artifact manifest entry does not match quarantined artifact.")
                if large:
                    check_chunk_metadata(
                        connection,
                        row["id"],
                        [{"relative_path": row["relative_path"], "size": row["size"]}],
                    )
                    large_ids.add(row["id"])
                else:
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
                # Large artifacts live in the control chunk store, not the project object
                # store, so they carry no placement or retention record (like legacy
                # unplaced SQLite artifacts).
                for entry in (item for item in verified if item["id"] not in large_ids):
                    self._verify_placement(
                        connection, grant, entry["id"], entry["sha256"], entry["size"]
                    )
                    require_project_artifact_reference_admissible(
                        connection, grant["project_id"], entry["id"]
                    )
            if (
                body["partial"]
                and manifest_is_partial(verified, declared)
                and not _attempt_interrupted(connection, lease_id, body["operation_id"])
            ):
                # Control's own record decides, never the worker's say-so: a partial manifest
                # is admitted only after the provider moved the instance after custody began.
                raise Conflict("A partial artifact manifest needs an interrupted attempt.")
            canonical = self._canonical_payload(lease, grant, actor, verified, body["operation_id"])
            if body["partial"] and manifest_is_partial(verified, declared):
                # Part of the digest only when files are missing, so every complete manifest
                # keeps its digest.
                canonical["partial"] = True
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
                "late": int(bool(self._late(connection, grant, lease, manifest=True)) or any_late),
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
