"""Immutable, metadata-only runtime skill snapshots and human releases.

This control-plane slice stores no skill bytes and never invokes a skill setup
step, script, or runtime loader.  Filesystem validation belongs to
``workspace.snapshot_skill`` before this store receives its deterministic result.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, _parse_utc, utc_now
from .workspace import SkillSnapshot, skill_manifest_digest

MAX_SKILL_FILES = 10_000
MAX_SKILL_FILE_BYTES = 16 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 256 * 1024 * 1024
_DIGEST = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_IDENTIFIER = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_DESCRIPTION = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
_PATH = re.compile(r"^(?!.*(?:^|/)\.\.?$)[^/\\\x00]+(?:/[^/\\\x00]+)*$")


class SkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SkillManifestEntry(SkillRequest):
    relative_path: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    sha256: _DIGEST
    size: Annotated[int, Field(strict=True, ge=0, le=MAX_SKILL_FILE_BYTES)]

    @field_validator("relative_path")
    @classmethod
    def portable_relative_path(cls, value: str) -> str:
        if not _PATH.fullmatch(value) or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("skill manifest paths must be portable relative paths")
        return value


class CreateSkillSnapshot(SkillRequest):
    name: _IDENTIFIER
    description: _DESCRIPTION
    digest: _DIGEST
    manifest: Annotated[list[SkillManifestEntry], Field(min_length=1, max_length=MAX_SKILL_FILES)]
    provenance: Literal["imported_unverified"] = "imported_unverified"
    classification: Literal["unclassified"] = "unclassified"

    @field_validator("manifest")
    @classmethod
    def complete_unique_manifest(
        cls, entries: list[SkillManifestEntry]
    ) -> list[SkillManifestEntry]:
        paths = [entry.relative_path for entry in entries]
        if len(paths) != len(set(paths)) or "SKILL.md" not in paths:
            raise ValueError("skill manifest needs unique paths including SKILL.md")
        if sum(entry.size for entry in entries) > MAX_SKILL_TOTAL_BYTES:
            raise ValueError("skill manifest exceeds total byte limit")
        return entries


class ReleaseSkill(SkillRequest):
    expected_digest: _DIGEST
    worker_principal_id: _IDENTIFIER
    permits_worker_use: Literal[True]
    rationale: _DESCRIPTION
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_type(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("expiry requires an ISO timestamp") from error
        if not isinstance(value, datetime):
            raise ValueError("expiry requires an ISO timestamp")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expiry requires a timezone")
        return value.astimezone(UTC)


class RevokeSkillRelease(SkillRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


def create_skill_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS skill_snapshots (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, description TEXT NOT NULL, digest TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK(provenance='imported_unverified'),
        classification TEXT NOT NULL CHECK(classification='unclassified'),
        created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(project_id, name, digest))"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS skill_snapshots_by_project
        ON skill_snapshots(project_id, created_at, id)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS skill_releases (
        id TEXT PRIMARY KEY, skill_snapshot_id TEXT NOT NULL REFERENCES skill_snapshots(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id), digest TEXT NOT NULL,
        rationale TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL CHECK(revision >= 1), revoked_at TEXT)"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS active_skill_release
        ON skill_releases(skill_snapshot_id, worker_principal_id) WHERE status='active'"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_grant_skills (
        grant_id TEXT NOT NULL REFERENCES execution_grants(id), digest TEXT NOT NULL,
        skill_release_id TEXT NOT NULL REFERENCES skill_releases(id),
        expires_at TEXT NOT NULL, PRIMARY KEY(grant_id, digest))"""
    )


def _skill(connection: sqlite3.Connection, actor: dict[str, Any], skill_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT s.* FROM skill_snapshots s JOIN projects p ON p.id=s.project_id
        WHERE s.id=? AND p.org_id=?""",
        (skill_id, actor["org_id"]),
    ).fetchone()
    if row is None:
        raise NotFound("Skill snapshot was not found.")
    Store._project(connection, actor, row["project_id"])
    return row


def _worker(
    connection: sqlite3.Connection,
    actor: dict[str, Any],
    worker_id: str,
    project_id: str,
) -> dict[str, Any]:
    worker = Store._live_principal(
        connection, {"id": worker_id, "org_id": actor["org_id"]}, audience="worker"
    )
    if (
        worker["kind"] != "worker"
        or worker["role"] != "agent"
        or worker["project_id"] != project_id
    ):
        raise Forbidden("Worker is not authorized for this project.")
    return worker


def authorize_skill_release(
    connection: sqlite3.Connection,
    actor: dict[str, Any],
    project_id: str,
    digest: str,
    worker_id: str,
    frozen_release_id: str | None = None,
) -> dict[str, Any]:
    """Check a live human release while an execution authority transaction is open."""
    worker = _worker(connection, actor, worker_id, project_id)
    query = """SELECT r.*, s.project_id FROM skill_releases r
        JOIN skill_snapshots s ON s.id=r.skill_snapshot_id
        WHERE s.project_id=? AND r.worker_principal_id=? AND r.digest=? AND r.status='active'"""
    values: tuple[object, ...] = (project_id, worker["id"], digest)
    if frozen_release_id is not None:
        query += " AND r.id=?"
        values += (frozen_release_id,)
    query += " ORDER BY r.created_at, r.id LIMIT 1"
    release = connection.execute(query, values).fetchone()
    if release is None:
        raise Forbidden("Skill has no live release for this worker.")
    try:
        expiry = _parse_utc(release["expires_at"])
    except ValueError as error:
        raise Conflict("Skill release expiry is invalid.") from error
    if expiry <= datetime.now(UTC):
        raise Forbidden("Skill release has expired for this worker.")
    issuer = Store._live_principal(
        connection,
        {"id": release["actor_principal_id"], "org_id": actor["org_id"]},
        audience="control",
    )
    if issuer["role"] != "human":
        raise Forbidden("Skill release issuer is no longer eligible.")
    Store._project(connection, issuer, project_id)
    return dict(release)


class SkillStore(Store):
    @staticmethod
    def _body(model: type[SkillRequest], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(payload).model_dump()
        except ValidationError as error:
            raise StoreError("Invalid skill request.") from error

    @staticmethod
    def _snapshot_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            manifest = json.loads(value.pop("manifest_json"))
            body = CreateSkillSnapshot.model_validate(
                {
                    "name": value["name"],
                    "description": value["description"],
                    "digest": value["digest"],
                    "manifest": manifest,
                    "provenance": value["provenance"],
                    "classification": value["classification"],
                }
            )
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            raise Conflict("Skill snapshot metadata integrity check failed.") from None
        tuples = tuple((item.relative_path, item.sha256, item.size) for item in body.manifest)
        if skill_manifest_digest(tuples) != value["digest"]:
            raise Conflict("Skill snapshot digest integrity check failed.")
        value["manifest"] = [item.model_dump() for item in body.manifest]
        return value

    def create_skill_snapshot(
        self, actor: dict[str, Any], project_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(CreateSkillSnapshot, payload)
        entries = tuple(
            (item["relative_path"], item["sha256"], item["size"]) for item in body["manifest"]
        )
        if skill_manifest_digest(entries) != body["digest"]:
            raise Conflict("Skill digest does not match its deterministic manifest.")

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            if live["role"] != "human":
                raise Forbidden("A human must persist a skill snapshot.")
            self._project(connection, live, project_id)
            ident, now = "skill_" + uuid.uuid4().hex, utc_now()
            row = {
                "id": ident,
                "project_id": project_id,
                "name": body["name"],
                "description": body["description"],
                "digest": body["digest"],
                "manifest_json": json.dumps(
                    body["manifest"], sort_keys=True, separators=(",", ":")
                ),
                "provenance": body["provenance"],
                "classification": body["classification"],
                "created_at": now,
                "actor_principal_id": live["id"],
            }
            connection.execute(
                """INSERT INTO skill_snapshots
                (id, project_id, name, description, digest, manifest_json, provenance,
                 classification, created_at, actor_principal_id)
                VALUES (:id, :project_id, :name, :description, :digest, :manifest_json,
                 :provenance, :classification, :created_at, :actor_principal_id)""",
                row,
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "skill.snapshot_persisted",
                1,
                {
                    "skill_snapshot_id": ident,
                    "name": body["name"],
                    "digest": body["digest"],
                    "file_count": len(entries),
                    "metadata_only": True,
                },
            )
            return 201, self._snapshot_response(row)

        return self._mutate(actor, key, "POST", f"/projects/{project_id}/skills", digest, command)

    def snapshot_skill(
        self, actor: dict[str, Any], project_id: str, key: str, snapshot: SkillSnapshot
    ) -> tuple[int, dict[str, Any]]:
        """Persist the result of the bounded, non-executing filesystem validator."""
        payload = {
            "name": snapshot.name,
            "description": snapshot.description,
            "digest": snapshot.digest,
            "manifest": [
                {"relative_path": path, "sha256": digest, "size": size}
                for path, digest, size in snapshot.manifest
            ],
            "provenance": snapshot.provenance,
            "classification": snapshot.classification,
        }
        from .store import canonical_digest

        return self.create_skill_snapshot(
            actor, project_id, key, canonical_digest(payload), payload
        )

    def get_skill_snapshot(self, actor: dict[str, Any], skill_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            return self._snapshot_response(_skill(connection, live, skill_id))
        finally:
            connection.close()

    def list_skill_snapshots(
        self, actor: dict[str, Any], project_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            return [
                self._snapshot_response(row)
                for row in connection.execute(
                    """SELECT * FROM skill_snapshots WHERE project_id=?
                    ORDER BY created_at,id LIMIT ? OFFSET ?""",
                    (project_id, limit, offset),
                )
            ]
        finally:
            connection.close()

    def release_skill(
        self,
        actor: dict[str, Any],
        skill_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(ReleaseSkill, payload)

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            if live["role"] != "human":
                raise Forbidden("A human must review and release a skill.")
            skill = _skill(connection, live, skill_id)
            if skill["digest"] != body["expected_digest"]:
                raise Conflict("Skill digest changed from the reviewed basis.")
            worker = _worker(connection, live, body["worker_principal_id"], skill["project_id"])
            now, expiry = datetime.now(UTC), body["expires_at"]
            if not now < expiry <= now + timedelta(hours=24):
                raise StoreError("Skill release expiry must be within 24 hours.")
            for identity in (live, worker):
                if identity["expires_at"] and expiry > _parse_utc(identity["expires_at"]):
                    raise StoreError("Skill release cannot outlive its identities.")
            if connection.execute(
                """SELECT 1 FROM skill_releases WHERE skill_snapshot_id=?
                AND worker_principal_id=? AND status='active'""",
                (skill_id, worker["id"]),
            ).fetchone():
                raise Conflict("Revoke the previous skill release before replacing it.")
            result = {
                "id": "skill_release_" + uuid.uuid4().hex,
                "skill_snapshot_id": skill_id,
                "worker_principal_id": worker["id"],
                "actor_principal_id": live["id"],
                "digest": skill["digest"],
                "rationale": body["rationale"],
                "expires_at": expiry.isoformat(),
                "created_at": utc_now(),
                "status": "active",
                "revision": 1,
                "revoked_at": None,
            }
            connection.execute(
                """INSERT INTO skill_releases VALUES
                (:id,:skill_snapshot_id,:worker_principal_id,:actor_principal_id,:digest,:rationale,
                 :expires_at,:created_at,:status,:revision,:revoked_at)""",
                result,
            )
            self._event(
                connection,
                live,
                skill["project_id"],
                None,
                "skill.released",
                1,
                {
                    "skill_snapshot_id": skill_id,
                    "skill_release_id": result["id"],
                    "worker_principal_id": worker["id"],
                    "digest": skill["digest"],
                    "expires_at": result["expires_at"],
                },
            )
            return 201, result

        return self._mutate(actor, key, "POST", f"/skills/{skill_id}/releases", digest, command)

    def revoke_skill_release(
        self,
        actor: dict[str, Any],
        release_id: str,
        key: str,
        digest: str,
        expected_revision: int,
    ) -> tuple[int, dict[str, Any]]:
        expected_revision = self._body(
            RevokeSkillRelease, {"expected_revision": expected_revision}
        )["expected_revision"]

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            if live["role"] != "human":
                raise Forbidden("A human must revoke a skill release.")
            release = connection.execute(
                """SELECT r.*,s.project_id FROM skill_releases r
                JOIN skill_snapshots s ON s.id=r.skill_snapshot_id
                JOIN projects p ON p.id=s.project_id WHERE r.id=? AND p.org_id=?""",
                (release_id, live["org_id"]),
            ).fetchone()
            if release is None:
                raise NotFound("Skill release was not found.")
            self._project(connection, live, release["project_id"])
            if release["revision"] != expected_revision or release["status"] != "active":
                raise Conflict("Skill release revision is stale.")
            now = utc_now()
            connection.execute(
                """UPDATE skill_releases SET status='revoked', revision=revision+1,
                revoked_at=? WHERE id=?""",
                (now, release_id),
            )
            result = {
                **dict(release),
                "status": "revoked",
                "revision": expected_revision + 1,
                "revoked_at": now,
            }
            self._event(
                connection,
                live,
                release["project_id"],
                None,
                "skill.release_revoked",
                result["revision"],
                {
                    "skill_release_id": release_id,
                    "skill_snapshot_id": release["skill_snapshot_id"],
                },
            )
            return 200, result

        return self._mutate(
            actor, key, "POST", f"/skill-releases/{release_id}/revoke", digest, command
        )

    def list_skill_releases(
        self, actor: dict[str, Any], skill_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            _skill(connection, live, skill_id)
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM skill_releases WHERE skill_snapshot_id=?
                    ORDER BY created_at,id LIMIT ? OFFSET ?""",
                    (skill_id, limit, offset),
                )
            ]
        finally:
            connection.close()
