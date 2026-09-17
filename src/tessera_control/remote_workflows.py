"""Durable, observation-only lifecycle for control-issued remote operations.

This journal is deliberately not a scheduler or a second authority engine.  Its
rows are created only beside the existing immutable external-operation intent;
worker lifecycle samples remain fenced by that operation's lease and the
existing observation/reconciliation predicates remain authoritative.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .execution import UNRESOLVED_GRANT_STATUSES, ExecutionStore
from .external_operations import current_operation_reconciliation
from .store import Conflict, NotFound, StoreError, _connect, utc_now

OperationId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RemoteWorkflowHeartbeat(_WorkflowRequest):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=1)]
    observed_state: Literal["running", "unknown", "exited"]


class RemoteWorkflowCheckpoint(_WorkflowRequest):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=1)]
    sequence: Annotated[int, Field(strict=True, ge=1)]
    checkpoint_cursor: Identifier
    checkpoint_digest: Digest


class RemoteWorkflowReconnect(_WorkflowRequest):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=1)]
    last_checkpoint_sequence: Annotated[int, Field(strict=True, ge=0)]
    control_outage: bool = False


def create_remote_workflow_schema(connection: sqlite3.Connection) -> None:
    """Install empty lifecycle rows; no legacy operation gains a fabricated run."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS remote_workflow_runs (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        experiment_attempt_id TEXT,
        experiment_id TEXT,
        experiment_design_revision INTEGER,
        experiment_variant_id TEXT,
        state TEXT NOT NULL CHECK (state IN ('prepared','running','unknown','exited')),
        last_heartbeat_at TEXT,
        last_checkpoint_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_checkpoint_sequence >= 0),
        last_checkpoint_cursor TEXT,
        last_checkpoint_digest TEXT,
        last_reconnected_at TEXT,
        control_outage_at TEXT,
        last_observed_at TEXT,
        last_observed_state TEXT CHECK (last_observed_state IN ('running','unknown','exited')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS remote_workflow_checkpoints (
        operation_id TEXT NOT NULL REFERENCES remote_workflow_runs(operation_id),
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        checkpoint_cursor TEXT NOT NULL,
        checkpoint_digest TEXT NOT NULL CHECK (
            length(checkpoint_digest)=64 AND checkpoint_digest NOT GLOB '*[^0-9a-f]*'),
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(operation_id, sequence), UNIQUE(operation_id, checkpoint_cursor))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS remote_workflow_runs_by_project "
        "ON remote_workflow_runs(project_id,updated_at,operation_id)"
    )


def record_remote_workflow_intent(
    connection: sqlite3.Connection, operation: dict[str, Any]
) -> None:
    """Create one lifecycle row with the same immutable operation/attempt identity."""
    now = str(operation["created_at"])
    connection.execute(
        """INSERT INTO remote_workflow_runs
        (operation_id,org_id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,
         experiment_attempt_id,experiment_id,experiment_design_revision,experiment_variant_id,
         state,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(operation_id) DO NOTHING""",
        (
            operation["operation_id"],
            operation["org_id"],
            operation["project_id"],
            operation["task_id"],
            operation["grant_id"],
            operation["lease_id"],
            operation["worker_principal_id"],
            operation["epoch"],
            operation.get("experiment_attempt_id"),
            operation.get("experiment_id"),
            operation.get("experiment_design_revision"),
            operation.get("experiment_variant_id"),
            "prepared",
            now,
            now,
        ),
    )


def sync_remote_workflow_observation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> None:
    """Project the reconciled evidence view; raw samples stay in their source journals."""
    reconciliation = current_operation_reconciliation(connection, operation_id)
    if reconciliation is None:
        return
    connection.execute(
        """UPDATE remote_workflow_runs SET state=?,last_observed_state=?,last_observed_at=?,
        updated_at=? WHERE operation_id=?""",
        (
            reconciliation["observed_state"],
            reconciliation["observed_state"],
            reconciliation["observed_at"],
            utc_now(),
            operation_id,
        ),
    )


class RemoteWorkflowStore(ExecutionStore):
    """Tenant-scoped lifecycle projections over already-issued remote operations."""

    @staticmethod
    def _body(model: type[_WorkflowRequest], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid remote workflow request.") from error

    @staticmethod
    def _response(
        connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
    ) -> dict[str, Any]:
        value = dict(row)
        operation = connection.execute(
            "SELECT desired_state FROM external_operations WHERE operation_id=? AND org_id=?",
            (value["operation_id"], value["org_id"]),
        ).fetchone()
        # This is a projection of the immutable operation intent, not a request to
        # stop and never an assertion that a remote process has terminated.
        value["operation_desired_state"] = operation["desired_state"] if operation else "unknown"
        reconciliation = current_operation_reconciliation(connection, value["operation_id"])
        value["reconciliation"] = reconciliation
        value["reconciliation_required"] = (
            reconciliation is None
            or reconciliation["observed_state"] != "exited"
            or not reconciliation["termination_confirmed"]
        )
        value["operational_only"] = True
        return value

    def _worker_workflow(
        self,
        connection: sqlite3.Connection,
        worker: dict[str, Any],
        operation_id: str,
        lease_id: str,
        epoch: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_workflow_runs WHERE operation_id=? AND org_id=?",
            (operation_id, worker["org_id"]),
        ).fetchone()
        if row is None or row["worker_principal_id"] != worker["id"]:
            raise NotFound("Remote workflow was not found.")
        if row["lease_id"] != lease_id or row["epoch"] != epoch:
            raise Conflict("Remote workflow lease identity is no longer current.")
        grant = self._grant(connection, worker, row["grant_id"])
        if grant["status"] not in UNRESOLVED_GRANT_STATUSES:
            raise Conflict("Remote workflow authority has already been reconciled.")
        # A cancelled grant permits only forensic/reconciliation lifecycle records.
        return row

    def list_workflows(
        self, actor: dict[str, Any], project_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM remote_workflow_runs WHERE project_id=?
                ORDER BY updated_at DESC,operation_id DESC LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [self._response(connection, row) for row in rows]
        finally:
            connection.close()

    def get_workflow(self, actor: dict[str, Any], operation_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT * FROM remote_workflow_runs WHERE operation_id=? AND org_id=?",
                (operation_id, actor["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Remote workflow was not found.")
            self._project(connection, actor, row["project_id"])
            return self._response(connection, row)
        finally:
            connection.close()

    def heartbeat(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(RemoteWorkflowHeartbeat, payload)
        path = f"/remote-workflows/{operation_id}/heartbeat"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            row = self._worker_workflow(
                connection, worker, operation_id, payload["lease_id"], payload["expected_epoch"]
            )
            now = utc_now()
            reconciliation = current_operation_reconciliation(connection, operation_id)
            # Heartbeats remain raw operational samples in the event ledger, but
            # cannot overwrite a projection already backed by timestamp/sequence-
            # ordered worker/provider reconciliation.
            state = (
                reconciliation["observed_state"]
                if reconciliation is not None
                else payload["observed_state"]
            )
            connection.execute(
                "UPDATE remote_workflow_runs SET state=?,last_heartbeat_at=?,updated_at=? "
                "WHERE operation_id=?",
                (state, now, now, operation_id),
            )
            updated = dict(row) | {"state": state, "last_heartbeat_at": now, "updated_at": now}
            self._event(
                connection,
                worker,
                row["project_id"],
                row["task_id"],
                "remote_workflow.heartbeat_recorded",
                row["epoch"],
                {
                    "operation_id": operation_id,
                    "observed_state": payload["observed_state"],
                    "operational_only": True,
                },
            )
            return 201, self._response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")

    def checkpoint(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(RemoteWorkflowCheckpoint, payload)
        path = f"/remote-workflows/{operation_id}/checkpoints"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            row = self._worker_workflow(
                connection, worker, operation_id, payload["lease_id"], payload["expected_epoch"]
            )
            if payload["sequence"] != row["last_checkpoint_sequence"] + 1:
                raise Conflict("Remote workflow checkpoint sequence is not next.")
            now = utc_now()
            connection.execute(
                """INSERT INTO remote_workflow_checkpoints
                (operation_id,sequence,checkpoint_cursor,checkpoint_digest,recorded_at)
                VALUES (?,?,?,?,?)""",
                (
                    operation_id,
                    payload["sequence"],
                    payload["checkpoint_cursor"],
                    payload["checkpoint_digest"],
                    now,
                ),
            )
            connection.execute(
                """UPDATE remote_workflow_runs
                SET last_checkpoint_sequence=?,last_checkpoint_cursor=?,
                last_checkpoint_digest=?,updated_at=? WHERE operation_id=?""",
                (
                    payload["sequence"],
                    payload["checkpoint_cursor"],
                    payload["checkpoint_digest"],
                    now,
                    operation_id,
                ),
            )
            updated = dict(row) | {
                "last_checkpoint_sequence": payload["sequence"],
                "last_checkpoint_cursor": payload["checkpoint_cursor"],
                "last_checkpoint_digest": payload["checkpoint_digest"],
                "updated_at": now,
            }
            self._event(
                connection,
                worker,
                row["project_id"],
                row["task_id"],
                "remote_workflow.checkpoint_recorded",
                row["epoch"],
                {
                    "operation_id": operation_id,
                    "sequence": payload["sequence"],
                    "checkpoint_digest": payload["checkpoint_digest"],
                    "operational_only": True,
                },
            )
            return 201, self._response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")

    def reconnect(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(RemoteWorkflowReconnect, payload)
        path = f"/remote-workflows/{operation_id}/reconnect"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            row = self._worker_workflow(
                connection, worker, operation_id, payload["lease_id"], payload["expected_epoch"]
            )
            if payload["last_checkpoint_sequence"] > row["last_checkpoint_sequence"]:
                raise Conflict("Remote workflow reconnect names an unknown checkpoint.")
            now = utc_now()
            reconciliation = current_operation_reconciliation(connection, operation_id)
            state = (
                "unknown"
                if payload["control_outage"] or reconciliation is None
                else reconciliation["observed_state"]
            )
            connection.execute(
                """UPDATE remote_workflow_runs
                SET state=?,last_reconnected_at=?,control_outage_at=?,updated_at=?
                WHERE operation_id=?""",
                (
                    state,
                    now,
                    now if payload["control_outage"] else row["control_outage_at"],
                    now,
                    operation_id,
                ),
            )
            updated = dict(row) | {
                "state": state,
                "last_reconnected_at": now,
                "control_outage_at": now if payload["control_outage"] else row["control_outage_at"],
                "updated_at": now,
            }
            self._event(
                connection,
                worker,
                row["project_id"],
                row["task_id"],
                "remote_workflow.reconnected",
                row["epoch"],
                {
                    "operation_id": operation_id,
                    "control_outage": payload["control_outage"],
                    "last_checkpoint_sequence": payload["last_checkpoint_sequence"],
                    "operational_only": True,
                },
            )
            return 200, self._response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")
