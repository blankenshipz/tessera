"""Bounded confirmed-stop -> human edit -> handback records.

This module only joins already-captured immutable snapshots.  It never receives a
filesystem path or bytes and cannot dispatch, accept a result, or issue authority.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .handoff_models import ConfirmWorkspaceStop, HandBackWorkspace, RequestWorkspaceHandoff
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, _parse_utc, utc_now


def create_handoff_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS workspace_handoffs (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        baseline_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        baseline_digest TEXT NOT NULL,
        workspace_label TEXT NOT NULL,
        bound_lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        bound_worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        bound_lease_epoch INTEGER NOT NULL CHECK (bound_lease_epoch >= 1),
        stop_epoch INTEGER NOT NULL CHECK (stop_epoch >= 1),
        status TEXT NOT NULL CHECK
            (status IN ('stop_requested', 'stopped_confirmed', 'handed_back')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        confirmation_observation_id TEXT REFERENCES execution_observations(id),
        human_owner_principal_id TEXT REFERENCES principals(id),
        result_snapshot_id TEXT REFERENCES source_snapshots(id),
        result_digest TEXT,
        changes_json TEXT,
        exclusions_json TEXT,
        context_epoch INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        confirmed_at TEXT, handed_back_at TEXT
        )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS workspace_handoffs_one_open_per_task
        ON workspace_handoffs(task_id) WHERE status IN ('stop_requested', 'stopped_confirmed')"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS workspace_handoffs_by_project
        ON workspace_handoffs(project_id, created_at, id)"""
    )


class WorkspaceHandoffStore(Store):
    @staticmethod
    def _require_human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("A human is required for workspace handoff.")

    @staticmethod
    def _body(model: Any, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid workspace handoff request.") from error

    @staticmethod
    def _snapshot(
        connection: sqlite3.Connection, actor: dict[str, Any], snapshot_id: str
    ) -> sqlite3.Row:
        from .snapshots import SnapshotStore

        return SnapshotStore._snapshot(connection, actor, snapshot_id)

    @staticmethod
    def _manifest(row: sqlite3.Row) -> dict[str, Any]:
        from .snapshots import SnapshotStore

        return SnapshotStore._response(row)["manifest"]

    @staticmethod
    def _verify_snapshot(
        connection: sqlite3.Connection, actor: dict[str, Any], snapshot_id: str
    ) -> None:
        from .snapshots import SnapshotStore

        SnapshotStore._verify_retained(connection, actor, snapshot_id)

    def _handoff(
        self, connection: sqlite3.Connection, actor: dict[str, Any], handoff_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workspace_handoffs WHERE id=? AND org_id=?",
            (handoff_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Workspace handoff was not found.")
        self._project(connection, actor, row["project_id"])
        return row

    @staticmethod
    def _diff(
        baseline: dict[str, Any], result: dict[str, Any]
    ) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
        before = {entry["relative_path"]: entry["sha256"] for entry in baseline["entries"]}
        after = {entry["relative_path"]: entry["sha256"] for entry in result["entries"]}
        changes = {
            "added": sorted(set(after) - set(before)),
            "deleted": sorted(set(before) - set(after)),
            "changed": sorted(
                path for path in set(before) & set(after) if before[path] != after[path]
            ),
        }
        exclusions = [*baseline["exclusions"], *result["exclusions"]]
        return changes, exclusions

    @staticmethod
    def _response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for field in ("changes_json", "exclusions_json"):
            encoded = value.pop(field)
            value[field.removesuffix("_json")] = (
                json.loads(encoded) if encoded is not None else None
            )
        value["human_owner"] = value["status"] in {"stopped_confirmed", "handed_back"}
        value["execution_dispatched"] = False
        value["operational_only"] = True
        return value

    def request_handoff(self, actor, project_id, key, digest, payload):
        payload = self._body(RequestWorkspaceHandoff, payload)
        path = f"/projects/{project_id}/workspace-handoffs"

        def command(connection, live):
            self._require_human(live)
            self._project(connection, live, project_id)
            task = self._task(connection, live, payload["task_id"])
            if task["project_id"] != project_id:
                raise NotFound("Task was not found.")
            if task["revision"] != payload["expected_task_revision"]:
                raise Conflict("Task revision is no longer current for workspace handoff.")
            grant = connection.execute(
                """SELECT * FROM execution_grants WHERE id=? AND project_id=?""",
                (payload["grant_id"], project_id),
            ).fetchone()
            if grant is None or grant["task_id"] != task["id"]:
                raise NotFound("Execution grant was not found.")
            if (
                grant["revision"] != payload["expected_grant_revision"]
                or grant["status"] != "active"
            ):
                raise Conflict("Execution grant is no longer active at the expected revision.")
            if (
                grant["task_revision"] != task["revision"]
                or grant["plan_digest"] != task["current_plan_digest"]
            ):
                raise Conflict("Execution grant task basis is no longer current.")
            baseline = self._snapshot(connection, live, payload["baseline_snapshot_id"])
            if (
                baseline["project_id"] != project_id
                or baseline["digest"] != payload["expected_baseline_digest"]
            ):
                raise Conflict("Workspace baseline snapshot does not match the selected handoff.")
            self._verify_snapshot(connection, live, baseline["id"])
            self._manifest(baseline)
            lease = connection.execute(
                """SELECT * FROM execution_leases WHERE grant_id=?
                ORDER BY issued_at DESC, id DESC LIMIT 1""",
                (grant["id"],),
            ).fetchone()
            if lease is None or lease["worker_principal_id"] != grant["worker_principal_id"]:
                raise Conflict(
                    "A current worker lease is required before workspace takeover can be requested."
                )
            if _parse_utc(lease["expires_at"]) <= datetime.now(UTC):
                raise Conflict(
                    "A current worker lease is required before workspace takeover can be requested."
                )
            if connection.execute(
                """SELECT 1 FROM workspace_handoffs WHERE task_id=?
                AND status IN ('stop_requested', 'stopped_confirmed')""",
                (task["id"],),
            ).fetchone():
                raise Conflict("Task already has an unresolved workspace handoff.")
            from .execution import ExecutionStore

            # Order is intentional and transactional: fence the grant and every
            # journaled operation first, record the handoff second, then append
            # cancellation and handoff events.  A retry returns the one durable
            # handoff response through the enclosing idempotency command.
            cancelled, operation_ids = ExecutionStore._request_grant_cancellation(
                self, connection, grant, grant["revision"]
            )
            stop_epoch, now, ident = cancelled["epoch"], utc_now(), "handoff_" + uuid.uuid4().hex
            row = {
                "id": ident,
                "org_id": live["org_id"],
                "project_id": project_id,
                "task_id": task["id"],
                "grant_id": grant["id"],
                "baseline_snapshot_id": baseline["id"],
                "baseline_digest": baseline["digest"],
                "workspace_label": payload["workspace_label"],
                "bound_lease_id": lease["id"],
                "bound_worker_principal_id": lease["worker_principal_id"],
                "bound_lease_epoch": lease["epoch"],
                "stop_epoch": stop_epoch,
                "status": "stop_requested",
                "revision": 1,
                "confirmation_observation_id": None,
                "human_owner_principal_id": None,
                "result_snapshot_id": None,
                "result_digest": None,
                "changes_json": None,
                "exclusions_json": None,
                "context_epoch": None,
                "created_at": now,
                "updated_at": now,
                "confirmed_at": None,
                "handed_back_at": None,
            }
            connection.execute(
                """INSERT INTO workspace_handoffs VALUES
                (:id,:org_id,:project_id,:task_id,:grant_id,
                :baseline_snapshot_id,:baseline_digest,:workspace_label,:bound_lease_id,
                :bound_worker_principal_id,:bound_lease_epoch,:stop_epoch,:status,:revision,
                :confirmation_observation_id,:human_owner_principal_id,:result_snapshot_id,:result_digest,
                :changes_json,:exclusions_json,:context_epoch,:created_at,:updated_at,:confirmed_at,:handed_back_at)""",
                row,
            )
            self._event(
                connection,
                live,
                project_id,
                task["id"],
                "execution.grant_cancel_requested",
                cancelled["revision"],
                {
                    "grant_id": grant["id"],
                    "epoch": stop_epoch,
                    "operation_ids": operation_ids,
                    "requested_by": "workspace_handoff",
                    "cancellation": ExecutionStore._cancellation_payload(
                        self, connection, cancelled
                    ),
                    "operational_only": True,
                },
            )
            self._event(
                connection,
                live,
                project_id,
                task["id"],
                "workspace.handoff_stop_requested",
                1,
                {
                    "handoff_id": ident,
                    "grant_id": grant["id"],
                    "lease_id": lease["id"],
                    "stop_epoch": stop_epoch,
                    "operation_ids": operation_ids,
                    "baseline_digest": baseline["digest"],
                    "operational_only": True,
                },
            )
            return 201, self._response(row)

        return self._mutate(actor, key, "POST", path, digest, command)

    def confirm_stop(self, actor, handoff_id, key, digest, expected_revision):
        expected_revision = self._body(
            ConfirmWorkspaceStop, {"expected_revision": expected_revision}
        )["expected_revision"]
        path = f"/workspace-handoffs/{handoff_id}/confirm-stop"

        def command(connection, live):
            self._require_human(live)
            handoff = self._handoff(connection, live, handoff_id)
            if handoff["revision"] != expected_revision or handoff["status"] != "stop_requested":
                raise Conflict("Workspace handoff is no longer awaiting stop confirmation.")
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (handoff["grant_id"],)
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (handoff["bound_lease_id"],)
            ).fetchone()
            if (
                grant is None
                or lease is None
                or grant["status"] not in {"cancel_requested", "reconciled"}
                or grant["epoch"] != handoff["stop_epoch"]
            ):
                raise Conflict("Workspace handoff authority fence is no longer current.")
            if (
                lease["worker_principal_id"] != handoff["bound_worker_principal_id"]
                or lease["epoch"] != handoff["bound_lease_epoch"]
            ):
                raise Conflict("Workspace handoff lease is no longer the selected worker lease.")
            observation = connection.execute(
                """SELECT * FROM execution_observations WHERE grant_id=?
                ORDER BY rowid DESC LIMIT 1""",
                (grant["id"],),
            ).fetchone()
            if observation is None or (
                observation["lease_id"] != handoff["bound_lease_id"]
                or observation["worker_principal_id"] != handoff["bound_worker_principal_id"]
                or observation["epoch"] != handoff["bound_lease_epoch"]
                or observation["desired_state"] != "cancel_requested"
                or observation["observed_state"] != "exited"
                or not observation["termination_confirmed"]
                or observation["provider_id"] is None
            ):
                raise Conflict("A matching worker-confirmed exited observation is required.")
            if _parse_utc(observation["observed_at"]) > _parse_utc(lease["expires_at"]):
                raise Conflict(
                    "Worker confirmation was not observed during the selected current lease."
                )
            providers = connection.execute(
                """SELECT DISTINCT provider_id FROM execution_observations WHERE grant_id=?
                AND provider_id IS NOT NULL""",
                (grant["id"],),
            ).fetchall()
            if len(providers) != 1 or providers[0]["provider_id"] != observation["provider_id"]:
                raise Conflict("Worker provider identity is not stable for workspace takeover.")
            if connection.execute(
                """SELECT 1 FROM execution_grants WHERE task_id=? AND id != ?
                AND status IN ('active', 'cancel_requested')""",
                (handoff["task_id"], grant["id"]),
            ).fetchone():
                raise Conflict("Continuing shared writer authority blocks workspace takeover.")
            now = utc_now()
            connection.execute(
                """UPDATE workspace_handoffs SET status='stopped_confirmed', revision=revision+1,
                confirmation_observation_id=?, human_owner_principal_id=?,
                updated_at=?, confirmed_at=?
                WHERE id=? AND revision=? AND status='stop_requested'""",
                (observation["id"], live["id"], now, now, handoff_id, expected_revision),
            )
            updated = dict(handoff)
            updated.update(
                status="stopped_confirmed",
                revision=expected_revision + 1,
                confirmation_observation_id=observation["id"],
                human_owner_principal_id=live["id"],
                updated_at=now,
                confirmed_at=now,
            )
            self._event(
                connection,
                live,
                handoff["project_id"],
                handoff["task_id"],
                "workspace.handoff_stopped_confirmed",
                updated["revision"],
                {
                    "handoff_id": handoff_id,
                    "observation_id": observation["id"],
                    "provider_id": observation["provider_id"],
                    "human_owner_principal_id": live["id"],
                    "operational_only": True,
                },
            )
            return 200, self._response(updated)

        return self._mutate(actor, key, "POST", path, digest, command)

    def hand_back(self, actor, handoff_id, key, digest, payload):
        payload = self._body(HandBackWorkspace, payload)
        path = f"/workspace-handoffs/{handoff_id}/handback"

        def command(connection, live):
            self._require_human(live)
            handoff = self._handoff(connection, live, handoff_id)
            if (
                handoff["revision"] != payload["expected_revision"]
                or handoff["status"] != "stopped_confirmed"
            ):
                raise Conflict("Workspace handoff is not owned by a human at this revision.")
            if handoff["human_owner_principal_id"] != live["id"]:
                raise Forbidden("Only the attributed human workspace owner may hand work back.")
            if handoff["baseline_digest"] != payload["expected_baseline_digest"]:
                raise Conflict("Workspace baseline digest no longer matches the handoff.")
            baseline = self._snapshot(connection, live, handoff["baseline_snapshot_id"])
            result = self._snapshot(connection, live, payload["result_snapshot_id"])
            if (
                result["project_id"] != handoff["project_id"]
                or result["digest"] != payload["expected_result_digest"]
            ):
                raise Conflict("Workspace result snapshot does not match the selected handback.")
            if baseline["digest"] != handoff["baseline_digest"]:
                raise Conflict(
                    "Workspace baseline snapshot integrity no longer matches the handoff."
                )
            self._verify_snapshot(connection, live, baseline["id"])
            self._verify_snapshot(connection, live, result["id"])
            baseline_manifest, result_manifest = self._manifest(baseline), self._manifest(result)
            changes, exclusions = self._diff(baseline_manifest, result_manifest)
            from .execution import ExecutionStore

            context_epoch = ExecutionStore._next_epoch(self, connection, handoff["task_id"])
            now = utc_now()
            connection.execute(
                """UPDATE workspace_handoffs SET status='handed_back', revision=revision+1,
                result_snapshot_id=?, result_digest=?, changes_json=?, exclusions_json=?,
                context_epoch=?, updated_at=?, handed_back_at=? WHERE id=? AND revision=?
                AND status='stopped_confirmed'""",
                (
                    result["id"],
                    result["digest"],
                    json.dumps(changes, sort_keys=True),
                    json.dumps(exclusions, sort_keys=True),
                    context_epoch,
                    now,
                    now,
                    handoff_id,
                    payload["expected_revision"],
                ),
            )
            updated = dict(handoff)
            updated.update(
                status="handed_back",
                revision=payload["expected_revision"] + 1,
                result_snapshot_id=result["id"],
                result_digest=result["digest"],
                changes_json=json.dumps(changes, sort_keys=True),
                exclusions_json=json.dumps(exclusions, sort_keys=True),
                context_epoch=context_epoch,
                updated_at=now,
                handed_back_at=now,
            )
            self._event(
                connection,
                live,
                handoff["project_id"],
                handoff["task_id"],
                "workspace.handoff_handed_back",
                updated["revision"],
                {
                    "handoff_id": handoff_id,
                    "baseline_digest": handoff["baseline_digest"],
                    "result_digest": result["digest"],
                    "context_epoch": context_epoch,
                    "no_op": not any(changes.values()),
                    "operational_only": True,
                },
            )
            return 200, self._response(updated)

        return self._mutate(actor, key, "POST", path, digest, command)

    def get_handoff(self, actor, handoff_id):
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            return self._response(self._handoff(connection, live, handoff_id))
        finally:
            connection.close()

    def list_handoffs(self, actor, project_id, offset, limit):
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            rows = connection.execute(
                """SELECT * FROM workspace_handoffs WHERE project_id=?
                ORDER BY created_at,id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [self._response(row) for row in rows]
        finally:
            connection.close()
