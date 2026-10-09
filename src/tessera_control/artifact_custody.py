"""Human-gated artifact read custody and immutable reproduction exports.

Quarantine remains the classification of worker output.  A custody release grants
only a scoped human read of already placed private bytes; it neither classifies,
publishes, accepts, nor changes the output.
"""
# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifact_manifests import _metadata as manifest_metadata
from .project_artifacts import (
    FilesystemProjectArtifactBackend,
    ProjectArtifactBackend,
    ProjectArtifactPlacement,
    ProjectArtifactStorageError,
)
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    canonical_digest,
    utc_now,
)

MAX_PREVIEW_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_RECORDS = 256


def create_artifact_custody_schema(connection: sqlite3.Connection) -> None:
    """Install human custody-release history and immutable bundle payloads."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS artifact_custody_releases (
        id TEXT PRIMARY KEY,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest)=64
            AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        state TEXT NOT NULL CHECK (state IN ('released','revoked')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        UNIQUE(manifest_id, revision)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS artifact_custody_releases_by_manifest "
        "ON artifact_custody_releases(manifest_id, revision)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS reproduction_bundles (
        id TEXT PRIMARY KEY,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest)=64
            AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
        custody_release_id TEXT NOT NULL REFERENCES artifact_custody_releases(id),
        bundle_digest TEXT NOT NULL UNIQUE CHECK (length(bundle_digest)=64
            AND bundle_digest NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL CHECK (length(payload_json) <= 4194304),
        size INTEGER NOT NULL CHECK (size >= 1 AND size <= 4194304),
        created_at TEXT NOT NULL,
        actor_principal_id TEXT NOT NULL REFERENCES principals(id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS reproduction_bundles_by_manifest "
        "ON reproduction_bundles(manifest_id, created_at, id)"
    )


class ArtifactCustodyReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    expected_revision: int = Field(strict=True, ge=0, le=2**63 - 1)
    action: Literal["release", "revoke"]
    rationale: str = Field(min_length=1, max_length=8000)


class ReproductionBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    max_bytes: int = Field(strict=True, ge=1024, le=MAX_BUNDLE_BYTES)


def _compact_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    )


class ArtifactCustodyStore(Store):
    """Read private placed bytes only after an explicit human custody release."""

    def __init__(self, state_dir, backend: ProjectArtifactBackend | None = None) -> None:
        super().__init__(state_dir)
        self.backend = backend or FilesystemProjectArtifactBackend(
            self.database_path.parent / "project-artifacts"
        )

    @staticmethod
    def _manifest(
        connection: sqlite3.Connection, live: dict[str, Any], manifest_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT m.* FROM artifact_manifests m JOIN projects p ON p.id=m.project_id
            WHERE m.id=? AND p.org_id=?""",
            (manifest_id, live["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Artifact manifest was not found.")
        Store._project(connection, live, row["project_id"])
        return row

    @staticmethod
    def _human(live: dict[str, Any]) -> None:
        if live["role"] != "human":
            raise Forbidden("A human researcher custody credential is required.")

    @staticmethod
    def _current_release(connection: sqlite3.Connection, manifest_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM artifact_custody_releases WHERE manifest_id=? ORDER BY revision DESC LIMIT 1",
            (manifest_id,),
        ).fetchone()

    def _released_manifest(
        self, connection: sqlite3.Connection, live: dict[str, Any], manifest_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        self._human(live)
        manifest = self._manifest(connection, live, manifest_id)
        release = self._current_release(connection, manifest_id)
        if release is None or release["state"] != "released":
            raise Forbidden("Artifact custody has not been released for researcher retrieval.")
        if release["manifest_digest"] != manifest["manifest_digest"]:
            raise Conflict("Artifact custody release no longer matches the manifest.")
        disposition = self._latest_disposition(connection, manifest_id)
        if disposition is None or disposition["disposition"] != "retain_quarantined":
            raise Forbidden("Artifact custody is blocked by the current manifest disposition.")
        return manifest, release

    @staticmethod
    def _latest_disposition(connection: sqlite3.Connection, manifest_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM artifact_dispositions WHERE manifest_id=? ORDER BY revision DESC LIMIT 1",
            (manifest_id,),
        ).fetchone()

    def record_release(
        self,
        actor: dict[str, Any],
        manifest_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = ArtifactCustodyReleaseRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact custody release request.") from error
        if not body["rationale"].strip():
            raise StoreError("Custody release rationale must not be blank.")

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._human(live)
            manifest = self._manifest(connection, live, manifest_id)
            if manifest["manifest_digest"] != body["expected_manifest_digest"]:
                raise Conflict("Artifact manifest digest does not match.")
            current = self._current_release(connection, manifest_id)
            revision = 0 if current is None else current["revision"]
            if body["expected_revision"] != revision:
                raise Conflict("Artifact custody release revision is no longer current.")
            if body["action"] == "release":
                disposition = self._latest_disposition(connection, manifest_id)
                if disposition is None or disposition["disposition"] != "retain_quarantined":
                    raise Conflict(
                        "A current retain_quarantined disposition is required for custody release."
                    )
                state = "released"
            else:
                state = "revoked"
            row = {
                "id": f"artifact_custody_release_{manifest_id}_{revision + 1}",
                "manifest_id": manifest_id,
                "manifest_digest": manifest["manifest_digest"],
                "revision": revision + 1,
                "state": state,
                "rationale": body["rationale"],
                "actor_principal_id": live["id"],
                "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO artifact_custody_releases
                (id,manifest_id,manifest_digest,revision,state,rationale,actor_principal_id,created_at)
                VALUES (:id,:manifest_id,:manifest_digest,:revision,:state,:rationale,
                        :actor_principal_id,:created_at)""",
                row,
            )
            self._event(
                connection,
                live,
                manifest["project_id"],
                manifest["task_id"],
                f"human.artifact_custody_{state}",
                row["revision"],
                {"manifest_id": manifest_id, "manifest_digest": manifest["manifest_digest"]},
            )
            return 201, row

        def replay_guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any) -> None:
            self._human(live)
            self._manifest(connection, live, manifest_id)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/artifact-manifests/{manifest_id}/custody-releases",
            digest,
            command,
            replay_guard=replay_guard,
        )

    def get_released_manifest(self, actor: dict[str, Any], manifest_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            manifest, release = self._released_manifest(connection, live, manifest_id)
            return {"manifest": manifest_metadata(manifest), "custody_release": dict(release)}
        finally:
            connection.close()

    def _artifact_for_read(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        project_id: str,
        manifest: sqlite3.Row,
        artifact_id: str,
    ) -> tuple[dict[str, Any], ProjectArtifactPlacement, str]:
        entries = json.loads(manifest["artifacts_json"])
        if not any(entry.get("id") == artifact_id for entry in entries):
            raise NotFound("Released artifact was not found in this manifest.")
        row = connection.execute(
            """SELECT a.id,a.project_id,a.relative_path,a.sha256,a.size,a.status,a.classification,
            r.state AS retention_state,p.tenant_namespace,p.project_namespace,p.object_key,
            p.sha256 AS placement_sha256,p.size AS placement_size,project.org_id
            FROM artifacts a JOIN projects project ON project.id=a.project_id
            LEFT JOIN project_artifact_retention r ON r.artifact_id=a.id AND r.project_id=a.project_id
            LEFT JOIN project_artifact_placements p ON p.artifact_id=a.id AND p.project_id=a.project_id
            WHERE a.id=? AND a.project_id=? AND project.org_id=? AND p.backend_kind=?""",
            (artifact_id, project_id, live["org_id"], self.backend.kind),
        ).fetchone()
        if (
            row is None
            or row["status"] != "quarantined"
            or row["classification"] != "unclassified"
            or row["retention_state"] != "active"
            or row["sha256"] != row["placement_sha256"]
            or row["size"] != row["placement_size"]
        ):
            raise Conflict("Released artifact has no active, matching project custody.")
        placement = ProjectArtifactPlacement(
            row["tenant_namespace"],
            row["project_namespace"],
            row["object_key"],
            row["placement_sha256"],
            row["placement_size"],
        )
        return dict(row), placement, row["org_id"]

    def preview(
        self, actor: dict[str, Any], manifest_id: str, artifact_id: str, max_bytes: int
    ) -> dict[str, Any]:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PREVIEW_BYTES:
            raise StoreError("Invalid artifact preview byte limit.")
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, actor)
            manifest, _release = self._released_manifest(connection, live, manifest_id)
            artifact, content = self._read_artifact(connection, live, manifest, artifact_id)
            return {
                "artifact_id": artifact_id,
                "relative_path": artifact["relative_path"],
                "sha256": artifact["sha256"],
                "size": artifact["size"],
                "preview_base64": base64.b64encode(content[:max_bytes]).decode("ascii"),
                "preview_bytes": min(len(content), max_bytes),
                "truncated": len(content) > max_bytes,
                "status": "quarantined",
                "classification": "unclassified",
            }
        finally:
            connection.close()

    def _read_artifact(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        manifest: sqlite3.Row,
        artifact_id: str,
    ) -> tuple[dict[str, Any], bytes]:
        artifact, placement, tenant_id = self._artifact_for_read(
            connection, live, manifest["project_id"], manifest, artifact_id
        )
        try:
            content = self.backend.read(tenant_id, manifest["project_id"], artifact_id, placement)
        except ProjectArtifactStorageError as error:
            raise Conflict("Released artifact project custody integrity check failed.") from error
        return artifact, content

    def retrieve(self, actor: dict[str, Any], manifest_id: str, artifact_id: str) -> dict[str, Any]:
        """Return exact released bytes after private placement integrity verification."""
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, actor)
            manifest, _release = self._released_manifest(connection, live, manifest_id)
            artifact, content = self._read_artifact(connection, live, manifest, artifact_id)
            return {
                "content": content,
                "sha256": artifact["sha256"],
                "size": artifact["size"],
                "relative_path": artifact["relative_path"],
            }
        finally:
            connection.close()

    @staticmethod
    def _attempt_material(
        connection: sqlite3.Connection, operation_id: str, grant_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT a.id,a.experiment_id,a.state,a.outcome,a.reason,a.revision,a.created_at,a.updated_at,
            a.operational_state,a.operational_observed_state,a.operational_termination_confirmed,
            r.outcome AS human_outcome,r.reason AS human_reason,r.decided_at,r.decided_by_principal_id
            FROM research_experiment_admitted_attempts a
            LEFT JOIN research_experiment_admitted_attempt_resolutions r ON r.attempt_id=a.id
            WHERE a.operation_id=? OR a.grant_id=? ORDER BY a.created_at,a.id""",
            (operation_id, grant_id),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _measurements(
        connection: sqlite3.Connection, attempt_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not attempt_ids:
            return []
        placeholders = ",".join("?" for _ in attempt_ids)
        rows = connection.execute(
            "SELECT * FROM research_admitted_attempt_measurements WHERE attempt_id IN ("
            + placeholders
            + ") ORDER BY created_at,id",
            attempt_ids,
        ).fetchall()
        return [dict(row) for row in rows]

    def export_bundle(
        self,
        actor: dict[str, Any],
        manifest_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = ReproductionBundleRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid reproduction bundle request.") from error

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._human(live)
            manifest, release = self._released_manifest(connection, live, manifest_id)
            disposition_count = connection.execute(
                "SELECT count(*) FROM artifact_dispositions WHERE manifest_id=?", (manifest_id,)
            ).fetchone()[0]
            if disposition_count > MAX_BUNDLE_RECORDS:
                raise Conflict("Reproduction bundle exceeds the disposition record limit.")
            attempts = self._attempt_material(
                connection, manifest["operation_id"], manifest["grant_id"]
            )
            if len(attempts) > MAX_BUNDLE_RECORDS:
                raise Conflict("Reproduction bundle exceeds the attempt record limit.")
            measurements = self._measurements(connection, [item["id"] for item in attempts])
            if len(measurements) > MAX_BUNDLE_RECORDS:
                raise Conflict("Reproduction bundle exceeds the measurement record limit.")
            revision = connection.execute(
                """SELECT title,purpose,budget_cents,max_duration_seconds,execution_spec_json,revision
                FROM task_revisions WHERE task_id=? AND plan_digest=? ORDER BY revision DESC LIMIT 1""",
                (manifest["task_id"], manifest["plan_digest"]),
            ).fetchone()
            if revision is None:
                raise Conflict("Reproduction bundle plan material is missing.")
            try:
                execution_spec = json.loads(revision["execution_spec_json"])
            except (TypeError, ValueError) as error:
                raise Conflict("Reproduction bundle plan material is invalid.") from error
            artifacts: list[dict[str, Any]] = []
            for entry in json.loads(manifest["artifacts_json"]):
                artifact, placement, tenant_id = self._artifact_for_read(
                    connection, live, manifest["project_id"], manifest, entry["id"]
                )
                try:
                    content = self.backend.read(
                        tenant_id, manifest["project_id"], entry["id"], placement
                    )
                except ProjectArtifactStorageError as error:
                    raise Conflict(
                        "Reproduction bundle artifact custody integrity check failed."
                    ) from error
                artifacts.append(
                    {
                        "id": entry["id"],
                        "relative_path": artifact["relative_path"],
                        "sha256": artifact["sha256"],
                        "size": artifact["size"],
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "custody": "released_private_project_object",
                    }
                )
            snapshots = connection.execute(
                """SELECT id,digest,manifest_json,created_at FROM source_snapshots
                WHERE project_id=? AND digest=? ORDER BY created_at,id LIMIT 2""",
                (manifest["project_id"], execution_spec.get("input_snapshot_digest")),
            ).fetchall()
            input_snapshot = dict(snapshots[0]) if len(snapshots) == 1 else None
            if input_snapshot is not None:
                input_snapshot["manifest"] = json.loads(input_snapshot.pop("manifest_json"))
            dispositions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM artifact_dispositions WHERE manifest_id=? ORDER BY revision",
                    (manifest_id,),
                )
            ]
            bundle = {
                "version": 1,
                "manifest": manifest_metadata(manifest),
                "custody_release": dict(release),
                "outputs": artifacts,
                "execution": {
                    "task_revision": dict(revision) | {"execution_spec": execution_spec},
                    "plan_digest": manifest["plan_digest"],
                    "operation_id": manifest["operation_id"],
                },
                "attempts": attempts,
                "measurements": measurements,
                "decisions": {"manifest_dispositions": dispositions},
                "input_snapshot": input_snapshot,
                "missing_materials": [
                    {
                        "kind": "input_bytes",
                        "reason": "not_exported: input snapshots remain separately released and unclassified",
                    }
                    if input_snapshot is not None
                    else {
                        "kind": "input_snapshot",
                        "reason": "missing: no unique retained snapshot matches the frozen input digest",
                    },
                    {
                        "kind": "skill_material",
                        "reason": "restricted: this bundle records only pinned skill digests",
                    },
                    {
                        "kind": "environment_image",
                        "reason": "missing: execution profile records references, not an environment image export",
                    },
                ],
                "completeness": "bounded_private_reproduction_material",
            }
            size = _compact_size(bundle)
            if size > body["max_bytes"]:
                raise Conflict("Reproduction bundle exceeds the requested byte cap.")
            bundle_digest = canonical_digest(bundle)
            existing = connection.execute(
                "SELECT * FROM reproduction_bundles WHERE bundle_digest=?", (bundle_digest,)
            ).fetchone()
            if existing is not None:
                return 200, {"id": existing["id"], "bundle_digest": bundle_digest, "bundle": bundle}
            row = {
                "id": "reproduction_bundle_"
                + hashlib.sha256(bundle_digest.encode()).hexdigest()[:32],
                "manifest_id": manifest_id,
                "manifest_digest": manifest["manifest_digest"],
                "custody_release_id": release["id"],
                "bundle_digest": bundle_digest,
                "payload_json": json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
                "size": size,
                "created_at": utc_now(),
                "actor_principal_id": live["id"],
            }
            connection.execute(
                """INSERT INTO reproduction_bundles
                (id,manifest_id,manifest_digest,custody_release_id,bundle_digest,payload_json,size,
                 created_at,actor_principal_id)
                VALUES (:id,:manifest_id,:manifest_digest,:custody_release_id,:bundle_digest,
                        :payload_json,:size,:created_at,:actor_principal_id)""",
                row,
            )
            self._event(
                connection,
                live,
                manifest["project_id"],
                manifest["task_id"],
                "human.reproduction_bundle_exported",
                1,
                {"manifest_id": manifest_id, "bundle_digest": bundle_digest, "size": size},
            )
            return 201, {"id": row["id"], "bundle_digest": bundle_digest, "bundle": bundle}

        def replay_guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any) -> None:
            """Do not let a cached export outlive its current byte-read authority."""
            manifest, _release = self._released_manifest(connection, live, manifest_id)
            for entry in json.loads(manifest["artifacts_json"]):
                # Recheck the live placement/retention metadata.  The immutable
                # stored bundle remains auditable, but replay is still a new
                # request to expose its private bytes.
                self._artifact_for_read(
                    connection, live, manifest["project_id"], manifest, entry["id"]
                )

        return self._mutate(
            actor,
            key,
            "POST",
            f"/artifact-manifests/{manifest_id}/reproduction-bundles",
            digest,
            command,
            replay_guard=replay_guard,
        )

    def get_bundle(self, actor: dict[str, Any], bundle_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._human(live)
            row = connection.execute(
                """SELECT b.* FROM reproduction_bundles b JOIN artifact_manifests m ON m.id=b.manifest_id
                JOIN projects p ON p.id=m.project_id WHERE b.id=? AND p.org_id=?""",
                (bundle_id, live["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Reproduction bundle was not found.")
            self._project(
                connection,
                live,
                connection.execute(
                    "SELECT project_id FROM artifact_manifests WHERE id=?", (row["manifest_id"],)
                ).fetchone()[0],
            )
            self._released_manifest(connection, live, row["manifest_id"])
            try:
                bundle = json.loads(row["payload_json"])
            except (TypeError, ValueError) as error:
                raise Conflict("Stored reproduction bundle is invalid.") from error
            if (
                not isinstance(bundle, dict)
                or _compact_size(bundle) != row["size"]
                or canonical_digest(bundle) != row["bundle_digest"]
            ):
                raise Conflict("Stored reproduction bundle integrity check failed.")
            return {
                "id": row["id"],
                "bundle_digest": row["bundle_digest"],
                "bundle": bundle,
            }
        finally:
            connection.close()
