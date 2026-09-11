"""Authority records for the local CPU execution pilot, without dispatch."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from .execution_models import (
    AcquireExecutionLease,
    ConfigureProjectExecution,
    IssueExecutionGrant,
    ReconcileExecutionGrant,
    RenewExecutionLease,
    ReportExecutionObservation,
    ReportExternalOperationReceipt,
    RevokeExecutionGrant,
)
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    utc_now,
)

LEASE_SECONDS = 30
MAX_GRANT_LIFETIME = timedelta(hours=24)
MAX_CENTS = 2**63 - 1
MAX_OBSERVATION_FUTURE_SKEW = timedelta(seconds=5)
UNRESOLVED_GRANT_STATUSES = ("active", "cancel_requested")


def create_execution_schema(connection: sqlite3.Connection) -> None:
    """Install authority and accounting records without any execution tables."""
    connection.execute(
        """CREATE TABLE execution_project_limits (
        project_id TEXT PRIMARY KEY REFERENCES projects(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
        monetary_limit_cents INTEGER NOT NULL CHECK (monetary_limit_cents >= 0),
        max_concurrency INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 8),
        held_cents INTEGER NOT NULL DEFAULT 0 CHECK (held_cents >= 0),
        settled_cents INTEGER NOT NULL DEFAULT 0 CHECK (settled_cents >= 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE execution_task_epochs (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1)
        )"""
    )
    connection.execute(
        """CREATE TABLE execution_grants (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
        plan_digest TEXT NOT NULL,
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        snapshot_digest TEXT NOT NULL,
        snapshot_release_id TEXT NOT NULL REFERENCES snapshot_releases(id),
        snapshot_release_expires_at TEXT NOT NULL,
        pinned_image TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
        max_exposure_cents INTEGER NOT NULL CHECK (max_exposure_cents >= 0),
        acknowledges_unmeasured_infrastructure INTEGER NOT NULL CHECK
            (acknowledges_unmeasured_infrastructure IN (0, 1)),
        status TEXT NOT NULL CHECK (status IN ('active', 'cancel_requested', 'reconciled')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        reconciled_at TEXT,
        reconciled_by_principal_id TEXT REFERENCES principals(id),
        settlement_cents INTEGER CHECK (settlement_cents >= 0)
        )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX execution_one_unresolved_grant_per_task
        ON execution_grants(task_id) WHERE status IN ('active', 'cancel_requested')"""
    )
    connection.execute(
        """CREATE INDEX execution_grants_by_project
        ON execution_grants(project_id, created_at, id)"""
    )
    connection.execute(
        """CREATE TABLE execution_leases (
        id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, hard_expires_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX execution_leases_by_grant
        ON execution_leases(grant_id, expires_at, id)"""
    )
    connection.execute(
        """CREATE TABLE execution_observations (
        id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        desired_state TEXT NOT NULL CHECK (desired_state IN ('admitted', 'cancel_requested')),
        observed_state TEXT NOT NULL CHECK (observed_state IN ('running', 'exited', 'unknown')),
        observed_at TEXT NOT NULL, provider_id TEXT,
        termination_confirmed INTEGER NOT NULL CHECK (termination_confirmed IN (0, 1)),
        recorded_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX execution_observations_by_grant
        ON execution_observations(grant_id, recorded_at, id)"""
    )


class ExecutionStore(Store):
    """Durable policy, reservation and lease records; it never submits work."""

    @staticmethod
    def _require_human(principal: dict[str, Any]) -> None:
        if principal["role"] != "human":
            raise Forbidden("A human is required for execution authority.")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _body(model: Any, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid execution request.") from error

    @staticmethod
    def _limit_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["operational_only"] = True
        return value

    def _cancellation_payload(
        self, connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
    ) -> dict[str, Any]:
        """Project the immutable request/observation record without inventing a stop.

        This is deliberately derived from the durable grant and attributed worker
        observations rather than stored as a mutable status projection.  The latest
        observation wins: a later unknown observation must restore uncertainty.
        """
        requested_at = row["updated_at"] if row["status"] == "cancel_requested" else None
        if requested_at is None:
            request = connection.execute(
                """SELECT created_at FROM events
                WHERE event_type = 'execution.grant_cancel_requested'
                AND instr(payload_json, ?) > 0 ORDER BY cursor DESC LIMIT 1""",
                (f'"grant_id":"{row["id"]}"',),
            ).fetchone()
            if request is None:
                return {"state": "not_requested"}
            requested_at = request["created_at"]
        observation = connection.execute(
            """SELECT id, operation_id, lease_id, worker_principal_id, epoch, observed_state,
            observed_at, provider_id, termination_confirmed, recorded_at FROM execution_observations
            WHERE grant_id = ? ORDER BY rowid DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        result: dict[str, Any] = {"state": "requested", "requested_at": requested_at}
        if observation is None:
            return result
        observed = dict(observation)
        observed["termination_confirmed"] = bool(observed["termination_confirmed"])
        result["observation"] = observed
        result["state"] = (
            "confirmed_termination"
            if observed["observed_state"] == "exited" and observed["termination_confirmed"]
            else "unresolved"
        )
        return result

    def _grant_response(
        self, connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
    ) -> dict[str, Any]:
        value = dict(row)
        value["acknowledges_unmeasured_infrastructure"] = bool(
            value["acknowledges_unmeasured_infrastructure"]
        )
        value["desired_state"] = {
            "active": "admitted",
            "cancel_requested": "cancel_requested",
            "reconciled": None,
        }[value["status"]]
        value["execution_dispatched"] = False
        value["operational_only"] = True
        value["cancellation"] = self._cancellation_payload(connection, row)
        return value

    @staticmethod
    def _lease_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["execution_dispatched"] = False
        value["operational_only"] = True
        return value

    def _limit(
        self, connection: sqlite3.Connection, principal: dict[str, Any], project_id: str
    ) -> sqlite3.Row:
        self._project(connection, principal, project_id)
        row = connection.execute(
            "SELECT * FROM execution_project_limits WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise Conflict("Execution limits must be configured for this project.")
        return row

    def _grant(
        self, connection: sqlite3.Connection, principal: dict[str, Any], grant_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT g.* FROM execution_grants g JOIN projects p ON p.id = g.project_id
            WHERE g.id = ? AND p.org_id = ?""",
            (grant_id, principal["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Execution grant was not found.")
        self._project(connection, principal, row["project_id"])
        return row

    def _next_epoch(self, connection: sqlite3.Connection, task_id: str) -> int:
        row = connection.execute(
            "SELECT epoch FROM execution_task_epochs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            connection.execute("INSERT INTO execution_task_epochs VALUES (?, 1)", (task_id,))
            return 1
        epoch = row["epoch"] + 1
        connection.execute(
            "UPDATE execution_task_epochs SET epoch = ? WHERE task_id = ?", (epoch, task_id)
        )
        return epoch

    def _request_grant_cancellation(
        self,
        connection: sqlite3.Connection,
        grant: sqlite3.Row | dict[str, Any],
        expected_revision: int,
    ) -> tuple[dict[str, Any], list[str]]:
        """Atomically fence a grant and request cancellation of its real operations.

        This is deliberately a desired-state transition only.  It does not infer a
        provider stop from changing the grant or operation fence.  Callers append
        their command events after this transition, in the same transaction.
        """
        if grant["status"] != "active" or grant["revision"] != expected_revision:
            raise Conflict("Execution grant is no longer active at the expected revision.")
        epoch, now = ExecutionStore._next_epoch(self, connection, grant["task_id"]), utc_now()
        updated_count = connection.execute(
            """UPDATE execution_grants SET status='cancel_requested', revision=revision+1,
            epoch=?, updated_at=? WHERE id=? AND revision=? AND status='active'""",
            (epoch, now, grant["id"], expected_revision),
        ).rowcount
        if updated_count != 1:
            raise Conflict("Execution grant is no longer active at the expected revision.")
        operation_ids = [
            row["operation_id"]
            for row in connection.execute(
                """SELECT operation_id FROM external_operations
                WHERE grant_id=? ORDER BY operation_id""",
                (grant["id"],),
            ).fetchall()
        ]
        # A v17 lease can have no operation journal.  Do not create one here;
        # fence every existing journaled operation without claiming delivery.
        connection.execute(
            """UPDATE external_operations SET desired_state='cancel_requested', updated_at=?
            WHERE grant_id=? AND desired_state='admitted'""",
            (now, grant["id"]),
        )
        updated = dict(grant)
        updated.update(
            status="cancel_requested",
            revision=expected_revision + 1,
            epoch=epoch,
            updated_at=now,
        )
        return updated, operation_ids

    @staticmethod
    def _parse_future(value: str, maximum: timedelta, label: str) -> datetime:
        try:
            result = _parse_utc(value)
        except ValueError as error:
            raise Conflict(f"{label} requires an offset.") from error
        now = datetime.now(UTC)
        if result <= now or result > now + maximum:
            raise Conflict(f"{label} must be within the next 24 hours.")
        return result

    def _worker(
        self,
        connection: sqlite3.Connection,
        org_id: str,
        worker_principal_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        worker = self._live_principal(
            connection,
            {"id": worker_principal_id, "org_id": org_id},
            audience="worker",
        )
        if (
            worker["kind"] != "worker"
            or worker["role"] != "agent"
            or worker["project_id"] != project_id
        ):
            raise Forbidden("Worker is not authorized for this project.")
        return worker

    def _authorize_snapshot(
        self,
        connection: sqlite3.Connection,
        principal: dict[str, Any],
        snapshot_id: str,
        worker_principal_id: str,
        expected_digest: str,
        frozen_release_id: str | None = None,
    ) -> dict[str, Any]:
        # Keep this import local: snapshots layers its operations on Store too.
        from .snapshots import SnapshotStore

        release = SnapshotStore._authorized_snapshot(
            connection, principal, snapshot_id, worker_principal_id, expected_digest
        )
        if release["classification"] != "public":
            raise Forbidden("The snapshot release is not approved for this local CPU authority.")
        if frozen_release_id is not None and release["id"] != frozen_release_id:
            raise Forbidden("The snapshot release no longer matches this grant.")
        return release

    def configure_project_execution(
        self,
        principal: dict[str, Any],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ConfigureProjectExecution, payload)
        path = f"/projects/{project_id}/execution-limits"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(actor)
            self._project(connection, actor, project_id)
            current = connection.execute(
                "SELECT * FROM execution_project_limits WHERE project_id = ?", (project_id,)
            ).fetchone()
            expected = payload["expected_revision"]
            now = utc_now()
            if current is None:
                if expected != 0:
                    raise Conflict("Execution limits have not been configured at this revision.")
                row = {
                    "project_id": project_id,
                    "revision": 1,
                    "currency": payload["currency"],
                    "monetary_limit_cents": payload["monetary_limit_cents"],
                    "max_concurrency": payload["max_concurrency"],
                    "held_cents": 0,
                    "settled_cents": 0,
                    "created_at": now,
                    "updated_at": now,
                }
                connection.execute(
                    """INSERT INTO execution_project_limits
                    (project_id, revision, currency, monetary_limit_cents, max_concurrency,
                     held_cents, settled_cents, created_at, updated_at)
                    VALUES (:project_id, :revision, :currency, :monetary_limit_cents,
                    :max_concurrency, :held_cents, :settled_cents, :created_at, :updated_at)""",
                    row,
                )
                status = 201
            else:
                if current["revision"] != expected:
                    raise Conflict("Execution limits revision is no longer current.")
                exposure = current["held_cents"] + current["settled_cents"]
                if payload["monetary_limit_cents"] < exposure:
                    raise Conflict(
                        "Execution limit cannot be lower than held and settled exposure."
                    )
                active_count = connection.execute(
                    """SELECT count(*) FROM execution_grants
                    WHERE project_id = ? AND status IN ('active', 'cancel_requested')""",
                    (project_id,),
                ).fetchone()[0]
                if payload["currency"] != current["currency"] and (exposure or active_count):
                    raise Conflict("Execution currency cannot change while authority remains.")
                if payload["max_concurrency"] < active_count:
                    raise Conflict(
                        "Execution concurrency cannot be lower than unresolved authority."
                    )
                row = dict(current)
                row.update(
                    revision=current["revision"] + 1,
                    currency=payload["currency"],
                    monetary_limit_cents=payload["monetary_limit_cents"],
                    max_concurrency=payload["max_concurrency"],
                    updated_at=now,
                )
                connection.execute(
                    """UPDATE execution_project_limits
                    SET revision = :revision,
                        currency = :currency,
                        monetary_limit_cents = :monetary_limit_cents,
                        max_concurrency = :max_concurrency,
                        updated_at = :updated_at
                    WHERE project_id = :project_id AND revision = :expected""",
                    row | {"expected": expected},
                )
                status = 200
            self._event(
                connection,
                actor,
                project_id,
                None,
                "execution.limits_configured",
                row["revision"],
                {
                    "currency": row["currency"],
                    "monetary_limit_cents": row["monetary_limit_cents"],
                    "max_concurrency": row["max_concurrency"],
                    "operational_only": True,
                },
            )
            return status, self._limit_response(row)

        return self._mutate(principal, key, "PUT", path, digest, command)

    def issue_execution_grant(
        self,
        principal: dict[str, Any],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(IssueExecutionGrant, payload)
        path = f"/projects/{project_id}/execution-grants"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(actor)
            limits = self._limit(connection, actor, project_id)
            if payload["currency"] != limits["currency"]:
                raise Conflict("Grant currency must match the project execution currency.")
            task = self._task(connection, actor, payload["task_id"])
            if task["project_id"] != project_id:
                raise NotFound("Task was not found.")
            if (
                task["status"] != "approved"
                or task["revision"] != payload["expected_task_revision"]
                or task["current_plan_digest"] != payload["expected_plan_digest"]
            ):
                raise Conflict("Task approval revision or plan digest is no longer current.")
            approved = connection.execute(
                """SELECT 1 FROM task_approvals WHERE task_id = ? AND plan_digest = ?
                ORDER BY approved_at DESC LIMIT 1""",
                (task["id"], task["current_plan_digest"]),
            ).fetchone()
            spec_row = connection.execute(
                """SELECT execution_spec_json FROM task_revisions
                WHERE task_id = ? AND plan_digest = ? ORDER BY revision DESC LIMIT 1""",
                (task["id"], task["current_plan_digest"]),
            ).fetchone()
            if approved is None or spec_row is None or spec_row["execution_spec_json"] is None:
                raise Conflict("A current approved executable task plan is required.")
            try:
                spec = json.loads(spec_row["execution_spec_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise Conflict("Task execution specification is invalid.") from error
            if spec.get("profile") != "local-cpu" or spec.get("network_enabled") is not False:
                raise Conflict("Task execution profile is not supported by the local CPU pilot.")
            expected_digest = spec.get("input_snapshot_digest")
            if not isinstance(expected_digest, str):
                raise Conflict("Task input snapshot digest is invalid.")
            worker = self._worker(
                connection, actor["org_id"], payload["worker_principal_id"], project_id
            )
            from .skills import authorize_skill_release

            skill_releases = [
                authorize_skill_release(connection, actor, project_id, skill_digest, worker["id"])
                for skill_digest in spec["pinned_skill_digests"]
            ]
            release = self._authorize_snapshot(
                connection, actor, payload["snapshot_id"], worker["id"], expected_digest
            )
            expiry = self._parse_future(payload["expires_at"], MAX_GRANT_LIFETIME, "Grant expiry")
            try:
                release_expiry = _parse_utc(release["expires_at"])
            except (KeyError, TypeError, ValueError) as error:
                raise Conflict("Snapshot release expiry is invalid.") from error
            if expiry > release_expiry:
                raise Conflict("Grant expiry cannot outlive the snapshot release.")
            if payload["max_exposure_cents"] > task["budget_cents"]:
                raise Conflict("Grant exposure cannot exceed the approved task budget.")
            task_settled = connection.execute(
                """SELECT coalesce(sum(settlement_cents), 0) FROM execution_grants
                WHERE task_id = ? AND status = 'reconciled'""",
                (task["id"],),
            ).fetchone()[0]
            task_held = connection.execute(
                """SELECT coalesce(sum(max_exposure_cents), 0) FROM execution_grants
                WHERE task_id = ? AND status IN ('active', 'cancel_requested')""",
                (task["id"],),
            ).fetchone()[0]
            if task_settled + task_held + payload["max_exposure_cents"] > task["budget_cents"]:
                raise Conflict("Grant would exceed the approved task budget across prior exposure.")
            exposure = (
                limits["held_cents"] + limits["settled_cents"] + payload["max_exposure_cents"]
            )
            if exposure > limits["monetary_limit_cents"]:
                raise Conflict("Grant would exceed the project execution limit.")
            unresolved = connection.execute(
                """SELECT count(*) FROM execution_grants
                WHERE project_id = ? AND status IN ('active', 'cancel_requested')""",
                (project_id,),
            ).fetchone()[0]
            if unresolved >= limits["max_concurrency"]:
                raise Conflict("Grant would exceed the project execution concurrency limit.")
            if connection.execute(
                """SELECT 1 FROM execution_grants WHERE task_id = ?
                AND status IN ('active', 'cancel_requested')""",
                (task["id"],),
            ).fetchone():
                raise Conflict("Task already has unresolved execution authority.")
            if connection.execute(
                """SELECT 1 FROM workspace_handoffs WHERE task_id = ?
                AND status IN ('stop_requested', 'stopped_confirmed')""",
                (task["id"],),
            ).fetchone():
                raise Conflict("An open workspace handoff blocks new execution authority.")
            now, ident = utc_now(), "grant_" + uuid.uuid4().hex
            epoch = self._next_epoch(connection, task["id"])
            row = {
                "id": ident,
                "project_id": project_id,
                "task_id": task["id"],
                "task_revision": task["revision"],
                "plan_digest": task["current_plan_digest"],
                "worker_principal_id": worker["id"],
                "snapshot_id": payload["snapshot_id"],
                "snapshot_digest": expected_digest,
                "snapshot_release_id": release["id"],
                "snapshot_release_expires_at": _utc_string(release_expiry),
                "pinned_image": payload["pinned_image"],
                "currency": limits["currency"],
                "max_exposure_cents": payload["max_exposure_cents"],
                "acknowledges_unmeasured_infrastructure": 1,
                "status": "active",
                "revision": 1,
                "epoch": epoch,
                "expires_at": _utc_string(expiry),
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO execution_grants
                (id, project_id, task_id, task_revision, plan_digest, worker_principal_id,
                 snapshot_id, snapshot_digest, snapshot_release_id, snapshot_release_expires_at,
                 pinned_image,
                 currency, max_exposure_cents, acknowledges_unmeasured_infrastructure, status,
                 revision, epoch, expires_at, created_at, updated_at, created_by_principal_id)
                VALUES (:id, :project_id, :task_id, :task_revision, :plan_digest,
                :worker_principal_id, :snapshot_id, :snapshot_digest, :snapshot_release_id,
                :snapshot_release_expires_at, :pinned_image, :currency, :max_exposure_cents,
                :acknowledges_unmeasured_infrastructure, :status, :revision, :epoch,
                :expires_at, :created_at, :updated_at, :created_by_principal_id)""",
                row,
            )
            connection.executemany(
                """INSERT INTO execution_grant_skills (grant_id,digest,skill_release_id,expires_at)
                VALUES (?,?,?,?)""",
                (
                    (ident, skill_digest, release["id"], release["expires_at"])
                    for skill_digest, release in zip(
                        spec["pinned_skill_digests"], skill_releases, strict=True
                    )
                ),
            )
            connection.execute(
                """UPDATE execution_project_limits SET held_cents = held_cents + ?,
                updated_at = ? WHERE project_id = ?""",
                (payload["max_exposure_cents"], now, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                task["id"],
                "execution.grant_issued",
                1,
                {
                    "grant_id": ident,
                    "epoch": epoch,
                    "worker_principal_id": worker["id"],
                    "snapshot_release_id": release["id"],
                    "pinned_skill_digests": spec["pinned_skill_digests"],
                    "max_exposure_cents": payload["max_exposure_cents"],
                    "currency": limits["currency"],
                    "operational_only": True,
                },
            )
            return 201, self._grant_response(connection, row)

        return self._mutate(principal, key, "POST", path, digest, command)

    def get_project_execution(self, principal: dict[str, Any], project_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            return self._limit_response(self._limit(connection, actor, project_id))
        finally:
            connection.close()

    def get_execution_grant(self, principal: dict[str, Any], grant_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            return self._grant_response(connection, self._grant(connection, actor, grant_id))
        finally:
            connection.close()

    def list_execution_grants(
        self, principal: dict[str, Any], project_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM execution_grants WHERE project_id = ?
                ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [self._grant_response(connection, row) for row in rows]
        finally:
            connection.close()

    def revoke_execution_grant(
        self,
        principal: dict[str, Any],
        grant_id: str,
        key: str,
        digest: str,
        expected_revision: int,
    ) -> tuple[int, dict[str, Any]]:
        expected_revision = self._body(
            RevokeExecutionGrant, {"expected_revision": expected_revision}
        )["expected_revision"]
        path = f"/execution-grants/{grant_id}/revoke"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(actor)
            grant = self._grant(connection, actor, grant_id)
            if grant["revision"] != expected_revision:
                raise Conflict("Execution grant revision is no longer current.")
            if grant["status"] == "reconciled":
                raise Conflict("Reconciled execution authority cannot be revoked.")
            if grant["status"] == "cancel_requested":
                return 200, self._grant_response(connection, grant)
            updated, operation_ids = self._request_grant_cancellation(
                connection, grant, expected_revision
            )
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "execution.grant_cancel_requested",
                updated["revision"],
                {
                    "grant_id": grant_id,
                    "epoch": updated["epoch"],
                    "operation_ids": operation_ids,
                    "cancellation": self._cancellation_payload(connection, updated),
                    "operational_only": True,
                },
            )
            return 200, self._grant_response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command)

    def _active_worker_grant(
        self, connection: sqlite3.Connection, worker: dict[str, Any], grant_id: str
    ) -> sqlite3.Row:
        grant = self._grant(connection, worker, grant_id)
        if grant["worker_principal_id"] != worker["id"] or worker["kind"] != "worker":
            raise Forbidden("Worker is not assigned to this execution grant.")
        from .workers import WorkerRegistryStore

        WorkerRegistryStore.require_ready_worker(connection, worker, grant["project_id"])
        if grant["status"] != "active":
            raise Conflict("Execution grant is not active for lease authority.")
        now = self._now()
        try:
            expiry = _parse_utc(grant["expires_at"])
        except ValueError as error:
            raise Conflict("Execution grant expiry is invalid.") from error
        if expiry <= now:
            raise Conflict("Execution grant has expired and remains unreconciled.")
        task = self._task(connection, worker, grant["task_id"])
        if (
            task["revision"] != grant["task_revision"]
            or task["status"] != "approved"
            or task["current_plan_digest"] != grant["plan_digest"]
        ):
            raise Conflict("Execution grant task basis is no longer current.")
        issuer = self._live_principal(
            connection,
            {"id": grant["created_by_principal_id"], "org_id": worker["org_id"]},
        )
        if issuer["role"] != "human":
            raise Forbidden("Execution grant issuer is no longer eligible.")
        self._project(connection, issuer, grant["project_id"])
        limits = self._limit(connection, worker, grant["project_id"])
        if grant["currency"] != limits["currency"]:
            raise Conflict("Execution grant currency no longer matches project limits.")
        if limits["held_cents"] + limits["settled_cents"] > limits["monetary_limit_cents"]:
            raise Conflict("Project execution limit is exceeded pending reconciliation.")
        unresolved = connection.execute(
            """SELECT count(*) FROM execution_grants
            WHERE project_id = ? AND status IN ('active', 'cancel_requested')""",
            (grant["project_id"],),
        ).fetchone()[0]
        if unresolved > limits["max_concurrency"]:
            raise Conflict("Project execution concurrency is exceeded pending reconciliation.")
        release = self._authorize_snapshot(
            connection,
            worker,
            grant["snapshot_id"],
            worker["id"],
            grant["snapshot_digest"],
            grant["snapshot_release_id"],
        )
        try:
            release_expiry = _parse_utc(release["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise Conflict("Snapshot release expiry is invalid.") from error
        if release_expiry < expiry or release["id"] != grant["snapshot_release_id"]:
            raise Forbidden("Snapshot release no longer covers this grant.")
        from .skills import authorize_skill_release

        skill_rows = connection.execute(
            """SELECT digest, skill_release_id, expires_at FROM execution_grant_skills
            WHERE grant_id=?""",
            (grant["id"],),
        ).fetchall()
        revision = connection.execute(
            """SELECT execution_spec_json FROM task_revisions WHERE task_id=? AND revision=?
            AND plan_digest=?""",
            (grant["task_id"], grant["task_revision"] - 1, grant["plan_digest"]),
        ).fetchone()
        try:
            pinned = json.loads(revision["execution_spec_json"])["pinned_skill_digests"]
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise Conflict("Execution grant skill basis is invalid.") from error
        if len(skill_rows) != len(pinned):
            raise Conflict("Execution grant skill release basis is incomplete.")
        by_digest = {row["digest"]: row for row in skill_rows}
        if len(by_digest) != len(skill_rows) or set(by_digest) != set(pinned):
            raise Conflict("Execution grant skill release basis is invalid.")
        for skill_digest in pinned:
            frozen = by_digest[skill_digest]
            if _parse_utc(frozen["expires_at"]) < expiry:
                raise Forbidden("Skill release no longer covers this grant.")
            current = authorize_skill_release(
                connection,
                worker,
                grant["project_id"],
                skill_digest,
                worker["id"],
                frozen["skill_release_id"],
            )
            if _parse_utc(current["expires_at"]) < expiry:
                raise Forbidden("Skill release no longer covers this grant.")
        return grant

    def _guard_lease_replay(
        self, connection: sqlite3.Connection, worker: dict[str, Any], response: dict[str, Any]
    ) -> None:
        """Replay only still-current worker authority, never a cached lease."""
        lease_id = response.get("id")
        epoch = response.get("epoch")
        expires_at = response.get("expires_at")
        if (
            not isinstance(lease_id, str)
            or not isinstance(epoch, int)
            or not isinstance(expires_at, str)
        ):
            raise Conflict("Cached execution lease response is invalid.")
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id = ?", (lease_id,)
        ).fetchone()
        if (
            lease is None
            or lease["worker_principal_id"] != worker["id"]
            or lease["epoch"] != epoch
            or lease["expires_at"] != expires_at
            or _parse_utc(lease["expires_at"]) <= self._now()
        ):
            raise Conflict("Cached execution lease is no longer current.")
        grant = self._active_worker_grant(connection, worker, lease["grant_id"])
        if grant["epoch"] != lease["epoch"]:
            raise Conflict("Cached execution lease epoch is fenced.")

    def acquire_execution_lease(
        self,
        principal: dict[str, Any],
        grant_id: str,
        key: str,
        digest: str,
    ) -> tuple[int, dict[str, Any]]:
        grant_id = self._body(AcquireExecutionLease, {"grant_id": grant_id})["grant_id"]
        path = f"/execution-grants/{grant_id}/lease"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            grant = self._active_worker_grant(connection, worker, grant_id)
            now_datetime = self._now()
            prior = connection.execute(
                "SELECT 1 FROM execution_leases WHERE grant_id = ? LIMIT 1",
                (grant_id,),
            ).fetchone()
            if prior is not None:
                raise Conflict("Execution grant already has a lease and cannot be reacquired.")
            grant_expiry = _parse_utc(grant["expires_at"])
            credential_expiry = _parse_utc(worker["expires_at"])
            task = self._task(connection, worker, grant["task_id"])
            task_deadline = now_datetime + timedelta(seconds=task["max_duration_seconds"])
            hard_expires_at = min(grant_expiry, credential_expiry, task_deadline)
            expires_at = min(now_datetime + timedelta(seconds=LEASE_SECONDS), hard_expires_at)
            if expires_at <= now_datetime:
                raise Conflict("Worker authority expires before a lease can be issued.")
            epoch, now = self._next_epoch(connection, grant["task_id"]), utc_now()
            lease = {
                "id": "lease_" + uuid.uuid4().hex,
                "grant_id": grant_id,
                "worker_principal_id": worker["id"],
                "epoch": epoch,
                "issued_at": now,
                "expires_at": _utc_string(expires_at),
                "hard_expires_at": _utc_string(hard_expires_at),
                "renewed_at": now,
            }
            connection.execute(
                """INSERT INTO execution_leases
                (id, grant_id, worker_principal_id, epoch, issued_at, expires_at, hard_expires_at,
                 renewed_at)
                VALUES (:id, :grant_id, :worker_principal_id, :epoch, :issued_at,
                :expires_at, :hard_expires_at, :renewed_at)""",
                lease,
            )
            connection.execute(
                "UPDATE execution_grants SET epoch = ?, updated_at = ? WHERE id = ?",
                (epoch, now, grant_id),
            )
            self._event(
                connection,
                worker,
                grant["project_id"],
                grant["task_id"],
                "execution.lease_acquired",
                epoch,
                {
                    "grant_id": grant_id,
                    "lease_id": lease["id"],
                    "epoch": epoch,
                    "operational_only": True,
                },
            )
            return 201, self._lease_response(lease)

        return self._mutate(
            principal,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=self._guard_lease_replay,
        )

    def renew_execution_lease(
        self,
        principal: dict[str, Any],
        lease_id: str,
        key: str,
        digest: str,
        expected_epoch: int,
    ) -> tuple[int, dict[str, Any]]:
        renewal = self._body(
            RenewExecutionLease, {"lease_id": lease_id, "expected_epoch": expected_epoch}
        )
        lease_id, expected_epoch = renewal["lease_id"], renewal["expected_epoch"]
        path = f"/execution-leases/{lease_id}/renew"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if lease is None or lease["worker_principal_id"] != worker["id"]:
                raise NotFound("Execution lease was not found.")
            if lease["epoch"] != expected_epoch or _parse_utc(lease["expires_at"]) <= self._now():
                raise Conflict("Execution lease is no longer current.")
            grant = self._active_worker_grant(connection, worker, lease["grant_id"])
            if grant["epoch"] != expected_epoch:
                raise Conflict("Execution grant epoch is no longer current.")
            now_datetime = self._now()
            expires_at = min(
                now_datetime + timedelta(seconds=LEASE_SECONDS),
                _parse_utc(lease["hard_expires_at"]),
            )
            if expires_at <= now_datetime:
                raise Conflict("Worker authority expires before lease renewal.")
            now = utc_now()
            connection.execute(
                """UPDATE execution_leases SET expires_at = ?, renewed_at = ?
                WHERE id = ? AND epoch = ?""",
                (_utc_string(expires_at), now, lease_id, expected_epoch),
            )
            updated = dict(lease)
            updated.update(expires_at=_utc_string(expires_at), renewed_at=now)
            self._event(
                connection,
                worker,
                grant["project_id"],
                grant["task_id"],
                "execution.lease_renewed",
                expected_epoch,
                {
                    "grant_id": grant["id"],
                    "lease_id": lease_id,
                    "epoch": expected_epoch,
                    "operational_only": True,
                },
            )
            return 200, self._lease_response(updated)

        return self._mutate(
            principal,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=self._guard_lease_replay,
        )

    def report_execution_observation(
        self,
        principal: dict[str, Any],
        lease_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReportExecutionObservation, payload | {"lease_id": lease_id})
        lease_id = payload["lease_id"]
        path = f"/execution-leases/{lease_id}/observations"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if lease is None or lease["worker_principal_id"] != worker["id"]:
                raise NotFound("Execution lease was not found.")
            grant = self._grant(connection, worker, lease["grant_id"])
            if connection.execute(
                """SELECT 1 FROM workspace_handoffs WHERE grant_id = ?
                AND status = 'stopped_confirmed'""",
                (grant["id"],),
            ).fetchone():
                raise Conflict("Human workspace ownership fences further worker observations.")
            if (
                grant["worker_principal_id"] != worker["id"]
                or lease["epoch"] != payload["expected_epoch"]
            ):
                raise Conflict("Execution observation epoch is no longer current.")
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if operation is not None:
                expected_binding = {
                    "operation_id": payload.get("operation_id"),
                    "org_id": worker["org_id"],
                    "project_id": grant["project_id"],
                    "task_id": grant["task_id"],
                    "grant_id": grant["id"],
                    "lease_id": lease_id,
                    "worker_principal_id": worker["id"],
                    "epoch": lease["epoch"],
                }
                if any(operation[field] != value for field, value in expected_binding.items()):
                    raise Conflict("Execution observation does not match the external operation.")
                if (
                    operation["provider_id"] is not None
                    and payload.get("provider_id") is not None
                    and operation["provider_id"] != payload["provider_id"]
                ):
                    raise Conflict("Provider identity cannot change within an external operation.")
            elif payload.get("operation_id") is not None:
                raise Conflict(
                    "Execution observation operation is not journaled for this legacy lease."
                )
            if grant["status"] not in UNRESOLVED_GRANT_STATUSES:
                raise Conflict("Execution authority has already been reconciled.")
            if grant["status"] == "cancel_requested" and not (
                payload["observed_state"] == "exited" and payload["termination_confirmed"]
            ):
                raise Conflict(
                    "Revoked execution permits only confirmed termination reconciliation."
                )
            if payload["termination_confirmed"] and payload["observed_state"] != "exited":
                raise Conflict("Termination confirmation requires an exited observation.")
            if payload["termination_confirmed"] and payload.get("provider_id") is None:
                raise Conflict("Termination confirmation requires a provider identity.")
            if payload["termination_confirmed"] and operation is not None:
                receipt = connection.execute(
                    "SELECT * FROM external_operation_receipts WHERE operation_id=?",
                    (operation["operation_id"],),
                ).fetchone()
                if receipt is not None and (
                    receipt["provider_id"] != payload["provider_id"]
                    or receipt["observed_state"] != "exited"
                    or not receipt["termination_confirmed"]
                ):
                    raise Conflict(
                        "Termination confirmation requires a matching provider receipt observation."
                    )
            observed_at = _parse_utc(payload["observed_at"])
            issued_at = _parse_utc(lease["issued_at"])
            now_datetime = self._now()
            if observed_at < issued_at or observed_at > now_datetime + MAX_OBSERVATION_FUTURE_SKEW:
                raise Conflict("Execution observation timestamp is outside the lease window.")
            latest = connection.execute(
                """SELECT observed_at FROM execution_observations WHERE lease_id = ?
                ORDER BY rowid DESC LIMIT 1""",
                (lease_id,),
            ).fetchone()
            if latest is not None and observed_at < _parse_utc(latest["observed_at"]):
                raise Conflict("Execution observations cannot be reported out of order.")
            prior_provider = connection.execute(
                """SELECT provider_id FROM execution_observations
                WHERE lease_id = ? AND provider_id IS NOT NULL
                ORDER BY rowid DESC LIMIT 1""",
                (lease_id,),
            ).fetchone()
            if (
                prior_provider is not None
                and payload.get("provider_id") is not None
                and prior_provider["provider_id"] != payload["provider_id"]
            ):
                raise Conflict("Provider identity cannot change within an execution lease.")
            row = {
                "id": "observation_" + uuid.uuid4().hex,
                "grant_id": grant["id"],
                "lease_id": lease_id,
                "worker_principal_id": worker["id"],
                "epoch": lease["epoch"],
                "operation_id": operation["operation_id"] if operation is not None else None,
                "desired_state": "cancel_requested"
                if grant["status"] == "cancel_requested"
                else "admitted",
                "observed_state": payload["observed_state"],
                "observed_at": _utc_string(observed_at),
                "provider_id": payload.get("provider_id"),
                "termination_confirmed": int(payload["termination_confirmed"]),
                "recorded_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO execution_observations
                (id, grant_id, lease_id, worker_principal_id, epoch, operation_id, desired_state,
                 observed_state, observed_at, provider_id, termination_confirmed, recorded_at)
                VALUES (:id, :grant_id, :lease_id, :worker_principal_id, :epoch, :operation_id,
                :desired_state, :observed_state, :observed_at, :provider_id,
                :termination_confirmed, :recorded_at)""",
                row,
            )
            if operation is not None:
                connection.execute(
                    """UPDATE external_operations SET observed_state=?,
                    provider_id=coalesce(provider_id, ?), updated_at=? WHERE operation_id=?""",
                    (
                        row["observed_state"],
                        row["provider_id"],
                        row["recorded_at"],
                        operation["operation_id"],
                    ),
                )
                from .experiments import record_admitted_attempt_observation

                record_admitted_attempt_observation(
                    connection,
                    operation,
                    worker,
                    row["observed_state"],
                    row["desired_state"],
                    bool(row["termination_confirmed"]),
                )
            self._event(
                connection,
                worker,
                grant["project_id"],
                grant["task_id"],
                "execution.observation_reported",
                lease["epoch"],
                {
                    "grant_id": grant["id"],
                    "lease_id": lease_id,
                    "operation_id": row["operation_id"],
                    "epoch": lease["epoch"],
                    "desired_state": row["desired_state"],
                    "observed_state": row["observed_state"],
                    "termination_confirmed": bool(row["termination_confirmed"]),
                    "cancellation": self._cancellation_payload(connection, grant),
                    "observation_proof": "trusted_worker_assertion",
                    "operational_only": True,
                },
            )
            return 201, {
                **row,
                "termination_confirmed": bool(row["termination_confirmed"]),
                "observation_proof": "trusted_worker_assertion",
                "cancellation": self._cancellation_payload(connection, grant),
                "operational_only": True,
            }

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")

    def report_external_operation_receipt(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Persist fake-provider evidence after fresh worker/lease fencing.

        The receipt's provider identity and canonical payload are immutable.  A
        later lookup may advance only its attributed observed state; it cannot
        turn a worker assertion into cost, artifact, result, or scientific truth.
        """
        payload = self._body(
            ReportExternalOperationReceipt, payload | {"lease_id": payload.get("lease_id")}
        )
        path = f"/external-operations/{operation_id}/receipt"
        canonical_payload = json.dumps(
            payload["receipt_payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        if len(canonical_payload.encode()) > 65536:
            raise StoreError("Provider receipt payload exceeds its bound.")
        import hashlib

        if hashlib.sha256(canonical_payload.encode()).hexdigest() != payload["receipt_digest"]:
            raise Conflict("Provider receipt digest does not match its canonical payload.")

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if operation is None or operation["worker_principal_id"] != worker["id"]:
                raise NotFound("External operation was not found.")
            if (
                operation["org_id"] != worker["org_id"]
                or operation["lease_id"] != payload["lease_id"]
            ):
                raise Conflict("Provider receipt does not match the external operation.")
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
            ).fetchone()
            if lease is None or lease["epoch"] != payload["expected_epoch"]:
                raise Conflict("Provider receipt epoch is no longer current.")
            grant = self._grant(connection, worker, operation["grant_id"])
            post_revocation_reconciliation = grant["status"] == "cancel_requested"
            if (
                grant["project_id"] != operation["project_id"]
                or grant["task_id"] != operation["task_id"]
                or grant["worker_principal_id"] != worker["id"]
                or operation["epoch"] != lease["epoch"]
                or grant["status"] not in UNRESOLVED_GRANT_STATUSES
                or (not post_revocation_reconciliation and grant["epoch"] != lease["epoch"])
            ):
                raise Conflict("Provider receipt authority is no longer current.")
            if post_revocation_reconciliation and not (
                payload["observed_state"] == "exited" and payload["termination_confirmed"]
            ):
                raise Conflict(
                    "Revoked execution permits only confirmed termination reconciliation."
                )
            observed_at = _parse_utc(payload["observed_at"])
            if (
                observed_at < _parse_utc(lease["issued_at"])
                or observed_at > self._now() + MAX_OBSERVATION_FUTURE_SKEW
            ):
                raise Conflict("Provider receipt timestamp is outside the lease window.")
            existing = connection.execute(
                "SELECT * FROM external_operation_receipts WHERE operation_id=?", (operation_id,)
            ).fetchone()
            identity = {
                "provider_kind": payload["provider_kind"],
                "provider_version": payload["provider_version"],
                "provider_id": payload["provider_id"],
                "receipt_payload_json": canonical_payload,
                "receipt_digest": payload["receipt_digest"],
            }
            now = utc_now()
            if existing is None:
                row = identity | {
                    "operation_id": operation_id,
                    "observed_state": payload["observed_state"],
                    "observed_at": _utc_string(observed_at),
                    "termination_confirmed": int(payload["termination_confirmed"]),
                    "created_at": now,
                    "updated_at": now,
                }
                connection.execute(
                    """INSERT INTO external_operation_receipts
                    (operation_id, provider_kind, provider_version, provider_id,
                     receipt_payload_json, receipt_digest, observed_state, observed_at,
                     termination_confirmed, created_at, updated_at)
                    VALUES (:operation_id,:provider_kind,:provider_version,:provider_id,
                    :receipt_payload_json,:receipt_digest,:observed_state,:observed_at,
                    :termination_confirmed,:created_at,:updated_at)""",
                    row,
                )
            else:
                if any(existing[field] != value for field, value in identity.items()):
                    raise Conflict("Provider receipt immutable identity conflicts.")
                if observed_at < _parse_utc(existing["observed_at"]):
                    raise Conflict("Provider receipt observations cannot be reported out of order.")
                connection.execute(
                    """UPDATE external_operation_receipts SET observed_state=?, observed_at=?,
                    termination_confirmed=?, updated_at=? WHERE operation_id=?""",
                    (
                        payload["observed_state"],
                        _utc_string(observed_at),
                        int(payload["termination_confirmed"]),
                        now,
                        operation_id,
                    ),
                )
                row = dict(existing) | {
                    "observed_state": payload["observed_state"],
                    "observed_at": _utc_string(observed_at),
                    "termination_confirmed": int(payload["termination_confirmed"]),
                    "updated_at": now,
                }
            if operation["provider_id"] not in {None, payload["provider_id"]}:
                raise Conflict("Provider identity cannot change within an external operation.")
            connection.execute(
                """UPDATE external_operations SET provider_id=coalesce(provider_id, ?),
                observed_state=?, updated_at=? WHERE operation_id=?""",
                (payload["provider_id"], payload["observed_state"], now, operation_id),
            )
            from .experiments import record_admitted_attempt_observation

            record_admitted_attempt_observation(
                connection,
                operation,
                worker,
                payload["observed_state"],
                operation["desired_state"],
                bool(payload["termination_confirmed"]),
            )
            result = dict(row)
            result["termination_confirmed"] = bool(result["termination_confirmed"])
            result["receipt_evidence"] = "fake_provider_only"
            result["operational_only"] = True
            self._event(
                connection,
                worker,
                operation["project_id"],
                operation["task_id"],
                "execution.provider_receipt_reported",
                operation["epoch"],
                {
                    "operation_id": operation_id,
                    "provider_kind": "fake",
                    "observed_state": payload["observed_state"],
                    "termination_confirmed": bool(payload["termination_confirmed"]),
                    "receipt_evidence": "fake_provider_only",
                    "operational_only": True,
                },
            )
            return 201, result

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")

    def reconcile_execution_grant(
        self,
        principal: dict[str, Any],
        grant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReconcileExecutionGrant, payload)
        path = f"/execution-grants/{grant_id}/reconcile"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(actor)
            grant = self._grant(connection, actor, grant_id)
            if grant["revision"] != payload["expected_revision"]:
                raise Conflict("Execution grant revision is no longer current.")
            if grant["status"] == "reconciled":
                return 200, self._grant_response(connection, grant)
            leases = connection.execute(
                "SELECT count(*) FROM execution_leases WHERE grant_id = ?", (grant_id,)
            ).fetchone()[0]
            if leases:
                observation = connection.execute(
                    """SELECT observed_state, termination_confirmed FROM execution_observations
                    WHERE grant_id = ? ORDER BY rowid DESC LIMIT 1""",
                    (grant_id,),
                ).fetchone()
                if (
                    observation is None
                    or observation["observed_state"] != "exited"
                    or not observation["termination_confirmed"]
                ):
                    raise Conflict("Authenticated worker termination confirmation is required.")
            elif payload["settlement_cents"] != 0:
                raise Conflict("An unleased execution grant may reconcile only zero settlement.")
            limits = self._limit(connection, actor, grant["project_id"])
            if payload["settlement_cents"] > MAX_CENTS - limits["settled_cents"]:
                raise Conflict("Settlement exceeds the supported durable ledger range.")
            now = utc_now()
            next_revision = grant["revision"] + 1
            connection.execute(
                """UPDATE execution_grants SET status = 'reconciled', revision = ?, updated_at = ?,
                reconciled_at = ?, reconciled_by_principal_id = ?, settlement_cents = ?
                WHERE id = ? AND revision = ? AND status IN ('active', 'cancel_requested')""",
                (
                    next_revision,
                    now,
                    now,
                    actor["id"],
                    payload["settlement_cents"],
                    grant_id,
                    grant["revision"],
                ),
            )
            connection.execute(
                """UPDATE execution_project_limits SET held_cents = held_cents - ?,
                settled_cents = settled_cents + ?, updated_at = ? WHERE project_id = ?
                AND held_cents >= ?""",
                (
                    grant["max_exposure_cents"],
                    payload["settlement_cents"],
                    now,
                    grant["project_id"],
                    grant["max_exposure_cents"],
                ),
            )
            updated = dict(grant)
            updated.update(
                status="reconciled",
                revision=next_revision,
                updated_at=now,
                reconciled_at=now,
                reconciled_by_principal_id=actor["id"],
                settlement_cents=payload["settlement_cents"],
            )
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "execution.grant_reconciled",
                next_revision,
                {
                    "grant_id": grant_id,
                    "settlement_cents": payload["settlement_cents"],
                    "currency": limits["currency"],
                    "operational_only": True,
                },
            )
            return 200, self._grant_response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command)
