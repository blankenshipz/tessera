"""Project/destination scoped external proposals and explicit disclosure records."""

import base64
import json
import sqlite3
import uuid

from .artifact_custody import ArtifactCustodyStore
from .external_agent_models import CreateAgentProposal
from .migrations import plan_digest
from .store import Conflict, Forbidden, NotFound, Store, _connect, canonical_digest, utc_now


class WithdrawnAgentContext(Forbidden):
    """A formerly disclosed artifact is no longer available to this recipient."""


def create_external_agent_schema(connection: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS worker_credential_renewals (
        id TEXT PRIMARY KEY, credential_id TEXT NOT NULL REFERENCES credentials(id),
        renewed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        renewed_by TEXT NOT NULL REFERENCES principals(id))""",
        """CREATE TABLE IF NOT EXISTS external_agent_scopes (
        credential_id TEXT PRIMARY KEY REFERENCES credentials(id),
        actions_json TEXT NOT NULL, destination TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS external_agent_proposals (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        submitted_by TEXT NOT NULL REFERENCES principals(id), destination TEXT NOT NULL,
        digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS external_agent_promotions (
        proposal_id TEXT PRIMARY KEY REFERENCES external_agent_proposals(id),
        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
        promoted_by TEXT NOT NULL REFERENCES principals(id), created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS external_agent_contexts (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id), destination TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL)""",
    ]
    for statement in statements:
        connection.execute(statement)
    for table in ("external_agent_proposals", "external_agent_contexts"):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_by_project "
            f"ON {table}(project_id,credential_id,created_at,id)"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS worker_credential_renewals_by_credential "
        "ON worker_credential_renewals(credential_id,renewed_at,id)"
    )
    for table in (
        "worker_credential_renewals",
        "external_agent_scopes",
        "external_agent_proposals",
        "external_agent_promotions",
        "external_agent_contexts",
    ):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, "
                "'external agent records are immutable'); END"
            )


class ExternalAgentStore(ArtifactCustodyStore):
    @staticmethod
    def _live_principal(connection, principal, *, audience="control"):
        return Store._live_principal(
            connection, principal, audience=audience, external_allowed=True
        )

    @staticmethod
    def _scope(connection, actor, action):
        scope = connection.execute(
            "SELECT * FROM external_agent_scopes WHERE credential_id=?", (actor["credential_id"],)
        ).fetchone()
        if actor["role"] != "agent" or scope is None:
            raise Forbidden("An enrolled external-agent credential is required.")
        if actor.get("agent_destination") != scope["destination"]:
            raise Forbidden("External-agent destination does not match enrollment.")
        if action is not None and action not in json.loads(scope["actions_json"]):
            raise Forbidden("External-agent action is outside enrolled scope.")
        return dict(scope)

    def _actor(self, connection, principal):
        actor = self._live_principal(connection, principal)
        actor["agent_destination"] = principal.get("agent_destination")
        return actor

    # _mutate rehydrates the actor; preserve only the untrusted recipient claim,
    # which _scope compares with immutable server enrollment on every command/replay.
    def _mutate(self, principal, key, method, path, digest, command, **kwargs):
        def wrapped(connection, actor):
            actor["agent_destination"] = principal.get("agent_destination")
            return command(connection, actor)

        guard = kwargs.pop("replay_guard", None)

        def replay(connection, actor, response):
            actor["agent_destination"] = principal.get("agent_destination")
            if guard:
                guard(connection, actor, response)

        return super()._mutate(
            principal, key, method, path, digest, wrapped, replay_guard=replay, **kwargs
        )

    def status(self, principal):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            scope = self._scope(connection, actor, None)
            return {
                **{k: actor[k] for k in ("id", "credential_id", "project_id", "expires_at")},
                "actions": json.loads(scope["actions_json"]),
                "destination": scope["destination"],
                "provenance": "external_agent_caller_attested",
            }
        finally:
            connection.close()

    def _proposal(self, connection, actor, proposal_id):
        row = connection.execute(
            "SELECT * FROM external_agent_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise NotFound("Agent proposal was not found.")
        self._project(connection, actor, row["project_id"])
        if actor["role"] != "human":
            self._scope(connection, actor, "proposal:read")
            if row["credential_id"] != actor["credential_id"]:
                raise NotFound("Agent proposal was not found.")
        promotion = connection.execute(
            "SELECT * FROM external_agent_promotions WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        result = {
            **dict(row),
            **json.loads(row["payload_json"]),
            "revision": 1,
            "provenance_kind": "external_agent_caller_attested",
            "authority_effect": "none",
            "promotion": dict(promotion) if promotion else None,
        }
        result.pop("payload_json")
        return result

    def create_proposal(self, principal, project_id, key, payload):
        payload = CreateAgentProposal.model_validate(payload).model_dump(mode="json")

        def command(connection, actor):
            scope = self._scope(connection, actor, "proposal:create")
            self._project(connection, actor, project_id)
            proposal_id = "agent_proposal_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO external_agent_proposals VALUES (?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    project_id,
                    actor["credential_id"],
                    actor["id"],
                    scope["destination"],
                    canonical_digest(payload),
                    json.dumps(payload),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                None,
                "external_agent.proposal_created",
                1,
                {"proposal_id": proposal_id, "authority_effect": "none"},
            )
            # Creation does not imply proposal:read authority for subsequent reads.
            row = connection.execute(
                "SELECT * FROM external_agent_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            result = {
                **dict(row),
                **payload,
                "revision": 1,
                "authority_effect": "none",
                "provenance_kind": "external_agent_caller_attested",
                "promotion": None,
            }
            result.pop("payload_json")
            return 201, result

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/agent-proposals",
            canonical_digest(payload),
            command,
            replay_guard=lambda c, a, _: self._scope(c, a, "proposal:create"),
        )

    def get_proposal(self, principal, proposal_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            return self._proposal(connection, self._actor(connection, principal), proposal_id)
        finally:
            connection.close()

    def list_records(self, principal, project_id, kind, offset, limit):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            self._project(connection, actor, project_id)
            suffix, params = "", [project_id]
            if actor["role"] != "human":
                scope = self._scope(
                    connection, actor, "proposal:read" if kind == "proposals" else "context:read"
                )
                suffix = " AND credential_id=? AND destination=?"
                params.extend([actor["credential_id"], scope["destination"]])
            rows = connection.execute(
                f"SELECT id FROM external_agent_{kind} WHERE project_id=?"
                + suffix
                + " ORDER BY created_at,id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            getter = self._proposal if kind == "proposals" else self._context
            records = []
            for row in rows:
                try:
                    records.append(getter(connection, actor, row["id"]))
                except WithdrawnAgentContext:
                    # Preserve immutable disclosure history, not withdrawn bytes.
                    # Other permission, integrity and storage failures stay visible.
                    continue
            return records
        finally:
            connection.close()

    def promote(self, principal, proposal_id, key, payload):
        def command(connection, actor):
            self._human(actor)
            proposal = self._proposal(connection, actor, proposal_id)
            if (
                payload["expected_revision"] != 1
                or payload["expected_digest"] != proposal["digest"]
            ):
                raise Conflict("Agent proposal revision or digest is no longer current.")
            if proposal["promotion"]:
                return 200, proposal["promotion"]
            plan = proposal["plan"]
            task_id, now = "task_" + uuid.uuid4().hex, utc_now()
            digest = plan_digest(
                plan["title"],
                plan["purpose"],
                plan["budget_cents"],
                plan["max_duration_seconds"],
                plan["execution_spec"],
            )
            connection.execute(
                """INSERT INTO tasks (id,project_id,title,purpose,budget_cents,max_duration_seconds,
                status,revision,current_plan_digest,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'draft',1,?,?,?)""",
                (
                    task_id,
                    proposal["project_id"],
                    plan["title"],
                    plan["purpose"],
                    plan["budget_cents"],
                    plan["max_duration_seconds"],
                    digest,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO task_revisions (task_id,revision,title,purpose,budget_cents,
                max_duration_seconds,execution_spec_json,plan_digest,created_at,actor_principal_id)
                VALUES (?,1,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    plan["title"],
                    plan["purpose"],
                    plan["budget_cents"],
                    plan["max_duration_seconds"],
                    json.dumps(plan["execution_spec"]),
                    digest,
                    now,
                    actor["id"],
                ),
            )
            result = {
                "proposal_id": proposal_id,
                "task_id": task_id,
                "promoted_by": actor["id"],
                "created_at": now,
            }
            connection.execute(
                "INSERT INTO external_agent_promotions VALUES (?,?,?,?)", tuple(result.values())
            )
            self._event(
                connection,
                actor,
                proposal["project_id"],
                task_id,
                "external_agent.proposal_promoted",
                1,
                {"proposal_id": proposal_id, "plan_digest": digest, "status": "draft"},
            )
            return 201, result

        return self._mutate(
            principal,
            key,
            "POST",
            f"/agent-proposals/{proposal_id}/promote",
            canonical_digest(payload),
            command,
            replay_guard=lambda c, a, _: (self._human(a), self._proposal(c, a, proposal_id)),
        )

    def _recipient(self, connection, actor, project_id, payload):
        self._human(actor)
        self._project(connection, actor, project_id)
        row = connection.execute(
            """SELECT p.id,p.org_id FROM credentials c JOIN principals p ON p.id=c.principal_id
            WHERE c.id=? AND c.project_id=? AND p.org_id=?""",
            (payload["credential_id"], project_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Context recipient was not found.")
        recipient = self._live_principal(connection, dict(row))
        recipient["agent_destination"] = payload["destination"]
        self._scope(connection, recipient, "context:read")

    def _artifact_context(self, connection, actor, payload):
        manifest = self._manifest(connection, actor, payload["manifest_id"])
        release = self._current_release(connection, manifest["id"])
        disposition = self._latest_disposition(connection, manifest["id"])
        if (
            release is None
            or release["state"] != "released"
            or disposition is None
            or disposition["disposition"] != "retain_quarantined"
        ):
            raise WithdrawnAgentContext("Artifact researcher custody is not currently released.")
        if (
            manifest["manifest_digest"] != payload["expected_manifest_digest"]
            or release["manifest_digest"] != manifest["manifest_digest"]
        ):
            raise Conflict("Artifact manifest digest changed.")
        if payload.get("custody_release_id", release["id"]) != release["id"]:
            raise WithdrawnAgentContext("Artifact disclosure custody release was superseded.")
        artifact, placement, tenant = self._artifact_for_read(
            connection, actor, manifest["project_id"], manifest, payload["artifact_id"]
        )
        if artifact["size"] > payload["max_bytes"]:
            raise Conflict("Artifact exceeds the delegated byte limit.")
        # Backend checks placement size and SHA256; never return a partial artifact.
        _, content = self._read_artifact(connection, actor, manifest, payload["artifact_id"])
        return release, artifact, content

    def create_context(self, principal, project_id, key, payload, *, artifact=False):
        path = f"/projects/{project_id}/agent-{'artifact-contexts' if artifact else 'contexts'}"

        def command(connection, actor):
            self._recipient(connection, actor, project_id, payload)
            stored = dict(payload)
            if artifact:
                manifest = self._manifest(connection, actor, payload["manifest_id"])
                if manifest["project_id"] != project_id:
                    raise NotFound("Artifact manifest was not found in this project.")
                release, metadata, _ = self._artifact_context(connection, actor, payload)
                stored.update(
                    custody_release_id=release["id"],
                    sha256=metadata["sha256"],
                    size=metadata["size"],
                )
            context_id = "agent_context_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO external_agent_contexts VALUES (?,?,?,?,?,?,?)",
                (
                    context_id,
                    project_id,
                    payload["credential_id"],
                    payload["destination"],
                    json.dumps(stored),
                    actor["id"],
                    utc_now(),
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                None,
                "external_agent.context_released",
                1,
                {
                    "context_id": context_id,
                    "credential_id": payload["credential_id"],
                    "destination": payload["destination"],
                    "classification": payload["classification"],
                },
            )
            return 201, self._context(connection, actor, context_id)

        def guard(connection, actor, response):
            self._recipient(connection, actor, project_id, payload)
            self._context(connection, actor, response["id"])

        return self._mutate(
            principal, key, "POST", path, canonical_digest(payload), command, replay_guard=guard
        )

    def _context(self, connection, actor, context_id):
        row = connection.execute(
            "SELECT * FROM external_agent_contexts WHERE id=?", (context_id,)
        ).fetchone()
        if row is None:
            raise NotFound("Agent context was not found.")
        self._project(connection, actor, row["project_id"])
        if actor["role"] != "human":
            scope = self._scope(connection, actor, "context:read")
            if (
                row["credential_id"] != actor["credential_id"]
                or row["destination"] != scope["destination"]
            ):
                raise NotFound("Agent context was not found.")
        payload = json.loads(row["payload_json"])
        response = {**dict(row), **payload, "revision": 1}
        response.pop("payload_json")
        if "artifact_id" in payload:
            _, metadata, content = self._artifact_context(connection, actor, payload)
            if metadata["sha256"] != payload["sha256"] or metadata["size"] != payload["size"]:
                raise Conflict("Delegated artifact identity changed.")
            response["content_base64"] = base64.b64encode(content).decode("ascii")
        return response

    def get_context(self, principal, context_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            return self._context(connection, self._actor(connection, principal), context_id)
        finally:
            connection.close()
