"""Explicit, manual project-object retention and conservative deletion recovery."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .project_artifacts import (
    ProjectArtifactBackend,
    ProjectArtifactPlacement,
    ProjectArtifactStorageError,
)
from .store import Conflict, NotFound, Store, _connect, utc_now


def create_project_artifact_retention_schema(connection: sqlite3.Connection) -> None:
    """Install recovery metadata; it neither schedules nor performs deletion."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS project_artifact_retention (
        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held', 'short_lived')),
        state TEXT NOT NULL CHECK (state IN ('active', 'tombstoned', 'delete_intent', 'degraded', 'deleted')),
        created_at TEXT NOT NULL, activity_at TEXT NOT NULL, tombstoned_at TEXT,
        delete_intent_at TEXT, deleted_at TEXT, incident_at TEXT, incident_reason TEXT
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS project_artifact_retention_by_project "
        "ON project_artifact_retention(project_id, state, activity_at, artifact_id)"
    )


def require_project_artifact_reference_admissible(
    connection: sqlite3.Connection, project_id: str, artifact_id: str
) -> None:
    """Reject new custody references once an object leaves active retention."""
    row = connection.execute(
        "SELECT project_id,state FROM project_artifact_retention WHERE artifact_id=?",
        (artifact_id,),
    ).fetchone()
    if row is None or row["project_id"] != project_id:
        raise Conflict("Project artifact retention metadata is unavailable.")
    if row["state"] != "active":
        raise Conflict("Project artifact is reserved for deletion and cannot be referenced.")


def admit_experiment_artifact_evidence_reference(
    connection: sqlite3.Connection,
    project_id: str,
    artifact_id: str,
    attempt_id: str,
    reference_id: str,
    reference_kind: str,
    created_at: str,
    measurement_id: str | None = None,
) -> None:
    """Atomically admit durable experiment evidence without inventing legacy custody.

    Pre-v22 SQLite-only artifacts deliberately have neither a placement nor retention
    metadata. They remain linkable as ``legacy_unplaced`` evidence. A placed object,
    however, must have active retention metadata before it can acquire a new durable
    experiment reference.
    """
    artifact = connection.execute(
        "SELECT project_id FROM artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    if artifact is None or artifact["project_id"] != project_id:
        raise Conflict("Experiment evidence artifact is not in this project.")
    retention = connection.execute(
        "SELECT project_id,state FROM project_artifact_retention WHERE artifact_id=?",
        (artifact_id,),
    ).fetchone()
    if retention is None:
        placement = connection.execute(
            "SELECT 1 FROM project_artifact_placements WHERE artifact_id=? AND project_id=?",
            (artifact_id, project_id),
        ).fetchone()
        if placement is not None:
            raise Conflict("Project artifact retention metadata is unavailable.")
    elif retention["project_id"] != project_id or retention["state"] != "active":
        raise Conflict("Project artifact is reserved for deletion and cannot be referenced.")
    connection.execute(
        """INSERT OR IGNORE INTO experiment_artifact_evidence_references
        (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (
            reference_id,
            artifact_id,
            project_id,
            attempt_id,
            measurement_id,
            reference_kind,
            created_at,
        ),
    )


class ProjectArtifactRetentionStore(Store):
    """Manual tombstone/delete commands; uncertainty becomes durable degraded state."""

    def __init__(self, state_dir, backend: ProjectArtifactBackend) -> None:
        super().__init__(state_dir)
        self.backend = backend

    def _object(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        project_id: str,
        artifact_id: str,
    ):
        self._project(connection, actor, project_id)
        row = connection.execute(
            """SELECT p.*, r.retention_class AS retention_metadata_class, r.state, r.activity_at
            FROM project_artifact_placements p
            JOIN project_artifact_retention r ON r.artifact_id=p.artifact_id
            WHERE p.artifact_id=? AND p.project_id=? AND p.backend_kind=?""",
            (artifact_id, project_id, self.backend.kind),
        ).fetchone()
        if row is None:
            raise NotFound("Project artifact retention object was not found.")
        return row

    @staticmethod
    def _referenced(connection: sqlite3.Connection, project_id: str, artifact_id: str) -> bool:
        # IDs are generated without quotes and manifests are canonical JSON. Parse
        # bounded rows instead of relying on optional SQLite JSON extensions.
        for row in connection.execute("SELECT artifacts_json FROM artifact_manifests"):
            try:
                manifest = json.loads(row[0])
                if not isinstance(manifest, list) or any(
                    not isinstance(item, dict) for item in manifest
                ):
                    return True
                if any(item.get("id") == artifact_id for item in manifest):
                    return True
            except (TypeError, ValueError):
                return True  # malformed authoritative metadata fails closed
        if (
            connection.execute(
                """SELECT 1 FROM experiment_artifact_evidence_references
                WHERE artifact_id=? AND project_id=? LIMIT 1""",
                (artifact_id, project_id),
            ).fetchone()
            is not None
        ):
            return True
        return False

    def _delete_allowed(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["retention_metadata_class"] == "held":
            raise Conflict("Held project artifacts cannot be deleted.")
        if self._referenced(connection, row["project_id"], row["artifact_id"]):
            raise Conflict("Referenced project artifacts cannot be deleted.")
        checkpoint = connection.execute(
            """SELECT 1 FROM execution_result_checkpoints c JOIN artifact_manifests m ON m.id=c.manifest_id
            WHERE m.project_id=? AND instr(m.artifacts_json, ?) > 0 LIMIT 1""",
            (row["project_id"], row["artifact_id"]),
        ).fetchone()
        if checkpoint is not None:
            raise Conflict("Checkpoint project artifacts cannot be deleted.")

    def _delete_ready(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["state"] != "delete_intent":
            raise Conflict("Project artifact deletion is no longer reserved.")
        self._delete_allowed(connection, row)

    def _delete_replay_ready(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        """Allow a completed same-key delete replay, but no earlier terminal state."""
        if row["state"] != "deleted":
            self._delete_ready(connection, row)

    def tombstone(
        self, actor: dict[str, Any], project_id: str, artifact_id: str, key: str, digest: str
    ):
        path = f"/projects/{project_id}/artifacts/{artifact_id}/retention/tombstone"

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            row = self._object(connection, live, project_id, artifact_id)
            self._delete_allowed(connection, row)
            if row["state"] == "deleted":
                return 200, {"state": "deleted"}
            now = utc_now()
            connection.execute(
                """UPDATE project_artifact_retention SET state='tombstoned',
                tombstoned_at=COALESCE(tombstoned_at, ?), activity_at=? WHERE artifact_id=?""",
                (now, now, artifact_id),
            )
            return 200, {"state": "tombstoned", "tombstoned_at": now}

        return self._mutate(actor, key, "POST", path, digest, command)

    def reconcile_delete(
        self, actor: dict[str, Any], project_id: str, artifact_id: str, key: str, digest: str
    ):
        """Perform one explicitly requested delete after durable intent, never automatically."""
        path = f"/projects/{project_id}/artifacts/{artifact_id}/retention/delete"

        def intent(connection: sqlite3.Connection, live: dict[str, Any]):
            row = self._object(connection, live, project_id, artifact_id)
            self._delete_allowed(connection, row)
            if row["state"] == "deleted":
                return 200, dict(row)
            if row["state"] not in ("tombstoned", "delete_intent"):
                raise Conflict("Project artifact must be tombstoned before deletion.")
            if row["state"] == "delete_intent":
                return 202, dict(row)
            now = utc_now()
            connection.execute(
                """UPDATE project_artifact_retention SET state='delete_intent',
                delete_intent_at=COALESCE(delete_intent_at, ?), activity_at=? WHERE artifact_id=?""",
                (now, now, artifact_id),
            )
            return 202, dict(row)

        status, row = self._mutate(
            actor,
            "delete-intent:" + key,
            "POST",
            path + "/intent",
            digest,
            intent,
            replay_guard=lambda connection, live, _: self._delete_replay_ready(
                connection, self._object(connection, live, project_id, artifact_id)
            ),
        )
        if status == 200 and row["state"] == "deleted":
            return status, {"state": "deleted"}
        # The intent's cached response predates the backend call.  A same-key
        # replay must observe the terminal state rather than trying deletion
        # again because that cached response remains ``delete_intent``.
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            if self._object(connection, live, project_id, artifact_id)["state"] == "deleted":
                return 200, {"state": "deleted"}
        finally:
            connection.close()
        placement = ProjectArtifactPlacement(
            row["tenant_namespace"],
            row["project_namespace"],
            row["object_key"],
            row["sha256"],
            row["size"],
        )
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            live = self._live_principal(connection, actor)
            current = self._object(connection, live, project_id, artifact_id)
            self._delete_ready(connection, current)
            tenant_id = connection.execute(
                "SELECT org_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()[0]
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        try:
            deleted = self.backend.delete(tenant_id, project_id, artifact_id, placement)
        except ProjectArtifactStorageError as error:
            deleted = False
            failure = str(error)
        else:
            failure = "Project artifact object is missing during deletion." if not deleted else None

        def finish(connection: sqlite3.Connection, live: dict[str, Any]):
            current = self._object(connection, live, project_id, artifact_id)
            if current["state"] == "deleted":
                return 200, {"state": "deleted"}
            now = utc_now()
            if failure is None:
                connection.execute(
                    "UPDATE project_artifact_retention SET state='deleted', deleted_at=?, activity_at=? WHERE artifact_id=?",
                    (now, now, artifact_id),
                )
                return 200, {"state": "deleted", "deleted_at": now}
            connection.execute(
                """UPDATE project_artifact_retention SET state='degraded', incident_at=?,
                incident_reason=?, activity_at=? WHERE artifact_id=?""",
                (now, failure[:500], now, artifact_id),
            )
            return 202, {"state": "degraded", "incident_reason": failure}

        return self._mutate(actor, key, "POST", path, digest, finish)

    def inspect(self, actor: dict[str, Any], project_id: str, artifact_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            row = self._object(connection, live, project_id, artifact_id)
            if row["state"] == "deleted":
                return {"state": "deleted", "activity_at": row["activity_at"]}
            placement = ProjectArtifactPlacement(
                *[
                    row[name]
                    for name in (
                        "tenant_namespace",
                        "project_namespace",
                        "object_key",
                        "sha256",
                        "size",
                    )
                ]
            )
            tenant_id = connection.execute(
                "SELECT org_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        try:
            observed = self.backend.inspect(
                tenant_id, project_id, artifact_id, placement.sha256, placement.size
            )
            failure = (
                None
                if observed == placement
                else "Project artifact object is missing or conflicts."
            )
        except ProjectArtifactStorageError as error:
            failure = str(error)
        if failure is None:
            connection = _connect(self.database_path)
            try:
                live = self._live_principal(connection, actor)
                current = self._object(connection, live, project_id, artifact_id)
                return {"state": current["state"], "activity_at": current["activity_at"]}
            finally:
                connection.close()
        connection = _connect(self.database_path)
        try:
            now = utc_now()
            connection.execute("BEGIN IMMEDIATE")
            live = self._live_principal(connection, actor)
            current = self._object(connection, live, project_id, artifact_id)
            if current["state"] == "deleted":
                connection.execute("COMMIT")
                return {"state": "deleted", "activity_at": current["activity_at"]}
            connection.execute(
                "UPDATE project_artifact_retention SET state='degraded',incident_at=?,incident_reason=?,activity_at=? WHERE artifact_id=?",
                (now, failure[:500], now, artifact_id),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return {"state": "degraded", "incident_reason": failure}
