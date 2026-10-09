"""Durable external-agent research proposals, observations and human-review requests."""

import json
import uuid

from .agent_authority import AgentAuthorityStore
from .agent_research_models import AgentObservation, AgentReviewRequest, ResearchProposal
from .external_agent_models import PromoteAgentProposal
from .research import ResearchStore
from .store import Conflict, Forbidden, NotFound, _connect, canonical_digest, utc_now


def create_agent_research_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_research_records (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        actor_id TEXT NOT NULL REFERENCES principals(id), kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_research_promotions (
        proposal_id TEXT PRIMARY KEY REFERENCES agent_research_records(id),
        result_json TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL)""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS agent_research_project "
        "ON agent_research_records(org_id,project_id,sequence)"
    )
    for table in ("agent_research_records", "agent_research_promotions"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN "
                "SELECT RAISE(ABORT,'agent research history is immutable'); END"
            )


class AgentResearchStore(AgentAuthorityStore):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.research = ResearchStore(state_dir)

    def _authorized_attempt(self, connection, actor, project_id, attempt_id):
        row = connection.execute(
            """SELECT a.*,e.project_id FROM research_experiment_admitted_attempts a
            JOIN research_experiments e ON e.id=a.experiment_id WHERE a.id=? AND e.project_id=?""",
            (attempt_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFound("Authorized attempt was not found.")
        grants = connection.execute(
            "SELECT bounds_json FROM agent_authority_grants "
            "WHERE org_id=? AND project_id=? AND subject_credential_id=?",
            (actor["org_id"], project_id, actor["credential_id"]),
        ).fetchall()
        if not any(
            any(
                b["execution_grant_id"] == row["grant_id"] and b["variant_id"] == row["variant_id"]
                for b in json.loads(g[0])["bindings"]
            )
            for g in grants
        ):
            raise NotFound("Authorized attempt was not found.")
        return row

    def _subject(self, connection, actor, project_id, body):
        kind = body["subject_type"]
        if kind == "attempt":
            subject = self._authorized_attempt(connection, actor, project_id, body["subject_id"])
            revision = subject["revision"]
        elif kind == "experiment":
            subject = self.experiments._experiment(connection, actor, body["subject_id"])
            if subject["project_id"] != project_id:
                raise NotFound("Research subject was not found.")
            # Review is permitted only for an experiment named by this agent's authority.
            attempts = connection.execute(
                "SELECT id FROM research_experiment_admitted_attempts WHERE experiment_id=?",
                (subject["id"],),
            ).fetchall()
            allowed = False
            for attempt in attempts:
                try:
                    self._authorized_attempt(connection, actor, project_id, attempt[0])
                    allowed = True
                    break
                except NotFound:
                    continue
            if not allowed:
                raise NotFound("Authorized research subject was not found.")
            revision = subject["current_design_revision"]
        else:
            subject = connection.execute(
                "SELECT * FROM agent_research_records WHERE id=? AND org_id=? "
                "AND project_id=? AND credential_id=? AND kind=?",
                (body["subject_id"], actor["org_id"], project_id, actor["credential_id"], kind),
            ).fetchone()
            if subject is None:
                raise NotFound("Research subject was not found.")
            revision = 1
        if revision != body["expected_revision"]:
            raise Conflict("Research subject revision changed.")

    def _observation(self, connection, actor, project_id, body):
        attempt = self._authorized_attempt(connection, actor, project_id, body["attempt_id"])
        if attempt["revision"] != body["expected_attempt_revision"]:
            raise Conflict("Observation attempt revision changed.")
        if len(set(body["artifact_ids"])) != len(body["artifact_ids"]):
            raise Conflict("Observation artifacts must be unique.")
        for artifact_id in body["artifact_ids"]:
            rows = connection.execute(
                "SELECT id,payload_json FROM external_agent_contexts WHERE project_id=? "
                "AND credential_id=? AND destination=? "
                "AND json_extract(payload_json,'$.artifact_id')=? ORDER BY created_at,id",
                (project_id, actor["credential_id"], actor["agent_destination"], artifact_id),
            ).fetchall()
            if not rows:
                raise Forbidden("Observation artifact has not been disclosed to this destination.")
            # Rechecks current custody/retention/integrity, never a cached disclosure.
            self._context(connection, actor, rows[-1]["id"])

    @staticmethod
    def _record(row):
        return {
            "id": row["id"],
            "sequence": row["sequence"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "digest": row["digest"],
            "revision": 1,
            "created_at": row["created_at"],
            "actor_id": row["actor_id"],
            "provenance_kind": "external_agent_caller_attested",
            "authority_effect": "none",
        }

    def append(self, principal, project_id, kind, key, payload):
        model = {
            "proposal": ResearchProposal,
            "observation": AgentObservation,
            "review-request": AgentReviewRequest,
        }[kind]
        body = model.model_validate(payload).model_dump(mode="json")
        scope = {
            "proposal": "proposal:create",
            "observation": "observation:create",
            "review-request": "review:request",
        }[kind]
        path_suffix = {
            "proposal": "agent-workflow-proposals",
            "observation": "agent-observations",
            "review-request": "agent-review-requests",
        }[kind]

        def command(connection, actor):
            self._scope(connection, actor, scope)
            self._project(connection, actor, project_id)
            if kind == "review-request":
                self._subject(connection, actor, project_id, body)
            elif kind == "observation":
                self._observation(connection, actor, project_id, body)
            elif body["proposal"]["kind"] == "experiment":
                study = self.research._study(connection, actor, body["proposal"]["study_id"])
                if study["project_id"] != project_id:
                    raise NotFound("Proposal study was not found.")
                self.experiments._validate_variant_execution_plans(
                    connection, actor, project_id, body["proposal"]["experiment"]["variants"]
                )
            ident = "agent_research_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO agent_research_records "
                "(id,org_id,project_id,credential_id,actor_id,kind,payload_json,digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    actor["org_id"],
                    project_id,
                    actor["credential_id"],
                    actor["id"],
                    kind,
                    json.dumps(body),
                    canonical_digest(body),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                None,
                "agent_research." + kind.replace("-", "_"),
                1,
                {"record_id": ident, "subject_id": body.get("subject_id")},
            )
            refs = {"message_id": ident}
            if kind == "observation":
                refs["attempt_id"] = body["attempt_id"]
            elif kind == "review-request" and body["subject_type"] in {"attempt", "experiment"}:
                refs[body["subject_type"] + "_id"] = body["subject_id"]
            self._provenance(
                connection,
                actor,
                project_id,
                ident,
                "message",
                "requested" if kind == "review-request" else "succeeded",
                refs,
            )
            return 201, self._record(
                connection.execute(
                    "SELECT * FROM agent_research_records WHERE id=?", (ident,)
                ).fetchone()
            )

        def replay(connection, actor, response):
            self._scope(connection, actor, scope)
            self._project(connection, actor, project_id)
            if kind == "review-request":
                self._subject(connection, actor, project_id, body)
            elif kind == "observation":
                self._observation(connection, actor, project_id, body)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/{path_suffix}",
            canonical_digest(body),
            command,
            replay_guard=replay,
        )

    def list_proposals(self, principal, project_id, offset=0, limit=100):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            self._project(connection, actor, project_id)
            credential = None
            if actor["role"] != "human":
                self._scope(connection, actor, "proposal:read")
                credential = actor["credential_id"]
            return [
                self._record(row)
                for row in connection.execute(
                    "SELECT * FROM agent_research_records WHERE project_id=? AND kind='proposal' "
                    "AND (? IS NULL OR credential_id=?) ORDER BY sequence LIMIT ? OFFSET ?",
                    (project_id, credential, credential, limit, offset),
                )
            ]
        finally:
            connection.close()

    def promote(self, principal, proposal_id, key, payload):
        body = self._body(PromoteAgentProposal, payload)

        def command(connection, actor):
            self._human(actor)
            row = connection.execute(
                "SELECT * FROM agent_research_records WHERE id=? AND org_id=? AND kind='proposal'",
                (proposal_id, actor["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Research proposal was not found.")
            self._project(connection, actor, row["project_id"])
            if body["expected_revision"] != 1 or body["expected_digest"] != row["digest"]:
                raise Conflict("Research proposal revision or digest changed.")
            existing = connection.execute(
                "SELECT result_json FROM agent_research_promotions WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing:
                return 200, json.loads(existing[0])
            proposal = json.loads(row["payload_json"])["proposal"]
            if proposal["kind"] == "project":
                _, resource = self._create_project_transaction(
                    connection, actor, {"name": proposal["name"]}
                )
            elif proposal["kind"] == "study":
                _, resource = self.research._create_study_transaction(
                    connection, actor, row["project_id"], proposal["study"]
                )
            else:
                _, resource = self.experiments._create_experiment_transaction(
                    connection, actor, row["project_id"], proposal["experiment"]
                )
            result = {
                "proposal_id": proposal_id,
                "kind": proposal["kind"],
                "resource": resource,
                "execution_authorized": False,
            }
            connection.execute(
                "INSERT INTO agent_research_promotions VALUES (?,?,?,?)",
                (proposal_id, json.dumps(result), actor["id"], utc_now()),
            )
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                "agent_research.promoted",
                1,
                {"proposal_id": proposal_id, "resource_id": resource["id"]},
            )
            return 201, result

        return self._mutate(
            principal,
            key,
            "POST",
            f"/agent-research-proposals/{proposal_id}/promote",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._human(a),
        )

    def notebook(self, principal, project_id, **options):
        from .agent_provenance import project_external_agent_notebook

        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            self._scope(connection, actor, "notebook:read")
            self._project(connection, actor, project_id)
            return project_external_agent_notebook(connection, actor, project_id, **options)
        finally:
            connection.close()
