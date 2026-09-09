"""Provider-neutral, metadata-only credential access leases.

This module deliberately has no production secret backend.  The private in-memory
backend is a test/development seam: it never serializes values and access handles
are validated again before a local trusted caller may resolve one.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from .credential_lease_models import (
    CreateCredentialAccessLease,
    RenewCredentialAccessLease,
    RevokeCredentialAccessLease,
)
from .execution import ExecutionStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    utc_now,
)
from .workers import WorkerRegistryStore

HANDLE_SECONDS = 15


def create_credential_access_lease_schema(connection: sqlite3.Connection) -> None:
    """Install empty metadata-only leases; no secret values or inferred leases."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS credential_access_leases (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        execution_lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        secret_ref TEXT NOT NULL CHECK (length(secret_ref) BETWEEN 1 AND 200),
        audience TEXT NOT NULL CHECK (length(audience) BETWEEN 1 AND 200),
        purpose TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 200),
        scope_json TEXT NOT NULL CHECK (length(scope_json) BETWEEN 2 AND 8192),
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        revoked_by_principal_id TEXT REFERENCES principals(id),
        UNIQUE(execution_lease_id, secret_ref, audience, purpose)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS credential_access_leases_by_project
        ON credential_access_leases(project_id, created_at, id)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS credential_access_leases_by_execution_lease
        ON credential_access_leases(execution_lease_id, epoch, id)"""
    )


class _FakeSecretBackend:
    """Private process-local test backend; values never cross persistence/API boundaries."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._handles: dict[str, tuple[str, str, datetime, Callable[[], bool]]] = {}
        self._revoked: set[str] = set()
        self.access_log: list[dict[str, str]] = []

    def put(self, secret_ref: str, value: str) -> None:
        self._values[secret_ref] = value

    def issue_handle(
        self, lease_id: str, secret_ref: str, expires_at: datetime, valid: Callable[[], bool]
    ) -> str:
        handle = "fake_handle_" + uuid.uuid4().hex
        self._handles[handle] = (lease_id, secret_ref, expires_at, valid)
        self.access_log.append({"action": "issued", "lease_id": lease_id, "handle": handle})
        return handle

    def resolve(self, handle: str) -> str:
        record = self._handles.get(handle)
        if record is None:
            self.access_log.append({"action": "denied", "handle": handle})
            raise Forbidden("Credential access handle is unavailable.")
        lease_id, secret_ref, expires_at, valid = record
        if lease_id in self._revoked or expires_at <= datetime.now(UTC) or not valid():
            self._revoked.add(lease_id)
            self.access_log.append({"action": "denied", "lease_id": lease_id, "handle": handle})
            raise Forbidden("Credential access handle is no longer current.")
        if secret_ref not in self._values:
            self.access_log.append({"action": "missing", "lease_id": lease_id, "handle": handle})
            raise NotFound("Fake secret reference is unavailable.")
        self.access_log.append({"action": "resolved", "lease_id": lease_id, "handle": handle})
        return self._values[secret_ref]

    def revoke(self, lease_id: str) -> None:
        self._revoked.add(lease_id)
        self.access_log.append({"action": "revoked", "lease_id": lease_id})


class CredentialAccessLeaseStore(ExecutionStore):
    """Authority records around opaque secret references, not authentication credentials."""

    @staticmethod
    def _require_human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("A human is required for credential access authority.")

    @staticmethod
    def _body(model: Any, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid credential access lease request.") from error

    @staticmethod
    def _response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["scope"] = json.loads(value.pop("scope_json"))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise Conflict("Credential access lease scope is invalid.") from error
        value["metadata_only"] = True
        return value

    def _record(
        self, connection: sqlite3.Connection, actor: dict[str, Any], lease_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM credential_access_leases WHERE id=? AND org_id=?""",
            (lease_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Credential access lease was not found.")
        self._project(connection, actor, row["project_id"])
        return row

    def _current(
        self, connection: sqlite3.Connection, worker: dict[str, Any], row: sqlite3.Row
    ) -> sqlite3.Row:
        if (
            worker["kind"] != "worker"
            or worker["role"] != "agent"
            or row["worker_principal_id"] != worker["id"]
            or row["project_id"] != worker["project_id"]
            or row["org_id"] != worker["org_id"]
        ):
            raise Forbidden("Worker is not authorized for this credential access lease.")
        if row["revoked_at"] is not None:
            raise Conflict("Credential access lease is revoked.")
        try:
            if _parse_utc(row["expires_at"]) <= datetime.now(UTC):
                raise Conflict("Credential access lease has expired.")
        except ValueError as error:
            raise Conflict("Credential access lease expiry is invalid.") from error
        parent = connection.execute(
            "SELECT * FROM execution_leases WHERE id=?", (row["execution_lease_id"],)
        ).fetchone()
        if (
            parent is None
            or parent["grant_id"] != row["grant_id"]
            or parent["worker_principal_id"] != worker["id"]
            or parent["epoch"] != row["epoch"]
        ):
            raise Conflict("Credential access lease parent binding is no longer current.")
        try:
            if _parse_utc(parent["expires_at"]) <= datetime.now(UTC):
                raise Conflict("Execution lease has expired.")
        except ValueError as error:
            raise Conflict("Execution lease expiry is invalid.") from error
        grant = self._active_worker_grant(connection, worker, row["grant_id"])
        if (
            grant["project_id"] != row["project_id"]
            or grant["task_id"] != row["task_id"]
            or grant["epoch"] != row["epoch"]
        ):
            raise Conflict("Credential access lease epoch is fenced.")
        if _parse_utc(row["expires_at"]) > _parse_utc(parent["expires_at"]):
            raise Conflict("Credential access lease outlives its execution lease.")
        WorkerRegistryStore.require_ready_worker(connection, worker, row["project_id"])
        return parent

    def create(
        self, actor: dict[str, Any], grant_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(CreateCredentialAccessLease, payload)
        path = f"/execution-grants/{grant_id}/credential-access-leases"

        def create_command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(live)
            grant = self._grant(connection, live, grant_id)
            parent = connection.execute(
                "SELECT * FROM execution_leases WHERE id=? AND grant_id=?",
                (payload["execution_lease_id"], grant_id),
            ).fetchone()
            if parent is None:
                raise NotFound("Execution lease was not found.")
            if parent["epoch"] != payload["expected_epoch"] or grant["epoch"] != parent["epoch"]:
                raise Conflict("Execution lease epoch is no longer current.")
            worker = self._worker(
                connection, live["org_id"], parent["worker_principal_id"], grant["project_id"]
            )
            grant = self._active_worker_grant(connection, worker, grant_id)
            WorkerRegistryStore.require_ready_worker(connection, worker, grant["project_id"])
            if grant["status"] != "active" or _parse_utc(grant["expires_at"]) <= datetime.now(UTC):
                raise Conflict("Execution grant is not active for credential access.")
            if _parse_utc(parent["expires_at"]) <= datetime.now(UTC):
                raise Conflict("Execution lease has expired.")
            expiry = _parse_utc(payload["expires_at"])
            if expiry <= datetime.now(UTC) or expiry > _parse_utc(parent["expires_at"]):
                raise Conflict("Credential access lease expiry must be within its execution lease.")
            if connection.execute(
                """SELECT 1 FROM credential_access_leases WHERE execution_lease_id=?
                AND secret_ref=? AND audience=? AND purpose=?""",
                (parent["id"], payload["secret_ref"], payload["audience"], payload["purpose"]),
            ).fetchone():
                raise Conflict("Credential access lease already exists for this binding.")
            now, ident = utc_now(), "credential_access_lease_" + uuid.uuid4().hex
            row = {
                "id": ident,
                "org_id": live["org_id"],
                "project_id": grant["project_id"],
                "task_id": grant["task_id"],
                "grant_id": grant_id,
                "execution_lease_id": parent["id"],
                "worker_principal_id": worker["id"],
                "epoch": parent["epoch"],
                "secret_ref": payload["secret_ref"],
                "audience": payload["audience"],
                "purpose": payload["purpose"],
                "scope_json": json.dumps(
                    payload["scope"], separators=(",", ":"), ensure_ascii=True
                ),
                "expires_at": _utc_string(expiry),
                "revoked_at": None,
                "revision": 1,
                "created_at": now,
                "renewed_at": now,
                "created_by_principal_id": live["id"],
                "revoked_by_principal_id": None,
            }
            connection.execute(
                """INSERT INTO credential_access_leases
                (id,org_id,project_id,task_id,grant_id,execution_lease_id,worker_principal_id,epoch,
                 secret_ref,audience,purpose,scope_json,expires_at,revoked_at,revision,created_at,
                 renewed_at,created_by_principal_id,revoked_by_principal_id)
                VALUES (:id,:org_id,:project_id,:task_id,:grant_id,:execution_lease_id,
                :worker_principal_id,:epoch,:secret_ref,:audience,:purpose,:scope_json,:expires_at,
                :revoked_at,:revision,:created_at,:renewed_at,:created_by_principal_id,
                :revoked_by_principal_id)""",
                row,
            )
            self._event(
                connection,
                live,
                grant["project_id"],
                grant["task_id"],
                "credential_access.lease_created",
                1,
                {
                    "credential_access_lease_id": ident,
                    "grant_id": grant_id,
                    "execution_lease_id": parent["id"],
                    "worker_principal_id": worker["id"],
                    "epoch": parent["epoch"],
                    "audience": payload["audience"],
                    "purpose": payload["purpose"],
                    "scope": payload["scope"],
                    "metadata_only": True,
                },
            )
            return 201, self._response(row)

        return self._mutate(actor, key, "POST", path, digest, create_command)

    def list(
        self, actor: dict[str, Any], project_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._require_human(live)
            self._project(connection, live, project_id)
            rows = connection.execute(
                """SELECT * FROM credential_access_leases WHERE org_id=? AND project_id=?
                ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (live["org_id"], project_id, limit, offset),
            ).fetchall()
            return [self._response(row) for row in rows]
        finally:
            connection.close()

    def renew(
        self, actor: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(RenewCredentialAccessLease, payload)
        path = f"/credential-access-leases/{lease_id}/renew"

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(live)
            row = self._record(connection, live, lease_id)
            if row["revision"] != payload["expected_revision"]:
                raise Conflict("Credential access lease revision is no longer current.")
            worker = self._worker(
                connection, live["org_id"], row["worker_principal_id"], row["project_id"]
            )
            parent = self._current(connection, worker, row)
            expiry = _parse_utc(payload["expires_at"])
            if expiry <= datetime.now(UTC) or expiry > _parse_utc(parent["expires_at"]):
                raise Conflict("Credential access lease expiry must be within its execution lease.")
            now = utc_now()
            updated = dict(row)
            updated.update(
                expires_at=_utc_string(expiry), renewed_at=now, revision=row["revision"] + 1
            )
            connection.execute(
                """UPDATE credential_access_leases SET expires_at=?, renewed_at=?, revision=?
                WHERE id=? AND revision=?""",
                (updated["expires_at"], now, updated["revision"], lease_id, row["revision"]),
            )
            self._event(
                connection,
                live,
                row["project_id"],
                row["task_id"],
                "credential_access.lease_renewed",
                updated["revision"],
                {
                    "credential_access_lease_id": lease_id,
                    "execution_lease_id": parent["id"],
                    "epoch": row["epoch"],
                    "metadata_only": True,
                },
            )
            return 200, self._response(updated)

        return self._mutate(actor, key, "POST", path, digest, command)

    def revoke(
        self, actor: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(RevokeCredentialAccessLease, payload)
        path = f"/credential-access-leases/{lease_id}/revoke"

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(live)
            row = self._record(connection, live, lease_id)
            if row["revoked_at"] is not None:
                return 200, self._response(row)
            if row["revision"] != payload["expected_revision"]:
                raise Conflict("Credential access lease revision is no longer current.")
            now = utc_now()
            updated = dict(row)
            updated.update(
                revoked_at=now, revoked_by_principal_id=live["id"], revision=row["revision"] + 1
            )
            connection.execute(
                """UPDATE credential_access_leases
                SET revoked_at=?, revoked_by_principal_id=?, revision=?
                WHERE id=? AND revision=?""",
                (now, live["id"], updated["revision"], lease_id, row["revision"]),
            )
            self._event(
                connection,
                live,
                row["project_id"],
                row["task_id"],
                "credential_access.lease_revoked",
                updated["revision"],
                {
                    "credential_access_lease_id": lease_id,
                    "grant_id": row["grant_id"],
                    "execution_lease_id": row["execution_lease_id"],
                    "epoch": row["epoch"],
                    "metadata_only": True,
                },
            )
            return 200, self._response(updated)

        return self._mutate(actor, key, "POST", path, digest, command)

    def issue_handle(
        self, worker: dict[str, Any], lease_id: str, backend: _FakeSecretBackend
    ) -> dict[str, str]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, worker, audience="worker")
            row = self._record(connection, live, lease_id)
            self._current(connection, live, row)
            expiry = min(
                _parse_utc(row["expires_at"]), datetime.now(UTC) + timedelta(seconds=HANDLE_SECONDS)
            )
            handle = backend.issue_handle(
                lease_id,
                row["secret_ref"],
                expiry,
                lambda: self._handle_current(worker, lease_id),
            )
            return {
                "credential_access_lease_id": lease_id,
                "handle": handle,
                "expires_at": _utc_string(expiry),
            }
        finally:
            connection.close()

    def _handle_current(self, worker: dict[str, Any], lease_id: str) -> bool:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, worker, audience="worker")
            row = self._record(connection, live, lease_id)
            self._current(connection, live, row)
            return True
        except (Conflict, Forbidden, NotFound, ValueError):
            return False
        finally:
            connection.close()
