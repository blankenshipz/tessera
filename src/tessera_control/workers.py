"""Bounded worker registration and lifecycle records; no attestation or brokering."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, _parse_utc, utc_now
from .worker_models import RegisterWorker, WorkerHeartbeat, WorkerLifecycle

HEARTBEAT_MAX_AGE = timedelta(seconds=60)


def create_worker_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS worker_registry (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        worker_principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
        credential_id TEXT NOT NULL UNIQUE REFERENCES credentials(id),
        approved_build_identifier TEXT NOT NULL,
        approved_build_digest TEXT NOT NULL,
        trust_class TEXT NOT NULL,
        region TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN
            ('ready', 'draining', 'disconnected', 'quarantined', 'retired')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        registered_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        retired_at TEXT,
        retired_by_principal_id TEXT REFERENCES principals(id)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS worker_registry_by_project
        ON worker_registry(project_id, registered_at, id)"""
    )


class WorkerRegistryStore(Store):
    """Owner-managed metadata and worker-authenticated liveness only."""

    @staticmethod
    def _body(model: Any, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid worker lifecycle request.") from error

    @staticmethod
    def _require_owner(actor: dict[str, Any]) -> None:
        if not Store._is_root_owner(actor):
            raise Forbidden("The local owner is required for worker lifecycle authority.")

    @staticmethod
    def _response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["approved_metadata_only"] = True
        value["build_attestation_verified"] = False
        value["capabilities_probed"] = False
        return value

    def _worker_record(
        self, connection: sqlite3.Connection, actor: dict[str, Any], worker_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM worker_registry WHERE id = ? AND org_id = ?""",
            (worker_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Worker was not found.")
        self._project(connection, actor, row["project_id"])
        return row

    def register_worker(
        self, actor: dict[str, Any], project_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(RegisterWorker, payload)
        path = f"/projects/{project_id}/workers"

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_owner(live)
            self._project(connection, live, project_id)
            worker = self._live_principal(
                connection,
                {"id": payload["worker_principal_id"], "org_id": live["org_id"]},
                audience="worker",
            )
            if worker["kind"] != "worker" or worker["project_id"] != project_id:
                raise Forbidden("Worker credential is not bound to this project.")
            if connection.execute(
                "SELECT 1 FROM worker_registry WHERE worker_principal_id = ?", (worker["id"],)
            ).fetchone():
                raise Conflict("Worker principal is already registered.")
            now = utc_now()
            row = {
                "id": "worker_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project_id,
                "worker_principal_id": worker["id"],
                "credential_id": worker["credential_id"],
                "approved_build_identifier": payload["approved_build_identifier"],
                "approved_build_digest": payload["approved_build_digest"],
                "trust_class": payload["trust_class"],
                "region": payload["region"],
                "lifecycle_state": "ready",
                "revision": 1,
                "registered_at": now,
                "heartbeat_at": now,
                "last_seen_at": now,
                "retired_at": None,
                "retired_by_principal_id": None,
            }
            connection.execute(
                """INSERT INTO worker_registry
                (id, org_id, project_id, worker_principal_id, credential_id,
                 approved_build_identifier, approved_build_digest, trust_class, region,
                 lifecycle_state, revision, registered_at,
                 heartbeat_at, last_seen_at, retired_at, retired_by_principal_id)
                VALUES (:id,:org_id,:project_id,:worker_principal_id,:credential_id,
                :approved_build_identifier,:approved_build_digest,:trust_class,:region,
                :lifecycle_state,:revision,:registered_at,
                :heartbeat_at,:last_seen_at,:retired_at,:retired_by_principal_id)""",
                row,
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "worker.registered",
                1,
                {
                    "worker_id": row["id"],
                    "worker_principal_id": worker["id"],
                    "approved_metadata_only": True,
                    "build_attestation_verified": False,
                    "capabilities_probed": False,
                },
            )
            return 201, self._response(row)

        return self._mutate(actor, key, "POST", path, digest, command)

    def lifecycle_worker(
        self,
        actor: dict[str, Any],
        worker_id: str,
        action: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(WorkerLifecycle, payload)
        target = {"drain": "draining", "quarantine": "quarantined", "retire": "retired"}[action]
        path = f"/workers/{worker_id}/{action}"

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_owner(live)
            row = self._worker_record(connection, live, worker_id)
            if row["revision"] != payload["expected_revision"]:
                raise Conflict("Worker lifecycle revision is no longer current.")
            if row["lifecycle_state"] == "retired":
                raise Conflict("Retired workers cannot change lifecycle state.")
            now = utc_now()
            updated = dict(row)
            updated.update(lifecycle_state=target, revision=row["revision"] + 1)
            if target == "retired":
                updated.update(retired_at=now, retired_by_principal_id=live["id"])
                connection.execute(
                    "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (now, row["credential_id"]),
                )
            connection.execute(
                """UPDATE worker_registry SET lifecycle_state=:lifecycle_state, revision=:revision,
                retired_at=:retired_at, retired_by_principal_id=:retired_by_principal_id
                WHERE id=:id AND revision=:expected""",
                updated | {"expected": row["revision"]},
            )
            self._event(
                connection,
                live,
                row["project_id"],
                None,
                f"worker.{target}",
                updated["revision"],
                {
                    "worker_id": worker_id,
                    "worker_principal_id": row["worker_principal_id"],
                    "credential_revoked": target == "retired",
                    "approved_metadata_only": True,
                },
            )
            return 200, self._response(updated)

        return self._mutate(actor, key, "POST", path, digest, command)

    def heartbeat(
        self, actor: dict[str, Any], key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        self._body(WorkerHeartbeat, payload)

        def command(
            connection: sqlite3.Connection, live: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            row = connection.execute(
                """SELECT * FROM worker_registry
                WHERE worker_principal_id = ? AND credential_id = ? AND org_id = ?""",
                (live["id"], live["credential_id"], live["org_id"]),
            ).fetchone()
            if row is None:
                raise Forbidden("Worker is not registered.")
            if row["lifecycle_state"] == "retired":
                raise Forbidden("Worker is retired.")
            now = utc_now()
            updated = dict(row)
            updated.update(heartbeat_at=now, last_seen_at=now, revision=row["revision"] + 1)
            connection.execute(
                """UPDATE worker_registry SET heartbeat_at=?, last_seen_at=?, revision=?
                WHERE id=? AND revision=?""",
                (now, now, updated["revision"], row["id"], row["revision"]),
            )
            self._event(
                connection,
                live,
                row["project_id"],
                None,
                "worker.heartbeat",
                updated["revision"],
                {"worker_id": row["id"], "approved_metadata_only": True},
            )
            return 200, self._response(updated)

        return self._mutate(
            actor, key, "POST", "/worker/heartbeat", digest, command, audience="worker"
        )

    def worker_status(self, actor: dict[str, Any]) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor, audience="worker")
            row = connection.execute(
                """SELECT * FROM worker_registry
                WHERE worker_principal_id=? AND credential_id=? AND org_id=?""",
                (live["id"], live["credential_id"], live["org_id"]),
            ).fetchone()
            if row is None:
                raise Forbidden("Worker is not registered.")
            return self._response(row)
        finally:
            connection.close()

    @classmethod
    def require_ready_worker(
        cls, connection: sqlite3.Connection, worker: dict[str, Any], project_id: str
    ) -> None:
        row = connection.execute(
            """SELECT lifecycle_state, last_seen_at FROM worker_registry
            WHERE worker_principal_id=? AND credential_id=? AND org_id=? AND project_id=?""",
            (worker["id"], worker["credential_id"], worker["org_id"], project_id),
        ).fetchone()
        if row is None:
            raise Forbidden("Worker is not registered for this project.")
        if row["lifecycle_state"] != "ready":
            raise Conflict("Worker is not ready for new lease authority.")
        try:
            last_seen = _parse_utc(row["last_seen_at"])
        except ValueError as error:
            raise Conflict("Worker liveness metadata is invalid.") from error
        if last_seen < datetime.now(UTC) - HEARTBEAT_MAX_AGE:
            raise Conflict("Worker is disconnected and cannot receive lease authority.")
