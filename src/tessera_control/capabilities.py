"""Transactional capability policy, admission intents and attributed effect audit.

No download, subprocess, secret resolution or runtime activation occurs here.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from .capability_models import (
    AdmitCapability,
    ApproveCapabilities,
    CapabilityReceipt,
    RequestCapabilities,
    RevokeCapabilities,
)
from .credential_leases import CredentialAccessLeaseStore
from .execution import ExecutionStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _parse_utc,
    canonical_digest,
    utc_now,
)


def create_capability_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS capability_policies (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        plan_revision INTEGER NOT NULL,
        plan_digest TEXT NOT NULL,
        declaration_json TEXT NOT NULL,
        declaration_digest TEXT NOT NULL,
        requested_by TEXT NOT NULL REFERENCES principals(id),
        approved_by TEXT REFERENCES principals(id),
        expires_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('requested','approved','revoked')),
        revision INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS capability_one_approval_per_plan
        ON capability_policies(task_id, plan_revision) WHERE approved_by IS NOT NULL""")
    connection.execute("""CREATE TABLE IF NOT EXISTS capability_operations (
        id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL REFERENCES capability_policies(id),
        action_id TEXT NOT NULL,
        worker_id TEXT NOT NULL REFERENCES principals(id),
        binding_json TEXT NOT NULL,
        deadline TEXT NOT NULL,
        receipt_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(policy_id, action_id)
    )""")


class CapabilityStore(ExecutionStore):
    @staticmethod
    def _body(model, payload):
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid capability request.") from error

    @staticmethod
    def _human(actor):
        if actor["role"] != "human":
            raise Forbidden("Only a human can approve or revoke capabilities.")

    def _policy(self, connection, actor, ident):
        row = connection.execute(
            "SELECT * FROM capability_policies WHERE id=? AND org_id=?", (ident, actor["org_id"])
        ).fetchone()
        if row is None:
            raise NotFound("Capability policy not found.")
        self._project(connection, actor, row["project_id"])
        return dict(row)

    @staticmethod
    def _response(row):
        result = dict(row)
        result["declaration"] = json.loads(result.pop("declaration_json"))
        result["execution_enabled"] = False
        return result

    def _capability_event(self, connection, actor, policy, event, payload):
        self._event(
            connection,
            actor,
            policy["project_id"],
            policy["task_id"],
            "capability." + event,
            policy["revision"],
            {"policy_id": policy["id"], **payload},
        )

    def _current_policy(self, connection, actor, policy):
        if canonical_digest(json.loads(policy["declaration_json"])) != policy["declaration_digest"]:
            raise Conflict("Capability declaration digest mismatch.")
        if policy["status"] != "approved":
            raise Conflict("Capability policy is not approved or is revoked.")
        if _parse_utc(policy["expires_at"]) <= datetime.now(UTC):
            raise Conflict("Capability policy expired.")
        task = self._task(connection, actor, policy["task_id"])
        if (
            task["status"] != "approved"
            or task["revision"] != policy["plan_revision"] + 1
            or task["current_plan_digest"] != policy["plan_digest"]
        ):
            raise Conflict("Capability plan basis is no longer current.")
        approver = self._live_principal(
            connection, {"id": policy["approved_by"], "org_id": actor["org_id"]}
        )
        self._human(approver)
        self._project(connection, approver, policy["project_id"])

    def request(self, actor, task_id, key, payload):
        payload = self._body(RequestCapabilities, payload)

        def command(connection, live):
            task = self._task(connection, live, task_id)
            basis = task["revision"] - (task["status"] == "approved")
            if (
                basis != payload["expected_plan_revision"]
                or task["current_plan_digest"] != payload["expected_plan_digest"]
            ):
                raise Conflict("Capability request requires the current immutable plan revision.")
            policy = {
                "id": "capability_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": task["project_id"],
                "task_id": task_id,
                "plan_revision": basis,
                "plan_digest": task["current_plan_digest"],
                "declaration_json": json.dumps(payload["declaration"], sort_keys=True),
                "declaration_digest": canonical_digest(payload["declaration"]),
                "requested_by": live["id"],
                "approved_by": None,
                "expires_at": None,
                "status": "requested",
                "revision": 1,
                "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO capability_policies VALUES
                (:id,:org_id,:project_id,:task_id,:plan_revision,:plan_digest,:declaration_json,
                 :declaration_digest,:requested_by,:approved_by,:expires_at,:status,:revision,:created_at)""",
                policy,
            )
            response = self._response(policy)
            self._capability_event(connection, live, policy, "requested", response)
            return 201, response

        def guard(connection, live, response):
            self._policy(connection, live, response["id"])

        return self._mutate(
            actor,
            key,
            "POST",
            f"/tasks/{task_id}/capabilities",
            canonical_digest(payload),
            command,
            replay_guard=guard,
        )

    def approve(self, actor, ident, key, payload):
        payload = self._body(ApproveCapabilities, payload)

        def command(connection, live):
            self._human(live)
            policy = self._policy(connection, live, ident)
            task = self._task(connection, live, policy["task_id"])
            if (
                policy["status"] != "requested"
                or policy["revision"] != payload["expected_revision"]
                or policy["declaration_digest"] != payload["declaration_digest"]
                or task["status"] != "approved"
                or task["revision"] != policy["plan_revision"] + 1
                or task["current_plan_digest"] != policy["plan_digest"]
            ):
                raise Conflict("Capability approval basis changed.")
            expiry = _parse_utc(payload["expires_at"])
            if not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(hours=24):
                raise Conflict("Capability approval requires expiry within 24 hours.")
            if connection.execute(
                "SELECT 1 FROM capability_policies WHERE task_id=? AND plan_revision=? "
                "AND approved_by IS NOT NULL",
                (policy["task_id"], policy["plan_revision"]),
            ).fetchone():
                raise Conflict(
                    "This plan already had capability authority; revise the plan to change it."
                )
            policy.update(
                status="approved",
                approved_by=live["id"],
                expires_at=payload["expires_at"],
                revision=2,
            )
            connection.execute(
                "UPDATE capability_policies SET status=:status,approved_by=:approved_by,"
                "expires_at=:expires_at,revision=:revision WHERE id=:id",
                policy,
            )
            self._capability_event(connection, live, policy, "approved", self._response(policy))
            return 200, self._response(policy)

        def guard(connection, live, response):
            self._human(live)
            self._current_policy(connection, live, self._policy(connection, live, ident))

        return self._mutate(
            actor,
            key,
            "POST",
            f"/capabilities/{ident}/approve",
            canonical_digest(payload),
            command,
            replay_guard=guard,
        )

    def revoke(self, actor, ident, key, payload):
        payload = self._body(RevokeCapabilities, payload)

        def command(connection, live):
            self._human(live)
            policy = self._policy(connection, live, ident)
            if policy["revision"] != payload["expected_revision"]:
                raise Conflict("Capability revision changed.")
            policy.update(status="revoked", revision=policy["revision"] + 1)
            connection.execute(
                "UPDATE capability_policies SET status='revoked',revision=? WHERE id=?",
                (policy["revision"], ident),
            )
            self._capability_event(
                connection, live, policy, "revoked", {"termination_confirmed": False}
            )
            return 200, self._response(policy)

        def guard(connection, live, response):
            self._human(live)
            self._policy(connection, live, ident)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/capabilities/{ident}/revoke",
            canonical_digest(payload),
            command,
            replay_guard=guard,
        )

    def _binding(self, connection, worker, policy, binding):
        self._current_policy(connection, worker, policy)
        lease = connection.execute(
            "SELECT lease.* FROM execution_leases lease "
            "JOIN execution_grants grant_record ON grant_record.id=lease.grant_id "
            "JOIN projects project ON project.id=grant_record.project_id "
            "WHERE lease.id=? AND project.org_id=?",
            (binding["execution_lease_id"], worker["org_id"]),
        ).fetchone()
        if lease is None or lease["worker_principal_id"] != worker["id"]:
            raise Forbidden("Execution lease is not assigned to this worker.")
        grant = self._active_worker_grant(connection, worker, lease["grant_id"])
        if (
            lease["epoch"] != binding["expected_epoch"]
            or grant["epoch"] != lease["epoch"]
            or grant["task_id"] != policy["task_id"]
            or grant["plan_digest"] != policy["plan_digest"]
            or _parse_utc(lease["expires_at"]) <= datetime.now(UTC)
        ):
            raise Conflict("Capability execution lease/epoch is fenced.")
        declaration = json.loads(policy["declaration_json"])
        action = binding["action"]
        if (
            binding["environment_id"] != declaration["environment_id"]
            or binding["image_digest"] != declaration["image_digest"]
            or not grant["pinned_image"].endswith("@" + declaration["image_digest"])
            or action not in declaration["actions"]
        ):
            raise Forbidden(
                "Destination, package, digest, root or environment violates capability policy."
            )
        access_id = binding["credential_access_lease_id"]
        expiry = min(
            _parse_utc(lease["expires_at"]),
            _parse_utc(grant["expires_at"]),
            _parse_utc(policy["expires_at"]),
        )
        if action["credential_ref"]:
            access_store = CredentialAccessLeaseStore(self.database_path.parent)
            access = access_store._record(connection, worker, access_id)
            access_store._current(connection, worker, access)
            if (
                access["execution_lease_id"] != lease["id"]
                or access["secret_ref"] != action["credential_ref"]
                or access["audience"] != action["url"]
                or access["purpose"] != "artifact-read"
                or set(json.loads(access["scope_json"]))
                != {"action:read", "sha256:" + action["sha256"]}
            ):
                raise Forbidden("Credential access does not match the exact artifact scope.")
            expiry = min(expiry, _parse_utc(access["expires_at"]))
        elif access_id is not None:
            raise Forbidden("Undeclared credentials are forbidden.")
        return expiry

    def admit(self, worker, ident, key, payload):
        payload = self._body(AdmitCapability, payload)

        def command(connection, live):
            policy = self._policy(connection, live, ident)
            self._capability_event(connection, live, policy, "operation_requested", payload)
            try:
                expiry = self._binding(connection, live, policy, payload)
            except (Forbidden, Conflict, NotFound) as error:
                self._capability_event(
                    connection,
                    live,
                    policy,
                    "operation_denied",
                    {"reason": str(error), "request_digest": canonical_digest(payload)},
                )
                return 403, {"detail": str(error)}
            existing = connection.execute(
                "SELECT id FROM capability_operations WHERE policy_id=? AND action_id=?",
                (ident, payload["action"]["id"]),
            ).fetchone()
            if existing:
                self._capability_event(
                    connection,
                    live,
                    policy,
                    "operation_denied",
                    {"reason": "action already reserved"},
                )
                return 409, {
                    "detail": "Action already reserved; reconcile it, never retry as new work."
                }
            operation_id = "capability_op_" + uuid.uuid4().hex
            deadline = min(
                expiry, datetime.now(UTC) + timedelta(seconds=payload["action"]["max_seconds"])
            ).isoformat()
            connection.execute(
                "INSERT INTO capability_operations VALUES (?,?,?,?,?,?,NULL,?)",
                (
                    operation_id,
                    ident,
                    payload["action"]["id"],
                    live["id"],
                    json.dumps(payload, sort_keys=True),
                    deadline,
                    utc_now(),
                ),
            )
            response = {
                "id": operation_id,
                "policy_id": ident,
                "binding": payload,
                "deadline": deadline,
                "execution_enabled": False,
                "state": "intent",
            }
            self._capability_event(connection, live, policy, "operation_admitted", response)
            return 201, response

        def guard(connection, live, response):
            if "id" in response:
                self._operation_current(connection, live, response["id"])

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/capabilities/{ident}/operations",
            canonical_digest(payload),
            command,
            audience="worker",
            replay_guard=guard,
        )

    def _operation(self, connection, worker, ident):
        operation = connection.execute(
            "SELECT operation.* FROM capability_operations operation "
            "JOIN capability_policies policy ON policy.id=operation.policy_id "
            "WHERE operation.id=? AND policy.org_id=?",
            (ident, worker["org_id"]),
        ).fetchone()
        if operation is None:
            raise NotFound("Capability operation not found.")
        policy = self._policy(connection, worker, operation["policy_id"])
        if operation["worker_id"] != worker["id"] or worker["kind"] != "worker":
            raise Forbidden("Capability operation belongs to another worker.")
        return operation, policy

    def _operation_current(self, connection, worker, ident):
        from .autonomy import require_session_authority

        require_session_authority(self, connection, worker, ident)
        operation, policy = self._operation(connection, worker, ident)
        expiry = self._binding(connection, worker, policy, json.loads(operation["binding_json"]))
        if operation["receipt_json"] is not None or _parse_utc(
            operation["deadline"]
        ) <= datetime.now(UTC):
            raise Conflict(
                "Capability operation completed or expired; no repeat effects permitted."
            )
        return operation, policy, min(expiry, _parse_utc(operation["deadline"]))

    def authorize(self, worker, ident):
        # Deliberately no cached idempotent authority response: every use is checked.
        def command(connection, live):
            operation, policy = self._operation(connection, live, ident)
            try:
                _, _, expiry = self._operation_current(connection, live, ident)
            except (Forbidden, Conflict, NotFound) as error:
                self._capability_event(
                    connection,
                    live,
                    policy,
                    "use_denied",
                    {"operation_id": ident, "reason": str(error)},
                )
                return 403, {"detail": str(error)}
            response = {
                "operation_id": ident,
                "binding": json.loads(operation["binding_json"]),
                "authority_until": min(
                    expiry, datetime.now(UTC) + timedelta(seconds=5)
                ).isoformat(),
                "execution_enabled": False,
            }
            self._capability_event(connection, live, policy, "use_checked", response)
            return 200, response

        return self._mutate(
            worker,
            uuid.uuid4().hex,
            "POST",
            f"/worker/capability-operations/{ident}/authorize",
            canonical_digest({}),
            command,
            audience="worker",
        )

    def receipt(self, worker, ident, key, payload):
        payload = self._body(CapabilityReceipt, payload)

        def command(connection, live):
            operation, policy = self._operation(connection, live, ident)
            if operation["receipt_json"] is not None:
                existing = json.loads(operation["receipt_json"])
                if existing["receipt"] == payload:
                    return 200, existing
                raise Conflict("An immutable capability receipt already exists.")
            reasons = []
            try:
                self._operation_current(connection, live, ident)
            except (Forbidden, Conflict, NotFound):
                reasons.append("stale_authority")
            expected = json.loads(operation["binding_json"])["action"]
            if payload["actual"] != expected:
                reasons.append("action_or_digest_mismatch")
            if (
                payload["bytes_downloaded"] > expected["max_bytes"]
                or payload["elapsed_ms"] > expected["max_seconds"] * 1000
            ):
                reasons.append("budget_exceeded")
            if payload["outcome"] != (
                "installed" if expected["kind"] == "package" else "downloaded"
            ):
                reasons.append("incomplete_effect")
            if payload["downloaded_sha256"] != expected["sha256"]:
                reasons.append("download_digest_mismatch")
            inventory = payload["installed_packages"]
            if expected["kind"] == "package":
                if inventory != [
                    {
                        "name": expected["name"],
                        "version": expected["version"],
                        "artifact_sha256": expected["sha256"],
                    }
                ]:
                    reasons.append("installed_inventory_mismatch")
                if payload["result_sha256"] != canonical_digest(inventory):
                    reasons.append("inventory_digest_mismatch")
            elif inventory:
                reasons.append("undeclared_install")
            if expected["kind"] != "package" and payload["result_sha256"] != expected["sha256"]:
                reasons.append("result_digest_mismatch")
            response = {
                "operation_id": ident,
                "receipt": payload,
                "assessment": "quarantined" if reasons else "reported",
                "reasons": reasons,
                "evidence": "worker_reported_not_independently_verified",
            }
            connection.execute(
                "UPDATE capability_operations SET receipt_json=? WHERE id=?",
                (json.dumps(response, sort_keys=True), ident),
            )
            self._capability_event(connection, live, policy, "effect_reported", response)
            # A mismatch disables all remaining actions, but never erases actual effects.
            if reasons and policy["status"] == "approved":
                connection.execute(
                    "UPDATE capability_policies SET status='revoked',revision=revision+1 "
                    "WHERE id=?",
                    (policy["id"],),
                )
                self._capability_event(
                    connection,
                    live,
                    {**policy, "revision": policy["revision"] + 1},
                    "automatically_revoked",
                    {"operation_id": ident, "reasons": reasons, "termination_confirmed": False},
                )
            return 201, response

        def guard(connection, live, response):
            # Historical evidence can replay after expiry, but never across a
            # changed tenant/project/worker identity scope.
            self._operation(connection, live, ident)

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/capability-operations/{ident}/receipt",
            canonical_digest(payload),
            command,
            audience="worker",
            replay_guard=guard,
        )

    def get(self, actor, ident):
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            policy = self._policy(connection, live, ident)
            operations = connection.execute(
                "SELECT * FROM capability_operations WHERE policy_id=? ORDER BY created_at,id",
                (ident,),
            ).fetchall()
            return {
                **self._response(policy),
                "operations": [
                    {
                        **dict(row),
                        "binding": json.loads(row["binding_json"]),
                        "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
                    }
                    for row in operations
                ],
            }
        finally:
            connection.close()
