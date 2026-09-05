"""Explicit SQLite persistence for the local control plane."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
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
        INSERT INTO schema_meta (singleton, schema_version) VALUES (1, 1);

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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX tasks_by_project ON tasks(project_id, created_at, id);
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
    if row is None or row["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Control-plane database schema version is unsupported.")


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

    def authenticate(self, token: str) -> dict[str, str]:
        connection = _connect(self.database_path)
        try:
            row = connection.execute(
                """SELECT id, org_id, role FROM principals
                WHERE token_hash = ? AND revoked_at IS NULL""",
                (_token_hash(token),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise Forbidden("Unauthorized")
        return dict(row)

    def list_projects(
        self, principal: dict[str, str], offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
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
            return self._project(connection, principal["org_id"], project_id)
        finally:
            connection.close()

    def list_tasks(
        self, principal: dict[str, str], project_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            self._project(connection, principal["org_id"], project_id)
            rows = connection.execute(
                """SELECT id, project_id, title, purpose, budget_cents,
                max_duration_seconds, status, revision, created_at, updated_at
                FROM tasks WHERE project_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [self._task_response(row) for row in rows]
        finally:
            connection.close()

    def get_task(self, principal: dict[str, str], task_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            row = self._task(connection, principal["org_id"], task_id)
            return self._task_response(row)
        finally:
            connection.close()

    def labbook(
        self, principal: dict[str, str], project_id: str, after: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            self._project(connection, principal["org_id"], project_id)
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

    def create_project(
        self, principal: dict[str, str], key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        def command(connection: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            now = utc_now()
            project = {
                "id": "project_" + uuid.uuid4().hex,
                "org_id": principal["org_id"],
                "name": payload["name"],
                "created_at": now,
            }
            connection.execute(
                "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
                (project["id"], project["org_id"], project["name"], now),
            )
            self._event(
                connection,
                principal,
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

        def command(connection: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            self._project(connection, principal["org_id"], project_id)
            now = utc_now()
            task = {
                "id": "task_" + uuid.uuid4().hex,
                "project_id": project_id,
                "title": payload["title"],
                "purpose": payload["purpose"],
                "budget_cents": payload["budget_cents"],
                "max_duration_seconds": payload["max_duration_seconds"],
                "status": "draft",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "execution_granted": False,
            }
            connection.execute(
                """INSERT INTO tasks (id, project_id, title, purpose, budget_cents,
                max_duration_seconds, status, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task["id"],
                    task["project_id"],
                    task["title"],
                    task["purpose"],
                    task["budget_cents"],
                    task["max_duration_seconds"],
                    task["status"],
                    task["revision"],
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                principal,
                project_id,
                task["id"],
                "task.created",
                1,
                {"status": "draft", "title": task["title"]},
            )
            return 201, task

        return self._mutate(principal, key, "POST", path, digest, command)

    def approve_task(
        self,
        principal: dict[str, str],
        task_id: str,
        key: str,
        digest: str,
        expected_revision: int,
    ) -> tuple[int, dict[str, Any]]:
        path = f"/tasks/{task_id}/approve"

        def command(connection: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            task = self._task(connection, principal["org_id"], task_id)
            if principal["role"] != "human":
                raise Forbidden("Human approval is required.")
            if task["revision"] != expected_revision or task["status"] != "draft":
                raise Conflict("Task revision is no longer approvable.")
            now = utc_now()
            connection.execute(
                """UPDATE tasks SET status = 'approved', revision = 2, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'draft'""",
                (now, task_id, expected_revision),
            )
            updated = dict(task)
            updated.update(status="approved", revision=2, updated_at=now)
            self._event(
                connection,
                principal,
                task["project_id"],
                task_id,
                "task.approved",
                2,
                {
                    "status": "approved",
                    "approval_scope": "research_plan_only",
                    "execution_granted": False,
                },
            )
            return 200, self._task_response(updated)

        return self._mutate(principal, key, "POST", path, digest, command)

    def _mutate(
        self,
        principal: dict[str, str],
        key: str,
        method: str,
        path: str,
        digest: str,
        command: Any,
    ) -> tuple[int, dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT request_digest, response_status, response_json FROM idempotency
                WHERE principal_id = ? AND org_id = ? AND idempotency_key = ?
                AND method = ? AND path = ?""",
                (principal["id"], principal["org_id"], key, method, path),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise Conflict("Idempotency key was already used with a different request.")
                connection.execute("COMMIT")
                return existing["response_status"], json.loads(existing["response_json"])
            status, response = command(connection)
            connection.execute(
                """INSERT INTO idempotency
                (principal_id, org_id, idempotency_key, method, path, request_digest,
                 response_status, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    principal["id"],
                    principal["org_id"],
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
    def _project(connection: sqlite3.Connection, org_id: str, project_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT id, org_id, name, created_at FROM projects WHERE id = ? AND org_id = ?",
            (project_id, org_id),
        ).fetchone()
        if row is None:
            raise NotFound("Project was not found.")
        return dict(row)

    @staticmethod
    def _task(connection: sqlite3.Connection, org_id: str, task_id: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT t.id, t.project_id, t.title, t.purpose, t.budget_cents,
            t.max_duration_seconds, t.status, t.revision, t.created_at, t.updated_at
            FROM tasks t JOIN projects p ON p.id = t.project_id
            WHERE t.id = ? AND p.org_id = ?""",
            (task_id, org_id),
        ).fetchone()
        if row is None:
            raise NotFound("Task was not found.")
        return dict(row)

    @staticmethod
    def _task_response(task: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        result = dict(task)
        result["execution_granted"] = False
        return result

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
