"""Explicit SQLite persistence for the local control plane."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .migrations import LATEST_SCHEMA_VERSION, migrate_database, plan_digest

SCHEMA_VERSION = LATEST_SCHEMA_VERSION
DATABASE_NAME = "tessera.sqlite3"
OWNER_HEADER_NAME = "owner.header"
LOCAL_ORG_ID = "org_local"


class StoreError(Exception):
    """Base class for errors that can safely reach an API client."""


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    pass


class Forbidden(StoreError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Credential expiry requires an offset.")
    return parsed.astimezone(UTC)


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _lstat_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _database_path(state_dir: Path) -> Path:
    return state_dir / DATABASE_NAME


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL
        );
        INSERT INTO schema_meta (singleton, schema_version) VALUES (1, 22);

        CREATE TABLE organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE principals (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            role TEXT NOT NULL CHECK (role IN ('human', 'agent')),
            token_hash TEXT NOT NULL UNIQUE,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
            kind TEXT NOT NULL CHECK (kind IN ('local_owner', 'human_session', 'agent', 'worker')),
            audience TEXT NOT NULL CHECK (audience IN ('control', 'worker')),
            project_id TEXT REFERENCES projects(id),
            parent_id TEXT REFERENCES credentials(id),
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            created_by_principal_id TEXT REFERENCES principals(id)
        );
        CREATE INDEX credentials_by_parent ON credentials(parent_id);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX projects_by_org ON projects(org_id, created_at, id);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL,
            purpose TEXT NOT NULL,
            budget_cents INTEGER NOT NULL CHECK (budget_cents >= 0),
            max_duration_seconds INTEGER NOT NULL
                CHECK (max_duration_seconds BETWEEN 1 AND 86400),
            status TEXT NOT NULL CHECK (status IN ('draft', 'approved')),
            revision INTEGER NOT NULL,
            current_plan_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX tasks_by_project ON tasks(project_id, created_at, id);
        CREATE TABLE task_revisions (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            revision INTEGER NOT NULL,
            title TEXT NOT NULL,
            purpose TEXT NOT NULL,
            budget_cents INTEGER NOT NULL CHECK (budget_cents >= 0),
            max_duration_seconds INTEGER NOT NULL
                CHECK (max_duration_seconds BETWEEN 1 AND 86400),
            execution_spec_json TEXT,
            plan_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            actor_principal_id TEXT REFERENCES principals(id),
            PRIMARY KEY (task_id, revision)
        );
        CREATE INDEX task_revisions_by_task ON task_revisions(task_id, revision);
        CREATE TABLE task_approvals (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            plan_revision INTEGER NOT NULL,
            plan_digest TEXT NOT NULL,
            approving_principal_id TEXT NOT NULL REFERENCES principals(id),
            approved_at TEXT NOT NULL,
            PRIMARY KEY (task_id, plan_revision)
        );
        CREATE TABLE events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT REFERENCES tasks(id),
            event_type TEXT NOT NULL,
            resource_revision INTEGER NOT NULL,
            actor_principal_id TEXT NOT NULL REFERENCES principals(id),
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX events_by_project_cursor ON events(project_id, cursor);
        CREATE TABLE idempotency (
            principal_id TEXT NOT NULL REFERENCES principals(id),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            idempotency_key TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            response_status INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (principal_id, org_id, idempotency_key, method, path)
        );
        """
    )
    # Imported only after Store itself is initialized to avoid schema helpers'
    # operation-layer imports forming a module cycle during startup.
    from .artifact_dispositions import create_artifact_disposition_schema
    from .artifact_manifests import create_artifact_manifest_schema
    from .artifacts import create_artifact_schema, create_artifact_upload_schema
    from .credential_leases import create_credential_access_lease_schema
    from .execution import create_execution_schema
    from .external_operations import (
        create_external_operation_receipt_schema,
        create_external_operation_schema,
    )
    from .handoffs import create_handoff_schema
    from .interaction import create_interaction_schema
    from .project_artifact_store import create_project_artifact_storage_schema
    from .recovery import create_recovery_schema
    from .research import create_research_schema
    from .result_acceptances import create_result_acceptance_schema
    from .result_checkpoints import create_result_checkpoint_schema
    from .skills import create_skill_schema
    from .snapshot_releases import create_release_schema
    from .snapshots import create_snapshot_schema
    from .workers import create_worker_schema

    create_research_schema(connection)
    create_interaction_schema(connection)
    create_snapshot_schema(connection)
    create_release_schema(connection)
    create_execution_schema(connection)
    create_artifact_schema(connection)
    create_artifact_upload_schema(connection)
    create_artifact_manifest_schema(connection)
    create_artifact_disposition_schema(connection)
    create_recovery_schema(connection)
    create_worker_schema(connection)
    create_result_checkpoint_schema(connection)
    create_skill_schema(connection)
    create_handoff_schema(connection)
    create_external_operation_schema(connection)
    create_external_operation_receipt_schema(connection)
    create_result_acceptance_schema(connection)
    create_credential_access_lease_schema(connection)
    create_project_artifact_storage_schema(connection)


def validate_database(database_path: Path) -> None:
    if _lstat_is_symlink(database_path) or not database_path.is_file():
        raise ValueError("Control-plane database is not a regular file.")
    connection = _connect(database_path)
    try:
        row = connection.execute(
            "SELECT schema_version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise ValueError("Control-plane database has no supported schema.") from error
    finally:
        connection.close()
    if row is None:
        raise ValueError("Control-plane database schema version is unsupported.")
    if row["schema_version"] < SCHEMA_VERSION:
        raise ValueError("Control-plane database requires an explicit `tessera migrate`.")
    if row["schema_version"] > SCHEMA_VERSION:
        raise ValueError("Control-plane database schema version is unsupported.")
    connection = _connect(database_path)
    try:
        recovery = connection.execute(
            "SELECT state FROM control_recovery WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise ValueError("Control-plane database has no recovery state.") from error
    finally:
        connection.close()
    if recovery is None:
        raise ValueError("Control-plane database has no recovery state.")
    marker = database_path.parent / ".restore-complete"
    if recovery["state"] == "held_restore" and (_lstat_is_symlink(marker) or not marker.is_file()):
        raise ValueError("Restored control-plane state was not completely published.")


def migrate_state(state_dir: Path) -> int:
    """Explicitly migrate an existing state database and return its schema version."""
    database_path = _database_path(state_dir)
    if _lstat_is_symlink(database_path) or not database_path.is_file():
        raise ValueError("Control-plane database is not a regular file.")
    return migrate_database(database_path, _connect)


def initialize_state(state_dir: Path) -> tuple[Path, Path]:
    """Create a new private state directory and return database/header paths.

    This deliberately refuses an existing target instead of guessing whether it is
    safe to add credentials to it.
    """

    state_dir = state_dir.expanduser()
    if state_dir.exists() or _lstat_is_symlink(state_dir):
        raise ValueError("State directory already exists or is not safe to initialize.")
    parent = state_dir.parent
    if not parent.is_dir() or _lstat_is_symlink(parent):
        raise ValueError("State directory parent is not a safe directory.")
    try:
        state_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        database_path = _database_path(state_dir)
        connection = _connect(database_path)
        try:
            _create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            token = secrets.token_urlsafe(32)
            principal_id = "principal_" + uuid.uuid4().hex
            credential_id = "credential_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
                (LOCAL_ORG_ID, "Local personal organization", now),
            )
            connection.execute(
                """INSERT INTO principals
                (id, org_id, role, token_hash, revoked_at, created_at)
                VALUES (?, ?, 'human', ?, NULL, ?)""",
                (principal_id, LOCAL_ORG_ID, _token_hash(token), now),
            )
            connection.execute(
                """INSERT INTO credentials
                (id, principal_id, kind, audience, project_id, parent_id, expires_at,
                 revoked_at, created_at, created_by_principal_id)
                VALUES (?, ?, 'local_owner', 'control', NULL, NULL, NULL, NULL, ?, ?)""",
                (credential_id, principal_id, now, principal_id),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        os.chmod(database_path, 0o600)
        header_path = state_dir / OWNER_HEADER_NAME
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(header_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, f"Authorization: Bearer {token}\n".encode())
        finally:
            os.close(descriptor)
        return database_path, header_path
    except Exception:
        # Leave a failed bootstrap visible for diagnosis; never delete a path that
        # might have been populated by another process after our initial check.
        raise


def canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, state_dir: Path):
        self.database_path = _database_path(state_dir)
        validate_database(self.database_path)

    def authenticate(self, token: str) -> dict[str, Any]:
        return self._authenticate(token, "control")

    def authenticate_worker(self, token: str) -> dict[str, Any]:
        """Authenticate only a worker-audience credential for future worker routes."""
        return self._authenticate(token, "worker")

    def _authenticate(self, token: str, audience: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            row = connection.execute(
                """SELECT p.id, p.org_id, p.role, c.id AS credential_id, c.kind,
                c.audience, c.project_id, c.expires_at, c.revoked_at, c.parent_id
                FROM principals p LEFT JOIN credentials c ON c.principal_id = p.id
                WHERE p.token_hash = ? AND p.revoked_at IS NULL""",
                (_token_hash(token),),
            ).fetchone()
            return self._credential_principal(connection, row, audience)
        finally:
            connection.close()

    @staticmethod
    def _credential_principal(
        connection: sqlite3.Connection, row: sqlite3.Row | None, audience: str
    ) -> dict[str, Any]:
        if row is None:
            raise Forbidden("Unauthorized")
        principal = dict(row)
        if principal["credential_id"] is None:
            raise Forbidden("Unauthorized")
        credential_id: str | None = principal["credential_id"]
        seen_credentials: set[str] = set()
        credential_count = 0
        while credential_id is not None:
            credential_count += 1
            if credential_id in seen_credentials or credential_count > 16:
                raise Forbidden("Unauthorized")
            seen_credentials.add(credential_id)
            credential = connection.execute(
                """SELECT c.id, c.principal_id, c.kind, c.audience, c.project_id,
                c.parent_id, c.expires_at, c.revoked_at, c.created_at,
                p.org_id, p.role, p.revoked_at AS principal_revoked_at
                FROM credentials c JOIN principals p ON p.id = c.principal_id WHERE c.id = ?""",
                (credential_id,),
            ).fetchone()
            if (
                credential is None
                or credential["revoked_at"] is not None
                or credential["principal_revoked_at"] is not None
            ):
                raise Forbidden("Unauthorized")
            if credential["org_id"] != principal["org_id"]:
                raise Forbidden("Unauthorized")
            if credential_count == 1 and credential["audience"] != audience:
                raise Forbidden("Unauthorized")
            Store._validate_credential_structure(connection, credential)
            try:
                created_at = _parse_utc(credential["created_at"])
            except ValueError as error:
                raise Forbidden("Unauthorized") from error
            expires_at = credential["expires_at"]
            if expires_at is not None:
                try:
                    expiry = _parse_utc(expires_at)
                except ValueError as error:
                    raise Forbidden("Unauthorized") from error
                if expiry > created_at + timedelta(hours=24) or expiry <= datetime.now(UTC):
                    raise Forbidden("Unauthorized")
            credential_id = credential["parent_id"]
        return principal

    @staticmethod
    def _validate_credential_structure(
        connection: sqlite3.Connection, credential: sqlite3.Row
    ) -> None:
        kind = credential["kind"]
        role = credential["role"]
        audience = credential["audience"]
        project_id = credential["project_id"]
        parent_id = credential["parent_id"]
        expires_at = credential["expires_at"]
        if kind == "local_owner":
            valid = (
                role == "human"
                and audience == "control"
                and project_id is None
                and parent_id is None
                and expires_at is None
            )
        elif kind == "human_session":
            valid = role == "human" and audience == "control" and parent_id is not None
        elif kind == "agent":
            valid = (
                role == "agent"
                and audience == "control"
                and project_id is not None
                and parent_id is not None
            )
        elif kind == "worker":
            valid = (
                role == "agent"
                and audience == "worker"
                and project_id is not None
                and parent_id is not None
            )
        else:
            valid = False
        if not valid or (kind != "local_owner" and expires_at is None):
            raise Forbidden("Unauthorized")
        if project_id is not None:
            project = connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND org_id = ?",
                (project_id, credential["org_id"]),
            ).fetchone()
            if project is None:
                raise Forbidden("Unauthorized")

    def list_projects(
        self, principal: dict[str, str], offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            principal = self._live_principal(connection, principal)
            if principal["project_id"] is not None:
                project = self._project(connection, principal, principal["project_id"])
                return [project][offset : offset + limit]
            rows = connection.execute(
                """SELECT id, org_id, name, created_at FROM projects
                WHERE org_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (principal["org_id"], limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_project(self, principal: dict[str, str], project_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            principal = self._live_principal(connection, principal)
            return self._project(connection, principal, project_id)
        finally:
            connection.close()

    def list_tasks(
        self, principal: dict[str, str], project_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            principal = self._live_principal(connection, principal)
            self._project(connection, principal, project_id)
            rows = connection.execute(
                """SELECT id, project_id, title, purpose, budget_cents,
                max_duration_seconds, status, revision, current_plan_digest, created_at, updated_at
                FROM tasks WHERE project_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [self._task_response(row) for row in rows]
        finally:
            connection.close()

    def get_task(self, principal: dict[str, str], task_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            principal = self._live_principal(connection, principal)
            row = self._task(connection, principal, task_id)
            return self._task_response(row)
        finally:
            connection.close()

    def list_task_revisions(
        self, principal: dict[str, str], task_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            principal = self._live_principal(connection, principal)
            self._task(connection, principal, task_id)
            rows = connection.execute(
                """SELECT revision, title, purpose, budget_cents, max_duration_seconds,
                execution_spec_json, plan_digest, created_at, actor_principal_id
                FROM task_revisions WHERE task_id = ? ORDER BY revision LIMIT ? OFFSET ?""",
                (task_id, limit, offset),
            ).fetchall()
            return [self._revision_response(row) for row in rows]
        finally:
            connection.close()

    def labbook(
        self, principal: dict[str, str], project_id: str, after: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            principal = self._live_principal(connection, principal)
            self._project(connection, principal, project_id)
            rows = connection.execute(
                """SELECT cursor, project_id, task_id, event_type, resource_revision,
                actor_principal_id, created_at, payload_json FROM events
                WHERE project_id = ? AND cursor > ? ORDER BY cursor LIMIT ?""",
                (project_id, after, limit),
            ).fetchall()
            return [
                {
                    "cursor": row["cursor"],
                    "project_id": row["project_id"],
                    "task_id": row["task_id"],
                    "event_type": row["event_type"],
                    "resource_revision": row["resource_revision"],
                    "actor_principal_id": row["actor_principal_id"],
                    "created_at": row["created_at"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def me(self, principal: dict[str, Any]) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            return self._credential_response(actor)
        finally:
            connection.close()

    def recovery_status(self, principal: dict[str, Any]) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._live_principal(connection, principal)
            row = connection.execute("SELECT * FROM control_recovery WHERE singleton=1").fetchone()
            grant_scope = ""
            parameters: list[Any] = [actor["org_id"]]
            if actor["project_id"] is not None:
                grant_scope = " AND project_id=?"
                parameters.append(actor["project_id"])
            count = connection.execute(
                (
                    """SELECT COUNT(*) FROM execution_grants
                    WHERE status IN ('active', 'cancel_requested')
                    AND project_id IN (SELECT id FROM projects WHERE org_id=?)"""
                    + grant_scope
                ),
                parameters,
            ).fetchone()[0]
            grants = connection.execute(
                """SELECT id, project_id, task_id, status, revision, epoch, expires_at,
                currency, max_exposure_cents
                FROM execution_grants WHERE status IN ('active', 'cancel_requested')
                AND project_id IN (SELECT id FROM projects WHERE org_id=?)
                """
                + grant_scope
                + " ORDER BY created_at, id LIMIT 100",
                parameters,
            ).fetchall()
            return {
                "state": row["state"],
                "recovery_hold": row["state"] == "held_restore",
                "incarnation_id": row["incarnation"],
                "restore_id": row["restore_id"],
                "source_backup": {
                    "digest": row["source_backup_digest"],
                    "event_watermark": row["event_watermark"],
                    "restored_at": row["restored_at"],
                },
                "original_branch_fenced": False,
                "unknown_post_backup_activity": row["state"] == "held_restore",
                "unresolved_grants": {
                    "count": count,
                    "listed_count": len(grants),
                    "truncated": count > len(grants),
                    "grants": [dict(grant) for grant in grants],
                },
            }
        finally:
            connection.close()

    def list_credentials(
        self, principal: dict[str, Any], offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            if not self._is_root_owner(actor):
                raise Forbidden("Only a local owner can list credentials.")
            rows = connection.execute(
                """SELECT c.id AS credential_id, p.id, p.org_id, p.role, c.kind, c.audience,
                c.project_id, c.expires_at, c.revoked_at, c.parent_id
                FROM credentials c JOIN principals p ON p.id = c.principal_id
                WHERE p.org_id = ? ORDER BY c.created_at, c.id LIMIT ? OFFSET ?""",
                (actor["org_id"], limit, offset),
            ).fetchall()
            return [self._credential_response(dict(row)) for row in rows]
        finally:
            connection.close()

    def issue_credential(
        self,
        principal: dict[str, Any],
        key: str,
        digest: str,
        payload: dict[str, Any],
        token: str,
    ) -> tuple[int, dict[str, Any]]:
        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            if not self._is_root_owner(actor):
                raise Forbidden("Only a local owner can issue credentials.")
            expires_at = _parse_utc(payload["expires_at"])
            now_datetime = datetime.now(UTC)
            if expires_at <= now_datetime or expires_at > now_datetime + timedelta(hours=24):
                raise Conflict("Credential expiry must be within the next 24 hours.")
            project_id = payload.get("project_id")
            if project_id is not None:
                self._project(connection, actor, project_id)
            credential_id = "credential_" + uuid.uuid4().hex
            principal_id = "principal_" + uuid.uuid4().hex
            role = "human" if payload["kind"] == "human_session" else "agent"
            now = utc_now()
            connection.execute(
                """INSERT INTO principals
                (id, org_id, role, token_hash, revoked_at, created_at)
                VALUES (?, ?, ?, ?, NULL, ?)""",
                (principal_id, actor["org_id"], role, _token_hash(token), now),
            )
            connection.execute(
                """INSERT INTO credentials
                (id, principal_id, kind, audience, project_id, parent_id, expires_at,
                 revoked_at, created_at, created_by_principal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    credential_id,
                    principal_id,
                    payload["kind"],
                    payload["audience"],
                    project_id,
                    actor["credential_id"],
                    _utc_string(expires_at),
                    now,
                    actor["id"],
                ),
            )
            return 201, self._credential_response(
                {
                    "credential_id": credential_id,
                    "id": principal_id,
                    "org_id": actor["org_id"],
                    "role": role,
                    "kind": payload["kind"],
                    "audience": payload["audience"],
                    "project_id": project_id,
                    "expires_at": _utc_string(expires_at),
                    "revoked_at": None,
                    "parent_id": actor["credential_id"],
                }
            )

        return self._mutate(principal, key, "POST", "/credentials", digest, command)

    def revoke_credential(
        self, principal: dict[str, Any], credential_id: str, key: str, digest: str
    ) -> tuple[int, dict[str, Any]]:
        path = f"/credentials/{credential_id}/revoke"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            row = connection.execute(
                """SELECT c.id AS credential_id, p.id, p.org_id, p.role, c.kind, c.audience,
                c.project_id, c.expires_at, c.revoked_at, c.parent_id
                FROM credentials c JOIN principals p ON p.id = c.principal_id
                WHERE c.id = ? AND p.org_id = ?""",
                (credential_id, actor["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Credential was not found.")
            credential = dict(row)
            if not self._is_root_owner(actor) and credential["id"] != actor["id"]:
                raise Forbidden("Only a local owner can revoke other credentials.")
            if credential["revoked_at"] is None:
                revoked_at = utc_now()
                connection.execute(
                    "UPDATE credentials SET revoked_at = ? WHERE id = ?",
                    (revoked_at, credential_id),
                )
                credential["revoked_at"] = revoked_at
            return 200, self._credential_response(credential)

        return self._mutate(principal, key, "POST", path, digest, command)

    def create_project(
        self, principal: dict[str, str], key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        def command(
            connection: sqlite3.Connection, actor: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            if actor["project_id"] is not None:
                raise Forbidden("Project-scoped credentials cannot create projects.")
            now = utc_now()
            project = {
                "id": "project_" + uuid.uuid4().hex,
                "org_id": actor["org_id"],
                "name": payload["name"],
                "created_at": now,
            }
            connection.execute(
                "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
                (project["id"], project["org_id"], project["name"], now),
            )
            self._event(
                connection,
                actor,
                project["id"],
                None,
                "project.created",
                1,
                {"name": project["name"]},
            )
            return 201, project

        return self._mutate(principal, key, "POST", "/projects", digest, command)

    def create_task(
        self,
        principal: dict[str, str],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        path = f"/projects/{project_id}/tasks"

        def command(
            connection: sqlite3.Connection, actor: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            self._project(connection, actor, project_id)
            now = utc_now()
            current_plan_digest = plan_digest(
                payload["title"],
                payload["purpose"],
                payload["budget_cents"],
                payload["max_duration_seconds"],
                None,
            )
            task = {
                "id": "task_" + uuid.uuid4().hex,
                "project_id": project_id,
                "title": payload["title"],
                "purpose": payload["purpose"],
                "budget_cents": payload["budget_cents"],
                "max_duration_seconds": payload["max_duration_seconds"],
                "status": "draft",
                "revision": 1,
                "current_plan_digest": current_plan_digest,
                "created_at": now,
                "updated_at": now,
                "execution_granted": False,
            }
            connection.execute(
                """INSERT INTO tasks (id, project_id, title, purpose, budget_cents,
                max_duration_seconds, status, revision, current_plan_digest, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task["id"],
                    task["project_id"],
                    task["title"],
                    task["purpose"],
                    task["budget_cents"],
                    task["max_duration_seconds"],
                    task["status"],
                    task["revision"],
                    task["current_plan_digest"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO task_revisions
                (task_id, revision, title, purpose, budget_cents, max_duration_seconds,
                 execution_spec_json, plan_digest, created_at, actor_principal_id)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (
                    task["id"],
                    task["revision"],
                    task["title"],
                    task["purpose"],
                    task["budget_cents"],
                    task["max_duration_seconds"],
                    task["current_plan_digest"],
                    now,
                    actor["id"],
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                task["id"],
                "task.created",
                1,
                {
                    "plan_digest": task["current_plan_digest"],
                    "status": "draft",
                    "title": task["title"],
                },
            )
            return 201, self._task_response(task)

        return self._mutate(principal, key, "POST", path, digest, command)

    def approve_task(
        self,
        principal: dict[str, str],
        task_id: str,
        key: str,
        digest: str,
        expected_revision: int,
        expected_plan_digest: str | None,
    ) -> tuple[int, dict[str, Any]]:
        path = f"/tasks/{task_id}/approve"

        def command(
            connection: sqlite3.Connection, actor: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            task = self._task(connection, actor, task_id)
            if actor["role"] != "human":
                raise Forbidden("Human approval is required.")
            if task["revision"] != expected_revision or task["status"] != "draft":
                raise Conflict("Task revision is no longer approvable.")
            if (
                expected_plan_digest is not None
                and task["current_plan_digest"] != expected_plan_digest
            ):
                raise Conflict("Task plan digest is no longer approvable.")
            now = utc_now()
            connection.execute(
                """UPDATE tasks SET status = 'approved', revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'draft'""",
                (now, task_id, expected_revision),
            )
            updated = dict(task)
            updated.update(status="approved", revision=expected_revision + 1, updated_at=now)
            connection.execute(
                """INSERT INTO task_approvals
                (task_id, plan_revision, plan_digest, approving_principal_id, approved_at)
                VALUES (?, ?, ?, ?, ?)""",
                (task_id, expected_revision, task["current_plan_digest"], actor["id"], now),
            )
            self._event(
                connection,
                actor,
                task["project_id"],
                task_id,
                "task.approved",
                expected_revision + 1,
                {
                    "approved_plan_digest": task["current_plan_digest"],
                    "approved_plan_revision": expected_revision,
                    "approving_principal_id": actor["id"],
                    "status": "approved",
                    "approval_scope": "research_plan_only",
                    "execution_granted": False,
                },
            )
            return 200, self._task_response(updated)

        return self._mutate(principal, key, "POST", path, digest, command)

    def revise_task_plan(
        self,
        principal: dict[str, str],
        task_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        path = f"/tasks/{task_id}/plan"

        def command(
            connection: sqlite3.Connection, actor: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            task = self._task(connection, actor, task_id)
            if actor["role"] != "human":
                raise Forbidden("Human plan revision is required.")
            if task["revision"] != payload["expected_revision"]:
                raise Conflict("Task revision is no longer current.")
            if connection.execute(
                """SELECT 1 FROM execution_grants
                WHERE task_id = ? AND status != 'reconciled' LIMIT 1""",
                (task_id,),
            ).fetchone():
                raise Conflict(
                    "Reconcile outstanding execution authority before revising the plan."
                )
            now = utc_now()
            revision = task["revision"] + 1
            execution_spec = payload["execution_spec"]
            current_plan_digest = plan_digest(
                payload["title"],
                payload["purpose"],
                payload["budget_cents"],
                payload["max_duration_seconds"],
                execution_spec,
            )
            connection.execute(
                """UPDATE tasks SET title = ?, purpose = ?, budget_cents = ?,
                max_duration_seconds = ?, status = 'draft', revision = ?, current_plan_digest = ?,
                updated_at = ? WHERE id = ? AND revision = ?""",
                (
                    payload["title"],
                    payload["purpose"],
                    payload["budget_cents"],
                    payload["max_duration_seconds"],
                    revision,
                    current_plan_digest,
                    now,
                    task_id,
                    task["revision"],
                ),
            )
            connection.execute(
                """INSERT INTO task_revisions
                (task_id, revision, title, purpose, budget_cents, max_duration_seconds,
                 execution_spec_json, plan_digest, created_at, actor_principal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    revision,
                    payload["title"],
                    payload["purpose"],
                    payload["budget_cents"],
                    payload["max_duration_seconds"],
                    json.dumps(execution_spec, separators=(",", ":"), sort_keys=True),
                    current_plan_digest,
                    now,
                    actor["id"],
                ),
            )
            updated = dict(task)
            updated.update(
                title=payload["title"],
                purpose=payload["purpose"],
                budget_cents=payload["budget_cents"],
                max_duration_seconds=payload["max_duration_seconds"],
                status="draft",
                revision=revision,
                current_plan_digest=current_plan_digest,
                updated_at=now,
            )
            self._event(
                connection,
                actor,
                task["project_id"],
                task_id,
                "task.plan_revised",
                revision,
                {
                    "invalidated_approval": task["status"] == "approved",
                    "plan_digest": current_plan_digest,
                    "status": "draft",
                },
            )
            return 200, self._task_response(updated)

        return self._mutate(principal, key, "PUT", path, digest, command)

    def _mutate(
        self,
        principal: dict[str, str],
        key: str,
        method: str,
        path: str,
        digest: str,
        command: Any,
        *,
        audience: str = "control",
        replay_guard: Any | None = None,
    ) -> tuple[int, dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            live_principal = self._live_principal(connection, principal, audience=audience)
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if recovery is None or recovery["state"] != "active":
                raise Forbidden("Control state is held after restore; mutations are disabled.")
            existing = connection.execute(
                """SELECT request_digest, response_status, response_json FROM idempotency
                WHERE principal_id = ? AND org_id = ? AND idempotency_key = ?
                AND method = ? AND path = ?""",
                (live_principal["id"], live_principal["org_id"], key, method, path),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise Conflict("Idempotency key was already used with a different request.")
                response = json.loads(existing["response_json"])
                if replay_guard is not None:
                    replay_guard(connection, live_principal, response)
                connection.execute("COMMIT")
                return existing["response_status"], response
            status, response = command(connection, live_principal)
            connection.execute(
                """INSERT INTO idempotency
                (principal_id, org_id, idempotency_key, method, path, request_digest,
                 response_status, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    live_principal["id"],
                    live_principal["org_id"],
                    key,
                    method,
                    path,
                    digest,
                    status,
                    json.dumps(response, separators=(",", ":"), sort_keys=True),
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")
            return status, response
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _project(
        connection: sqlite3.Connection, principal: dict[str, Any], project_id: str
    ) -> dict[str, Any]:
        if principal["project_id"] is not None and principal["project_id"] != project_id:
            raise NotFound("Project was not found.")
        row = connection.execute(
            "SELECT id, org_id, name, created_at FROM projects WHERE id = ? AND org_id = ?",
            (project_id, principal["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Project was not found.")
        return dict(row)

    @staticmethod
    def _live_principal(
        connection: sqlite3.Connection, principal: dict[str, Any], *, audience: str = "control"
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT p.id, p.org_id, p.role, c.id AS credential_id, c.kind,
            c.audience, c.project_id, c.expires_at, c.revoked_at, c.parent_id
            FROM principals p LEFT JOIN credentials c ON c.principal_id = p.id
            WHERE p.id = ? AND p.org_id = ? AND p.revoked_at IS NULL""",
            (principal["id"], principal["org_id"]),
        ).fetchone()
        return Store._credential_principal(connection, row, audience)

    @staticmethod
    def _is_root_owner(principal: dict[str, Any]) -> bool:
        return (
            principal["role"] == "human"
            and principal["kind"] == "local_owner"
            and principal["parent_id"] is None
        )

    @staticmethod
    def _credential_response(principal: dict[str, Any]) -> dict[str, Any]:
        return {
            "credential_id": principal["credential_id"],
            "principal_id": principal["id"],
            "org_id": principal["org_id"],
            "role": principal["role"],
            "kind": principal["kind"],
            "audience": principal["audience"],
            "project_id": principal["project_id"],
            "expires_at": principal["expires_at"],
            "revoked_at": principal["revoked_at"],
            "parent_id": principal["parent_id"],
        }

    @staticmethod
    def _task(
        connection: sqlite3.Connection, principal: dict[str, Any], task_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT t.id, t.project_id, t.title, t.purpose, t.budget_cents,
            t.max_duration_seconds, t.status, t.revision, t.current_plan_digest,
            t.created_at, t.updated_at
            FROM tasks t JOIN projects p ON p.id = t.project_id
            WHERE t.id = ? AND p.org_id = ?""",
            (task_id, principal["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Task was not found.")
        task = dict(row)
        if principal["project_id"] is not None and principal["project_id"] != task["project_id"]:
            raise NotFound("Task was not found.")
        return task

    @staticmethod
    def _task_response(task: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        result = dict(task)
        result["plan_digest"] = result["current_plan_digest"]
        result["execution_granted"] = False
        return result

    @staticmethod
    def _revision_response(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "revision": row["revision"],
            "title": row["title"],
            "purpose": row["purpose"],
            "budget_cents": row["budget_cents"],
            "max_duration_seconds": row["max_duration_seconds"],
            "execution_spec": (
                json.loads(row["execution_spec_json"])
                if row["execution_spec_json"] is not None
                else None
            ),
            "plan_digest": row["plan_digest"],
            "created_at": row["created_at"],
            "actor_principal_id": row["actor_principal_id"],
        }

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        principal: dict[str, str],
        project_id: str,
        task_id: str | None,
        event_type: str,
        revision: int,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO events
            (org_id, project_id, task_id, event_type, resource_revision,
             actor_principal_id, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                principal["org_id"],
                project_id,
                task_id,
                event_type,
                revision,
                principal["id"],
                utc_now(),
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )
