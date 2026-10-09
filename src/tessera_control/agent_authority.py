"""Bounded human-issued authority consumed transactionally by enrolled agents.

Execution authority remains separately human-issued. No actor role, credential or
owner token is substituted, and consumption never invokes a provider itself.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from .agent_authority_models import (
    ConsumeAgentAuthority,
    DelegateAgentAuthority,
    IssueAgentAuthority,
    RequestAgentAuthority,
    RevokeAgentAuthority,
)
from .agent_provenance import record_agent_provenance
from .execution import ExecutionStore
from .experiments import ExperimentStore
from .external_agents import ExternalAgentStore
from .store import (
    _EVENT_LINEAGE,
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    canonical_digest,
    utc_now,
)


def create_agent_authority_schema(connection: sqlite3.Connection) -> None:
    for statement in (
        """CREATE TABLE IF NOT EXISTS agent_authority_requests (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        bounds_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS agent_authority_grants (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        subject_credential_id TEXT NOT NULL REFERENCES credentials(id),
        issuer_id TEXT NOT NULL REFERENCES principals(id),
        parent_id TEXT REFERENCES agent_authority_grants(id),
        request_id TEXT REFERENCES agent_authority_requests(id),
        bounds_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL CHECK(revision>=1), epoch INTEGER NOT NULL CHECK(epoch>=1),
        reserved_cents INTEGER NOT NULL CHECK(reserved_cents>=0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS agent_authority_consumptions (
        id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES agent_authority_grants(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id), action TEXT NOT NULL,
        execution_grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        attempt_id TEXT, operation_id TEXT, budget_cents INTEGER NOT NULL CHECK(budget_cents>=0),
        command_digest TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS agent_authority_grants_parent
        ON agent_authority_grants(parent_id)""",
        """CREATE INDEX IF NOT EXISTS agent_authority_consumptions_execution
        ON agent_authority_consumptions(execution_grant_id,action)""",
    ):
        connection.execute(statement)
    for table in ("agent_authority_requests", "agent_authority_consumptions"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} "
                f"ON {table} BEGIN SELECT RAISE(ABORT,'agent authority history is immutable'); END"
            )


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def require_active_agent_lineage(connection, grant_id):
    """Recheck ancestry, issuer and subject identities inside an existing transaction."""
    seen = set()
    first = None
    while grant_id is not None:
        if grant_id in seen or len(seen) >= 9:
            raise Forbidden("Agent authority lineage is invalid.")
        seen.add(grant_id)
        row = connection.execute(
            "SELECT * FROM agent_authority_grants WHERE id=?", (grant_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            raise Forbidden("Agent authority or ancestor was revoked.")
        bounds = json.loads(row["bounds_json"])
        if _time(bounds["expires_at"]) <= datetime.now(UTC):
            raise Forbidden("Agent authority or ancestor expired.")
        recipient = connection.execute(
            "SELECT principal_id FROM credentials WHERE id=?", (row["subject_credential_id"],)
        ).fetchone()
        if recipient is None:
            raise Forbidden("Agent authority subject is unavailable.")
        subject = Store._live_principal(
            connection, {"id": recipient[0], "org_id": row["org_id"]}, external_allowed=True
        )
        if subject["credential_id"] != row["subject_credential_id"]:
            raise Forbidden("Agent authority subject changed.")
        issuer = Store._live_principal(
            connection, {"id": row["issuer_id"], "org_id": row["org_id"]}, external_allowed=True
        )
        if row["parent_id"] is None and issuer["role"] != "human":
            raise Forbidden("Root authority requires an eligible human issuer.")
        first = first or dict(row)
        grant_id = row["parent_id"]
    return first


def require_execution_agent_authority(connection, execution_grant_id):
    """Existing worker/package admission also fences consumed agent authority."""
    rows = connection.execute(
        "SELECT DISTINCT grant_id FROM agent_authority_consumptions "
        "WHERE execution_grant_id=? AND action IN ('attempt:admit','attempt:dispatch')",
        (execution_grant_id,),
    ).fetchall()
    for row in rows:
        require_active_agent_lineage(connection, row[0])


class AgentAuthorityStore(ExternalAgentStore):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.experiments = ExperimentStore(state_dir)
        self.execution = ExecutionStore(state_dir)

    @staticmethod
    def _body(model, payload):
        return model.model_validate(payload).model_dump(mode="json")

    def _recipient_bounds(self, connection, actor, project_id, bounds):
        self._project(connection, actor, project_id)
        recipient = connection.execute(
            """SELECT p.id,p.org_id FROM credentials c JOIN principals p ON p.id=c.principal_id
            WHERE c.id=? AND c.project_id=? AND p.org_id=?""",
            (bounds["subject_credential_id"], project_id, actor["org_id"]),
        ).fetchone()
        if recipient is None:
            raise NotFound("Authority recipient was not found.")
        subject = self._live_principal(connection, dict(recipient))
        subject["agent_destination"] = bounds["destination"]
        self._scope(connection, subject, "authority:consume")
        expiry = _time(bounds["expires_at"])
        if not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(hours=24):
            raise Conflict("Authority expiry must be within the next 24 hours.")
        if expiry > _time(subject["expires_at"]):
            raise Forbidden("Authority cannot outlive its subject credential.")
        return subject

    def _validate_binding(self, connection, actor, project_id, bounds, binding, *, active=True):
        variant = self.experiments._variant(connection, actor, binding["variant_id"])
        experiment = self.experiments._experiment(connection, actor, variant["experiment_id"])
        task = self._task(connection, actor, binding["task_id"])
        grant = self.execution._grant(connection, actor, binding["execution_grant_id"])
        if experiment["project_id"] != project_id or task["project_id"] != project_id:
            raise NotFound("Authority resource was not found.")
        if (
            variant["revision"] != binding["expected_variant_revision"]
            or variant["design_revision"] != binding["expected_design_revision"]
            or experiment["current_design_revision"] != binding["expected_design_revision"]
            or variant["execution_task_id"] != task["id"]
            or variant["execution_plan_digest"] != binding["plan_digest"]
            or task["revision"] != binding["expected_task_revision"]
            or task["current_plan_digest"] != binding["plan_digest"]
            or grant["task_id"] != task["id"]
            or grant["plan_digest"] != binding["plan_digest"]
            or grant["task_revision"] != task["revision"]
        ):
            raise Conflict("Authority resource revision or digest changed.")
        if active and (
            grant["status"] != "active"
            or grant["revision"] != binding["expected_execution_grant_revision"]
            or grant["epoch"] != binding["expected_execution_epoch"]
            or _time(grant["expires_at"]) <= datetime.now(UTC)
        ):
            raise Conflict("Execution authority revision, epoch or lifetime changed.")
        if task["max_duration_seconds"] > bounds["max_duration_seconds"]:
            raise Forbidden("Execution duration exceeds agent authority.")
        # No running grant/lease can outlive the controlling agent authority.
        if {"attempt:admit", "attempt:dispatch"} & set(bounds["actions"]):
            if _time(grant["expires_at"]) > _time(bounds["expires_at"]):
                raise Forbidden("Execution lifetime exceeds agent authority expiry.")
        return grant

    @staticmethod
    def _response(row):
        result = dict(row)
        bounds = json.loads(result.pop("bounds_json"))
        return {
            **result,
            "bounds": bounds,
            "remaining_budget_cents": bounds["budget_cents"] - row["reserved_cents"],
        }

    def _grant_row(self, connection, actor, grant_id, *, subject=False):
        row = connection.execute(
            "SELECT * FROM agent_authority_grants WHERE id=? AND org_id=?",
            (grant_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Agent authority grant was not found.")
        self._project(connection, actor, row["project_id"])
        if subject or actor["role"] != "human":
            self._scope(connection, actor, "authority:consume")
            if row["subject_credential_id"] != actor["credential_id"]:
                raise NotFound("Agent authority grant was not found.")
        return dict(row)

    @staticmethod
    def _fence(row, body):
        if row["revision"] != body["expected_revision"] or row["epoch"] != body["expected_epoch"]:
            raise Conflict("Agent authority revision or epoch changed.")

    def _provenance(
        self, connection, actor, project_id, ident, kind, outcome, references, model=None
    ):
        lineage = _EVENT_LINEAGE.get()
        record_agent_provenance(
            connection,
            actor,
            project_id,
            {
                "record_id": ident,
                "kind": kind,
                "outcome": outcome,
                "correlation_id": lineage[0] if lineage else ident,
                "causation_id": lineage[1] if lineage else None,
                "model_id": model,
                "references": references,
            },
        )

    def request_authority(self, principal, project_id, key, payload):
        body = self._body(RequestAgentAuthority, payload)

        def command(connection, actor):
            self._scope(connection, actor, "authority:request")
            self._project(connection, actor, project_id)
            if body["bounds"]["subject_credential_id"] != actor["credential_id"]:
                raise Forbidden("Agent requests must name their own subject credential.")
            self._recipient_bounds(connection, actor, project_id, body["bounds"])
            ident = "agent_authority_request_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO agent_authority_requests VALUES (?,?,?,?,?,?,?)",
                (
                    ident,
                    actor["org_id"],
                    project_id,
                    actor["credential_id"],
                    actor["id"],
                    json.dumps(body["bounds"]),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                None,
                "agent_authority.requested",
                1,
                {"request_id": ident},
            )
            self._provenance(
                connection,
                actor,
                project_id,
                ident,
                "permission",
                "requested",
                {"permission_id": ident},
            )
            return 201, {"id": ident, "project_id": project_id, **body, "authority_effect": "none"}

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/agent-authority-requests",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._scope(c, a, "authority:request"),
        )

    def list_requests(self, principal, project_id, offset=0, limit=100):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid request pagination.")
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            self._project(connection, actor, project_id)
            credential = None
            if actor["role"] != "human":
                self._scope(connection, actor, "authority:request")
                credential = actor["credential_id"]
            return [
                {
                    "id": r["id"],
                    "project_id": project_id,
                    "bounds": json.loads(r["bounds_json"]),
                    "authority_effect": "none",
                }
                for r in connection.execute(
                    "SELECT * FROM agent_authority_requests WHERE project_id=? "
                    "AND (? IS NULL OR credential_id=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                    (project_id, credential, credential, limit, offset),
                )
            ]
        finally:
            connection.close()

    def _insert_grant(
        self, connection, actor, project_id, bounds, *, parent_id=None, request_id=None
    ):
        row = {
            "id": "agent_authority_" + uuid.uuid4().hex,
            "org_id": actor["org_id"],
            "project_id": project_id,
            "subject_credential_id": bounds["subject_credential_id"],
            "issuer_id": actor["id"],
            "parent_id": parent_id,
            "request_id": request_id,
            "bounds_json": json.dumps(bounds),
            "status": "active",
            "revision": 1,
            "epoch": 1,
            "reserved_cents": 0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        connection.execute(
            """INSERT INTO agent_authority_grants VALUES
        (:id,:org_id,:project_id,:subject_credential_id,:issuer_id,:parent_id,:request_id,
        :bounds_json,:status,:revision,:epoch,:reserved_cents,:created_at,:updated_at)""",
            row,
        )
        event = "delegated" if parent_id else "issued"
        self._event(
            connection,
            actor,
            project_id,
            None,
            f"agent_authority.{event}",
            1,
            {"grant_id": row["id"], "parent_grant_id": parent_id, "request_id": request_id},
        )
        self._provenance(
            connection,
            actor,
            project_id,
            row["id"],
            "delegation" if parent_id else "permission",
            "succeeded",
            {"permission_id": row["id"], "delegation_id": row["id"] if parent_id else None},
        )
        return 201, self._response(row)

    def issue_grant(self, principal, project_id, key, payload):
        body = self._body(IssueAgentAuthority, payload)

        def command(connection, actor):
            self._human(actor)
            bounds = body["bounds"]
            self._recipient_bounds(connection, actor, project_id, bounds)
            for binding in bounds["bindings"]:
                self._validate_binding(connection, actor, project_id, bounds, binding)
            if body["request_id"]:
                request = connection.execute(
                    "SELECT * FROM agent_authority_requests "
                    "WHERE id=? AND org_id=? AND project_id=?",
                    (body["request_id"], actor["org_id"], project_id),
                ).fetchone()
                if request is None or request["credential_id"] != bounds["subject_credential_id"]:
                    raise NotFound("Authority request was not found.")
            return self._insert_grant(
                connection, actor, project_id, bounds, request_id=body["request_id"]
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/agent-authority-grants",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: (
                self._human(a),
                self._grant_row(c, a, r["id"]),
                require_active_agent_lineage(c, r["id"]),
            ),
        )

    def get_grant(self, principal, grant_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            row = self._grant_row(connection, actor, grant_id)
            result = self._response(row)
            try:
                require_active_agent_lineage(connection, grant_id)
                result["effective_status"] = "active"
            except Forbidden:
                result["effective_status"] = "fenced"
            return result
        finally:
            connection.close()

    def revoke_grant(self, principal, grant_id, key, payload):
        body = self._body(RevokeAgentAuthority, payload)

        def command(connection, actor):
            self._human(actor)
            row = self._grant_row(connection, actor, grant_id)
            self._fence(row, body)
            connection.execute(
                "UPDATE agent_authority_grants SET status='revoked',revision=revision+1,"
                "epoch=epoch+1,updated_at=? WHERE id=?",
                (utc_now(), grant_id),
            )
            # Fence real queued/running execution, but never claim provider termination.
            consumed = connection.execute(
                """WITH RECURSIVE lineage(id) AS (
                SELECT ? UNION ALL SELECT g.id FROM agent_authority_grants g
                JOIN lineage l ON g.parent_id=l.id)
                SELECT DISTINCT c.execution_grant_id FROM agent_authority_consumptions c
                JOIN lineage l ON c.grant_id=l.id
                WHERE c.action IN ('attempt:admit','attempt:dispatch')""",
                (grant_id,),
            ).fetchall()
            for consumed_row in consumed:
                execution = self.execution._grant(connection, actor, consumed_row[0])
                if execution["status"] == "active":
                    self.execution._cancel_grant_transaction(
                        connection, actor, execution["id"], execution["revision"]
                    )
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                "agent_authority.revoked",
                row["revision"] + 1,
                {"grant_id": grant_id},
            )
            self._provenance(
                connection,
                actor,
                row["project_id"],
                grant_id + "_revoke_" + str(row["revision"] + 1),
                "permission",
                "canceled",
                {"permission_id": grant_id},
            )
            return 200, self._response(
                connection.execute(
                    "SELECT * FROM agent_authority_grants WHERE id=?", (grant_id,)
                ).fetchone()
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/agent-authority-grants/{grant_id}/revoke",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._human(a),
        )

    def delegate_grant(self, principal, grant_id, key, payload):
        body = self._body(DelegateAgentAuthority, payload)

        def command(connection, actor):
            row = self._grant_row(connection, actor, grant_id, subject=True)
            self._fence(row, body)
            require_active_agent_lineage(connection, grant_id)
            parent, child = json.loads(row["bounds_json"]), body["bounds"]
            if "authority:delegate" not in parent["actions"] or parent["delegation_depth"] <= 0:
                raise Forbidden("Agent authority does not permit delegation.")
            self._recipient_bounds(connection, actor, row["project_id"], child)
            if (
                not set(child["actions"]) <= set(parent["actions"])
                or child["destination"] != parent["destination"]
                or child["model"] != parent["model"]
                or child["delegation_depth"] >= parent["delegation_depth"]
                or child["max_duration_seconds"] > parent["max_duration_seconds"]
                or _time(child["expires_at"]) > _time(parent["expires_at"])
                or any(b not in parent["bindings"] for b in child["bindings"])
                or child["budget_cents"] > parent["budget_cents"] - row["reserved_cents"]
            ):
                raise Forbidden(
                    "Delegation must be a resource, action, budget and lifetime subset."
                )
            for binding in child["bindings"]:
                self._validate_binding(connection, actor, row["project_id"], child, binding)
            connection.execute(
                "UPDATE agent_authority_grants SET reserved_cents=reserved_cents+?,"
                "updated_at=? WHERE id=?",
                (child["budget_cents"], utc_now(), grant_id),
            )
            return self._insert_grant(
                connection, actor, row["project_id"], child, parent_id=grant_id
            )

        def replay(connection, actor, response):
            self._grant_row(connection, actor, grant_id, subject=True)
            require_active_agent_lineage(connection, response["id"])

        return self._mutate(
            principal,
            key,
            "POST",
            f"/agent-authority-grants/{grant_id}/delegate",
            canonical_digest(body),
            command,
            replay_guard=replay,
        )

    def _consumption_basis(self, connection, actor, grant_id, body, *, replay=False):
        row = self._grant_row(connection, actor, grant_id, subject=True)
        self._fence(row, body)
        require_active_agent_lineage(connection, grant_id)
        bounds, command = json.loads(row["bounds_json"]), body["command"]
        if bounds["destination"] != actor["agent_destination"] or bounds["model"] != body["model"]:
            raise Forbidden("Agent destination or model is outside authority.")
        if command["action"] not in bounds["actions"]:
            raise Forbidden("Agent action is outside authority.")
        attempt = None
        if "attempt_id" in command:
            attempt = connection.execute(
                "SELECT * FROM research_experiment_admitted_attempts WHERE id=?",
                (command["attempt_id"],),
            ).fetchone()
            if attempt is None:
                raise NotFound("Admitted attempt was not found.")
            execution_id = attempt["grant_id"]
        else:
            execution_id = command["execution_grant_id"]
        binding = next(
            (b for b in bounds["bindings"] if b["execution_grant_id"] == execution_id), None
        )
        if binding is None or (attempt and attempt["variant_id"] != binding["variant_id"]):
            raise NotFound("Execution resource is outside authority.")
        grant = self._validate_binding(
            connection,
            actor,
            row["project_id"],
            bounds,
            binding,
            active=command["action"] != "attempt:status"
            and not (replay and command["action"] == "execution:cancel"),
        )
        if (
            replay
            and command["action"] == "execution:cancel"
            and (
                grant["status"] != "cancel_requested"
                or grant["revision"] != command["expected_revision"] + 1
            )
        ):
            raise Conflict("Cancelled execution basis is no longer current.")
        if command["action"] == "attempt:admit" and (
            command["variant_id"] != binding["variant_id"]
            or command["expected_variant_revision"] != binding["expected_variant_revision"]
            or command["expected_design_revision"] != binding["expected_design_revision"]
        ):
            raise Conflict("Admission is outside the approved variant revision.")
        if (
            command["action"] == "attempt:dispatch"
            and command["target_digest"] != binding["target_digest"]
        ):
            raise Forbidden("Dispatch target is outside authority.")
        return row, bounds, command, grant, attempt

    def consume(self, principal, grant_id, key, payload):
        body = self._body(ConsumeAgentAuthority, payload)

        def command(connection, actor):
            row, bounds, call, grant, attempt = self._consumption_basis(
                connection, actor, grant_id, body
            )
            charge = grant["max_exposure_cents"] if call["action"] == "attempt:admit" else 0
            if charge > bounds["budget_cents"] - row["reserved_cents"]:
                raise Forbidden("Agent authority budget would be exceeded.")
            if call["action"] == "attempt:admit":
                status, result = self.experiments._admit_attempt_transaction(
                    connection,
                    actor,
                    call["variant_id"],
                    {
                        "expected_variant_revision": call["expected_variant_revision"],
                        "expected_design_revision": call["expected_design_revision"],
                        "grant_id": grant["id"],
                    },
                )
                attempt_id = result["id"]
            elif call["action"] == "attempt:dispatch":
                dispatch = self.experiments._dispatch_admitted_attempt_transaction
                if call["profile_version"] == "ordinary-docker-v1":
                    from .ordinary_docker import OrdinaryDockerStore

                    dispatch = OrdinaryDockerStore(self.database_path.parent).dispatch_transaction
                status, result = dispatch(
                    connection,
                    actor,
                    call["attempt_id"],
                    {
                        "expected_revision": call["expected_revision"],
                        "target_digest": call["target_digest"],
                        "profile_version": call["profile_version"],
                    },
                )
                attempt_id = attempt["id"]
            elif call["action"] == "execution:cancel":
                status, result = self.execution._cancel_grant_transaction(
                    connection, actor, grant["id"], call["expected_revision"]
                )
                attempt_id = None
            else:
                status, result, attempt_id = 200, dict(attempt), attempt["id"]
            ident = "agent_consumption_" + uuid.uuid4().hex
            operation_id = result.get("operation_id")
            connection.execute(
                "INSERT INTO agent_authority_consumptions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    grant_id,
                    actor["credential_id"],
                    call["action"],
                    grant["id"],
                    attempt_id,
                    operation_id,
                    charge,
                    canonical_digest(body),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE agent_authority_grants SET reserved_cents=reserved_cents+?,"
                "updated_at=? WHERE id=?",
                (charge, utc_now(), grant_id),
            )
            self._event(
                connection,
                actor,
                row["project_id"],
                grant["task_id"],
                "agent_authority.consumed",
                row["revision"],
                {
                    "grant_id": grant_id,
                    "execution_grant_id": grant["id"],
                    "consumption_id": ident,
                    "action": call["action"],
                    "admitted_attempt_id": attempt_id,
                    "operation_id": operation_id,
                    "budget_cents": charge,
                },
            )
            self._provenance(
                connection,
                actor,
                row["project_id"],
                ident,
                "permission",
                "succeeded",
                {
                    "permission_id": grant_id,
                    "grant_id": grant["id"],
                    "consumption_id": ident,
                    "attempt_id": attempt_id,
                    "operation_id": operation_id,
                    "task_id": grant["task_id"],
                },
                model=body["model"],
            )
            return status, {
                "consumption_id": ident,
                "authority_grant_id": grant_id,
                "action": call["action"],
                "result": result,
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/agent-authority-grants/{grant_id}/consume",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._consumption_basis(c, a, grant_id, body, replay=True),
        )
