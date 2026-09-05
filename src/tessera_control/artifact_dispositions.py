"""Append-only human dispositions for quarantined artifact manifests."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .artifact_manifests import ArtifactManifestStore
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, utc_now

ManifestDigest = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
]


def create_artifact_disposition_schema(connection: sqlite3.Connection) -> None:
    """Install immutable, append-only human disposition history."""
    connection.execute(
        """CREATE TABLE artifact_dispositions (
        id TEXT PRIMARY KEY,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
        manifest_digest TEXT NOT NULL CHECK (
            length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
        ),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        disposition TEXT NOT NULL CHECK (disposition IN ('retain_quarantined', 'reject')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        UNIQUE(manifest_id, revision)
        )"""
    )
    connection.execute(
        "CREATE INDEX artifact_dispositions_by_manifest "
        "ON artifact_dispositions(manifest_id, revision)"
    )


class ArtifactDispositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_manifest_digest: ManifestDigest
    expected_revision: int = Field(strict=True, ge=0, le=2**63 - 1)
    disposition: str = Field(pattern=r"^(retain_quarantined|reject)$")
    rationale: str = Field(min_length=1, max_length=8000)


def _metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


class ArtifactDispositionStore(ArtifactManifestStore):
    """Record and read human review decisions without changing manifest state."""

    @staticmethod
    def _manifest_for_actor(
        connection: sqlite3.Connection, actor: dict[str, Any], manifest_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT m.id, m.project_id, m.task_id, m.manifest_digest, p.org_id
            FROM artifact_manifests m JOIN projects p ON p.id = m.project_id
            WHERE m.id = ? AND p.org_id = ?""",
            (manifest_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Artifact manifest was not found.")
        if actor["project_id"] is not None and actor["project_id"] != row["project_id"]:
            raise NotFound("Artifact manifest was not found.")
        return row

    @staticmethod
    def _response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return _metadata(row)

    def record_disposition(
        self,
        actor: dict[str, Any],
        manifest_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = ArtifactDispositionRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact disposition request.") from error
        if not body["rationale"].strip():
            raise StoreError("Disposition rationale must not be blank.")
        path = f"/artifact-manifests/{manifest_id}/dispositions"

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            if live["role"] != "human":
                raise Forbidden("Human artifact disposition is required.")
            manifest = self._manifest_for_actor(connection, live, manifest_id)
            if manifest["manifest_digest"] != body["expected_manifest_digest"]:
                raise Conflict("Artifact manifest digest does not match.")
            current = connection.execute(
                """SELECT revision FROM artifact_dispositions
                WHERE manifest_id=? ORDER BY revision DESC LIMIT 1""",
                (manifest_id,),
            ).fetchone()
            current_revision = current["revision"] if current is not None else 0
            if body["expected_revision"] != current_revision:
                raise Conflict("Artifact disposition revision is no longer current.")
            revision = current_revision + 1
            row = {
                "id": f"artifact_disposition_{manifest_id}_{revision}",
                "manifest_id": manifest_id,
                "manifest_digest": body["expected_manifest_digest"],
                "revision": revision,
                "disposition": body["disposition"],
                "rationale": body["rationale"],
                "actor_principal_id": live["id"],
                "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO artifact_dispositions
                (id, manifest_id, manifest_digest, revision, disposition, rationale,
                 actor_principal_id, created_at)
                VALUES (:id,:manifest_id,:manifest_digest,:revision,:disposition,:rationale,
                        :actor_principal_id,:created_at)""",
                row,
            )
            self._event(
                connection,
                live,
                manifest["project_id"],
                manifest["task_id"],
                "human.artifact_disposition_recorded",
                revision,
                {
                    "manifest_id": manifest_id,
                    "manifest_digest": row["manifest_digest"],
                    "disposition": row["disposition"],
                },
            )
            return 201, self._response(row)

        def replay_guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any) -> None:
            if live["role"] != "human":
                raise Forbidden("Human artifact disposition is required.")
            self._manifest_for_actor(connection, live, manifest_id)

        return self._mutate(actor, key, "POST", path, digest, command, replay_guard=replay_guard)

    def list_dispositions(
        self, actor: dict[str, Any], manifest_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._manifest_for_actor(connection, live, manifest_id)
            rows = connection.execute(
                """SELECT id, manifest_id, manifest_digest, revision, disposition,
                rationale, actor_principal_id, created_at
                FROM artifact_dispositions WHERE manifest_id=?
                ORDER BY revision LIMIT ? OFFSET ?""",
                (manifest_id, limit, offset),
            ).fetchall()
            return [_metadata(row) for row in rows]
        finally:
            connection.close()
