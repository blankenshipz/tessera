# ruff: noqa: E501, E701, E702
# fmt: off
"""Durable, provider-disabled Explain status discussion protocol.

This is intentionally not an operation-harness binding: its short lease fences
one read-only discussion turn and carries no execution, provider, credential
value, shell, MCP, or skill authority.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .astra import _digest
from .astra_status_models import (
    BeginStatusToolCall,
    ClaimExplainStatusTurn,
    RecordStatusToolResult,
    RequestExplainStatusTurn,
    SettleExplainStatusTurn,
)
from .store import Conflict, Forbidden, NotFound, Store, _connect, utc_now

_LEASE_SECONDS = 300
_TOOL = "read_experiment_status"


def create_astra_status_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS astra_status_discussions (
            turn_id TEXT PRIMARY KEY REFERENCES astra_coordinator_turns(id),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
            design_revision INTEGER NOT NULL CHECK (design_revision >= 1),
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision)
        )""",
        "CREATE INDEX IF NOT EXISTS astra_status_discussions_by_project ON astra_status_discussions(project_id,created_at,turn_id)",
        """CREATE TABLE IF NOT EXISTS astra_status_discussion_bindings (
            id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussions(turn_id),
            org_id TEXT NOT NULL REFERENCES organizations(id), project_id TEXT NOT NULL REFERENCES projects(id),
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id), design_revision INTEGER NOT NULL,
            credential_id TEXT NOT NULL REFERENCES credentials(id), agent_principal_id TEXT NOT NULL REFERENCES principals(id),
            lease_expires_at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('claimed','tool_intent','tool_result','answered','uncertain','cancelled','expired')),
            revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision)
        )""",
        "CREATE INDEX IF NOT EXISTS astra_status_bindings_by_project ON astra_status_discussion_bindings(project_id,state,lease_expires_at,id)",
        """CREATE TABLE IF NOT EXISTS astra_status_tool_invocations (
            id TEXT PRIMARY KEY, binding_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussion_bindings(id),
            tool_name TEXT NOT NULL CHECK (tool_name='read_experiment_status'), input_json TEXT NOT NULL, input_digest TEXT NOT NULL,
            result_json TEXT, result_digest TEXT, provenance_json TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('intent','recorded','uncertain','cancelled')),
            created_at TEXT NOT NULL, observed_at TEXT, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        )""",
        """CREATE TABLE IF NOT EXISTS astra_status_answers (
            id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussions(turn_id),
            binding_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussion_bindings(id),
            invocation_id TEXT NOT NULL UNIQUE REFERENCES astra_status_tool_invocations(id),
            org_id TEXT NOT NULL REFERENCES organizations(id), project_id TEXT NOT NULL REFERENCES projects(id),
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id), design_revision INTEGER NOT NULL,
            operation_ids_json TEXT NOT NULL, body TEXT NOT NULL CHECK (length(body)<=4000), body_digest TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            provider_disabled INTEGER NOT NULL CHECK (provider_disabled=1), created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision)
        )""",
        "CREATE INDEX IF NOT EXISTS astra_status_answers_by_project ON astra_status_answers(project_id,created_at,id)",
        """CREATE TRIGGER IF NOT EXISTS astra_status_tool_invocations_no_update_input BEFORE UPDATE OF input_json,input_digest,tool_name ON astra_status_tool_invocations
        BEGIN SELECT RAISE(ABORT, 'Astra status tool intent is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_status_answers_no_update BEFORE UPDATE ON astra_status_answers
        BEGIN SELECT RAISE(ABORT, 'Astra status answers are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_status_answers_no_delete BEFORE DELETE ON astra_status_answers
        BEGIN SELECT RAISE(ABORT, 'Astra status answers are immutable'); END""",
    )
    for statement in statements:
        connection.execute(statement)


class AstraStatusStore:
    """Atomic status-turn state, tool-intent, result, and answer records."""

    def __init__(self, store: Store):
        self.store = store

    def _mutate(self, actor: dict[str, Any], key: str, path: str, payload: dict[str, Any], command: Any):
        return self.store._mutate(actor, key, "POST", path, _digest(payload), command)

    @staticmethod
    def _human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("Only an authenticated human can request or cancel an Explain status turn.")

    @staticmethod
    def _agent(actor: dict[str, Any]) -> None:
        if actor["role"] != "agent":
            raise Forbidden("Only a scoped agent can settle an Explain status turn.")

    def _discussion(self, conn: sqlite3.Connection, actor: dict[str, Any], turn_id: str) -> dict[str, Any]:
        row = conn.execute("""SELECT d.*,t.state AS turn_state,t.revision AS turn_revision,t.claimed_by_principal_id,
            t.context_snapshot_id,t.updated_at FROM astra_status_discussions d JOIN astra_coordinator_turns t ON t.id=d.turn_id
            WHERE d.turn_id=? AND d.org_id=? AND (? IS NULL OR d.project_id=?)""", (turn_id, actor["org_id"], actor["project_id"], actor["project_id"])).fetchone()
        if row is None:
            raise NotFound("Explain status turn was not found.")
        return dict(row)

    @staticmethod
    def _binding(conn: sqlite3.Connection, discussion: dict[str, Any]) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM astra_status_discussion_bindings WHERE turn_id=?", (discussion["turn_id"],)).fetchone()
        return None if row is None else dict(row)

    def _expire_binding_before_command(self, actor: dict[str, Any], turn_id: str) -> None:
        """Commit expiry before returning a conflict, rather than rolling it back.

        ``Store._mutate`` correctly rolls back a rejected command.  Expiry is a
        durable observed fence, not part of that rejected command, so reconcile
        it in its own tiny transaction before entering the idempotent mutation.
        """
        conn = _connect(self.store.database_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            live = self.store._live_principal(conn, actor)
            self._agent(live)
            discussion = self._discussion(conn, live, turn_id)
            binding = self._binding(conn, discussion)
            active = binding is not None and binding["state"] in {"claimed", "tool_intent", "tool_result"}
            expired = active and datetime.fromisoformat(binding["lease_expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC)
            if expired:
                now = utc_now()
                conn.execute("UPDATE astra_status_discussion_bindings SET state='expired',revision=revision+1,updated_at=? WHERE id=?", (now, binding["id"]))
                self.store._event(conn, live, discussion["project_id"], None, "astra_status_turn_expired", discussion["turn_revision"], {"turn_id": turn_id, "provider_disabled": True})
            conn.execute("COMMIT")
            if expired:
                raise Conflict("Explain status discussion binding expired.")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _binding_response(binding: dict[str, Any] | None) -> dict[str, Any] | None:
        if binding is None:
            return None
        return {key: binding[key] for key in ("id", "turn_id", "experiment_id", "design_revision", "lease_expires_at", "state", "revision", "created_at", "updated_at")}

    def _response(self, conn: sqlite3.Connection, discussion: dict[str, Any]) -> dict[str, Any]:
        binding = self._binding(conn, discussion)
        invocation = None
        answer = None
        if binding is not None:
            invocation_row = conn.execute("SELECT * FROM astra_status_tool_invocations WHERE binding_id=?", (binding["id"],)).fetchone()
            if invocation_row is not None:
                value = dict(invocation_row)
                invocation = {"id": value["id"], "tool_name": value["tool_name"], "input_digest": value["input_digest"], "result_digest": value["result_digest"], "observed_at": value["observed_at"], "outcome": value["outcome"], "provenance": json.loads(value["provenance_json"])}
            answer_row = conn.execute("SELECT * FROM astra_status_answers WHERE binding_id=?", (binding["id"],)).fetchone()
            if answer_row is not None:
                value = dict(answer_row)
                answer = {"id": value["id"], "body": value["body"], "body_digest": value["body_digest"], "operation_ids": json.loads(value["operation_ids_json"]), "provider_disabled": True, "provenance": json.loads(value["provenance_json"]), "created_at": value["created_at"], "created_by_principal_id": value["created_by_principal_id"]}
        return {"turn_id": discussion["turn_id"], "project_id": discussion["project_id"], "experiment_id": discussion["experiment_id"], "design_revision": discussion["design_revision"], "state": discussion["turn_state"], "revision": discussion["turn_revision"], "binding": self._binding_response(binding), "tool_invocation": invocation, "answer": answer}

    @staticmethod
    def _current_result(conn: sqlite3.Connection, discussion: dict[str, Any]) -> dict[str, Any]:
        experiment = conn.execute("SELECT * FROM research_experiments WHERE id=? AND project_id=?", (discussion["experiment_id"], discussion["project_id"])).fetchone()
        if experiment is None or experiment["current_design_revision"] != discussion["design_revision"]:
            raise Conflict("Explain status design context is stale.")
        rows = conn.execute("""SELECT o.operation_id,o.project_id,o.task_id,o.desired_state,o.observed_state,o.provider_id
            FROM external_operations o WHERE o.org_id=? AND o.project_id=? AND
            (o.experiment_id=? OR EXISTS (SELECT 1 FROM research_experiment_attempts a JOIN research_experiment_variants v ON v.id=a.variant_id WHERE a.operation_id=o.operation_id AND v.experiment_id=?) OR EXISTS (SELECT 1 FROM research_experiment_admitted_attempts a WHERE a.operation_id=o.operation_id AND a.experiment_id=?))
            ORDER BY o.created_at,o.operation_id""", (discussion["org_id"], discussion["project_id"], discussion["experiment_id"], discussion["experiment_id"], discussion["experiment_id"])).fetchall()
        operations = [dict(row) for row in rows]
        return {"experiment_id": discussion["experiment_id"], "project_id": discussion["project_id"], "current_design_revision": discussion["design_revision"], "operation_count": len(operations), "operations": operations[:100], "operations_truncated": len(operations) > 100, "scientific_validity": "not_assessed"}

    def _active_binding(self, conn: sqlite3.Connection, actor: dict[str, Any], turn_id: str, expected: int, state: str) -> tuple[dict[str, Any], dict[str, Any]]:
        discussion = self._discussion(conn, actor, turn_id)
        binding = self._binding(conn, discussion)
        if binding is None or binding["agent_principal_id"] != actor["id"] or binding["credential_id"] != actor["credential_id"] or binding["revision"] != expected or binding["state"] != state:
            raise Conflict("Explain status discussion binding is no longer current.")
        if datetime.fromisoformat(binding["lease_expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC):
            raise Conflict("Explain status discussion binding expired.")
        if discussion["turn_state"] != "claimed" or discussion["claimed_by_principal_id"] != actor["id"]:
            raise Conflict("Explain status turn is no longer claimable.")
        self._current_result(conn, discussion)
        return discussion, binding

    def request_turn(self, actor: dict[str, Any], key: str, project_id: str, **payload: Any):
        body = RequestExplainStatusTurn(**payload)
        payload = body.model_dump(mode="json")
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._human(live); self.store._project(conn, live, project_id)
            experiment = conn.execute("SELECT current_design_revision FROM research_experiments WHERE id=? AND project_id=?", (body.experiment_id, project_id)).fetchone()
            if experiment is None: raise NotFound("Experiment was not found.")
            if experiment["current_design_revision"] != body.expected_design_revision: raise Conflict("Explain status design context is stale.")
            conversation = conn.execute("SELECT * FROM astra_project_conversations WHERE project_id=?", (project_id,)).fetchone()
            if conversation is None:
                if body.expected_conversation_revision != 0: raise Conflict("Astra conversation revision is stale.")
                now = utc_now(); conn.execute("INSERT INTO astra_project_conversations VALUES (?,?,?,?,?)", (project_id, live["org_id"], 0, now, live["id"])); conversation = {"revision": 0}
            if conversation["revision"] != body.expected_conversation_revision: raise Conflict("Astra conversation revision is stale.")
            now, revision = utc_now(), conversation["revision"] + 1
            message_id, snapshot_id, turn_id = "astra_message_" + uuid.uuid4().hex, "astra_context_" + uuid.uuid4().hex, "astra_turn_" + uuid.uuid4().hex
            refs = [{"kind": "experiment", "record_id": body.experiment_id, "expected_revision": body.expected_design_revision}]; encoded = json.dumps(refs, sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO astra_conversation_messages VALUES (?,?,?,?,?,?,?,?,?)", (message_id, project_id, revision, "human", body.prompt, None, snapshot_id, now, live["id"]))
            conn.execute("INSERT INTO astra_selected_context_snapshots VALUES (?,?,?,?,?,?,?,?)", (snapshot_id, live["org_id"], project_id, revision, encoded, _digest(refs), now, live["id"]))
            conn.execute("""INSERT INTO astra_coordinator_turns
                (id,org_id,project_id,input_message_id,context_snapshot_id,state,revision,
                 claimed_by_principal_id,claimed_at,claim_expires_at,claim_attempts,
                 completion_message_id,proposal_id,error_code,created_at,updated_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (turn_id, live["org_id"], project_id, message_id, snapshot_id, "requested", 1, None, None, None, 0, None, None, None, now, now, live["id"]))
            conn.execute(
                """INSERT INTO astra_status_discussions
                (turn_id,org_id,project_id,experiment_id,design_revision,created_at,
                 created_by_principal_id) VALUES (?,?,?,?,?,?,?)""",
                (
                    turn_id,
                    live["org_id"],
                    project_id,
                    body.experiment_id,
                    body.expected_design_revision,
                    now,
                    live["id"],
                ),
            )
            conn.execute("UPDATE astra_project_conversations SET revision=? WHERE project_id=?", (revision, project_id))
            self.store._event(conn, live, project_id, None, "astra_status_turn_requested", 1, {"turn_id": turn_id, "experiment_id": body.experiment_id, "design_revision": body.expected_design_revision, "provider_disabled": True})
            discussion = self._discussion(conn, live, turn_id)
            return 201, self._response(conn, discussion)
        return self._mutate(actor, key, f"/astra/projects/{project_id}/explain-status-turns", payload, command)

    def claim_turn(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = ClaimExplainStatusTurn(**payload)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._agent(live); discussion = self._discussion(conn, live, turn_id)
            if discussion["turn_state"] != "requested" or discussion["turn_revision"] != body.expected_revision: raise Conflict("Explain status turn is no longer claimable.")
            self._current_result(conn, discussion)
            expiry = datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS)
            credential_expiry = datetime.fromisoformat(live["expires_at"].replace("Z", "+00:00"))
            expiry = min(expiry, credential_expiry)
            if expiry <= datetime.now(UTC): raise Conflict("Explain status credential expired.")
            now, binding_id = utc_now(), "astra_status_binding_" + uuid.uuid4().hex
            conn.execute("UPDATE astra_coordinator_turns SET state='claimed',revision=revision+1,claimed_by_principal_id=?,claimed_at=?,updated_at=? WHERE id=?", (live["id"], now, now, turn_id))
            conn.execute(
                """INSERT INTO astra_status_discussion_bindings
                (id,turn_id,org_id,project_id,experiment_id,design_revision,credential_id,
                 agent_principal_id,lease_expires_at,state,revision,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    binding_id,
                    turn_id,
                    live["org_id"],
                    discussion["project_id"],
                    discussion["experiment_id"],
                    discussion["design_revision"],
                    live["credential_id"],
                    live["id"],
                    expiry.isoformat(),
                    "claimed",
                    1,
                    now,
                    now,
                ),
            )
            self.store._event(conn, live, discussion["project_id"], None, "astra_status_turn_claimed", 2, {"turn_id": turn_id, "binding_id": binding_id, "provider_disabled": True})
            return 200, self._response(conn, self._discussion(conn, live, turn_id))
        return self._mutate(actor, key, f"/astra/explain-status-turns/{turn_id}/claim", body.model_dump(mode="json"), command)

    def begin_tool(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = BeginStatusToolCall(**payload)
        payload = body.model_dump(mode="json")
        self._expire_binding_before_command(actor, turn_id)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._agent(live); discussion, binding = self._active_binding(conn, live, turn_id, body.expected_revision, "claimed")
            expected = {"identifier": discussion["experiment_id"]}
            if body.tool != _TOOL or body.arguments != expected: raise Conflict("Explain status permits only its bound read_experiment_status input.")
            now, invocation_id = utc_now(), "astra_status_tool_" + uuid.uuid4().hex
            encoded = json.dumps(expected, sort_keys=True, separators=(",", ":")); provenance = {"facade": "AstraAgentFacade", "harness": "tessera-agent.ExplainStatusCoordinator", "model": "deterministic-provider-disabled", "provider_disabled": True, "tool": _TOOL, "credential_id": live["credential_id"]}
            conn.execute(
                """INSERT INTO astra_status_tool_invocations
                (id,binding_id,tool_name,input_json,input_digest,result_json,result_digest,
                 provenance_json,outcome,created_at,observed_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    invocation_id,
                    binding["id"],
                    _TOOL,
                    encoded,
                    _digest(expected),
                    None,
                    None,
                    json.dumps(provenance, sort_keys=True, separators=(",", ":")),
                    "intent",
                    now,
                    None,
                    live["id"],
                ),
            )
            conn.execute("UPDATE astra_status_discussion_bindings SET state='tool_intent',revision=revision+1,updated_at=? WHERE id=?", (now, binding["id"]))
            return 200, self._response(conn, self._discussion(conn, live, turn_id))
        return self._mutate(actor, key, f"/astra/explain-status-turns/{turn_id}/tool-intent", payload, command)

    def record_result(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = RecordStatusToolResult(**payload); payload = body.model_dump(mode="json")
        self._expire_binding_before_command(actor, turn_id)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._agent(live); discussion, binding = self._active_binding(conn, live, turn_id, body.expected_revision, "tool_intent")
            expected = self._current_result(conn, discussion); result = body.result.model_dump(mode="json")
            if result != expected: raise Conflict("Explain status tool result is stale or does not match the bound control record.")
            invocation = conn.execute("SELECT * FROM astra_status_tool_invocations WHERE binding_id=?", (binding["id"],)).fetchone()
            if invocation is None or invocation["outcome"] != "intent": raise Conflict("Explain status tool invocation is no longer recordable.")
            now, encoded = utc_now(), json.dumps(result, sort_keys=True, separators=(",", ":"))
            conn.execute("UPDATE astra_status_tool_invocations SET result_json=?,result_digest=?,outcome='recorded',observed_at=? WHERE id=?", (encoded, _digest(result), now, invocation["id"]))
            conn.execute("UPDATE astra_status_discussion_bindings SET state='tool_result',revision=revision+1,updated_at=? WHERE id=?", (now, binding["id"]))
            return 200, self._response(conn, self._discussion(conn, live, turn_id))
        return self._mutate(actor, key, f"/astra/explain-status-turns/{turn_id}/tool-result", payload, command)

    def answer(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = SettleExplainStatusTurn(**payload)
        self._expire_binding_before_command(actor, turn_id)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._agent(live); discussion, binding = self._active_binding(conn, live, turn_id, body.expected_revision, "tool_result")
            invocation = conn.execute("SELECT * FROM astra_status_tool_invocations WHERE binding_id=? AND outcome='recorded'", (binding["id"],)).fetchone()
            if invocation is None: raise Conflict("Explain status tool result is unavailable.")
            result = json.loads(invocation["result_json"]); operation_ids = [item["operation_id"] for item in result["operations"]]
            answer = f"Experiment {result['experiment_id']} is at design revision {result['current_design_revision']} with {result['operation_count']} recorded operation(s). Scientific validity remains not assessed."
            now, answer_id, message_id = utc_now(), "astra_status_answer_" + uuid.uuid4().hex, "astra_message_" + uuid.uuid4().hex
            provenance = json.loads(invocation["provenance_json"])
            message_revision = conn.execute("SELECT revision FROM astra_project_conversations WHERE project_id=?", (discussion["project_id"],)).fetchone()["revision"] + 1
            conn.execute(
                """INSERT INTO astra_status_answers
                (id,turn_id,binding_id,invocation_id,org_id,project_id,experiment_id,
                  design_revision,operation_ids_json,body,body_digest,provenance_json,provider_disabled,
                  created_at,created_by_principal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    answer_id,
                    turn_id,
                    binding["id"],
                    invocation["id"],
                    live["org_id"],
                    discussion["project_id"],
                    discussion["experiment_id"],
                    discussion["design_revision"],
                    json.dumps(operation_ids, separators=(",", ":")),
                    answer,
                    _digest({"body": answer}),
                    json.dumps(provenance, sort_keys=True, separators=(",", ":")),
                    1,
                    now,
                    live["id"],
                ),
            )
            conn.execute("INSERT INTO astra_conversation_messages VALUES (?,?,?,?,?,?,?,?,?)", (message_id, discussion["project_id"], message_revision, "coordinator", answer, turn_id, discussion["context_snapshot_id"], now, live["id"]))
            conn.execute("UPDATE astra_project_conversations SET revision=? WHERE project_id=?", (message_revision, discussion["project_id"]))
            conn.execute("UPDATE astra_status_discussion_bindings SET state='answered',revision=revision+1,updated_at=? WHERE id=?", (now, binding["id"]))
            conn.execute("UPDATE astra_coordinator_turns SET state='completed',revision=revision+1,completion_message_id=?,updated_at=? WHERE id=?", (message_id, now, turn_id))
            self.store._event(conn, live, discussion["project_id"], None, "astra_status_turn_answered", discussion["turn_revision"] + 1, {"turn_id": turn_id, "answer_id": answer_id, "invocation_id": invocation["id"], "provider_disabled": True, "authority_effect": "none"})
            return 200, self._response(conn, self._discussion(conn, live, turn_id))
        return self._mutate(actor, key, f"/astra/explain-status-turns/{turn_id}/answer", body.model_dump(mode="json"), command)

    def uncertain(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = SettleExplainStatusTurn(**payload)
        self._expire_binding_before_command(actor, turn_id)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._agent(live); discussion, binding = self._active_binding(conn, live, turn_id, body.expected_revision, "tool_intent")
            now = utc_now(); conn.execute("UPDATE astra_status_discussion_bindings SET state='uncertain',revision=revision+1,updated_at=? WHERE id=?", (now, binding["id"]))
            conn.execute("UPDATE astra_status_tool_invocations SET outcome='uncertain' WHERE binding_id=? AND outcome='intent'", (binding["id"],))
            self.store._event(conn, live, discussion["project_id"], None, "astra_status_turn_uncertain", discussion["turn_revision"], {"turn_id": turn_id, "provider_disabled": True})
            return 200, self._response(conn, self._discussion(conn, live, turn_id))
        return self._mutate(actor, key, f"/astra/explain-status-turns/{turn_id}/uncertain", body.model_dump(mode="json"), command)

    def cancel(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = SettleExplainStatusTurn(**payload)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._human(live); discussion = self._discussion(conn, live, turn_id)
            if discussion["turn_state"] not in {"requested", "claimed"} or discussion["turn_revision"] != body.expected_revision: raise Conflict("Explain status turn is no longer cancellable.")
            now = utc_now(); binding = self._binding(conn, discussion)
            if binding is not None and binding["state"] not in {"answered", "uncertain", "cancelled", "expired"}:
                conn.execute("UPDATE astra_status_discussion_bindings SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?", (now, binding["id"]))
                conn.execute("UPDATE astra_status_tool_invocations SET outcome='cancelled' WHERE binding_id=? AND outcome='intent'", (binding["id"],))
            conn.execute("UPDATE astra_coordinator_turns SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?", (now, turn_id))
            self.store._event(conn, live, discussion["project_id"], None, "astra_status_turn_cancelled", discussion["turn_revision"] + 1, {"turn_id": turn_id, "provider_disabled": True})
            return 200, self._response(conn, self._discussion(conn, live, turn_id))
        return self._mutate(actor, key, f"/astra/explain-status-turns/{turn_id}/cancel", body.model_dump(mode="json"), command)

    def get_turn(self, actor: dict[str, Any], turn_id: str) -> dict[str, Any]:
        conn = _connect(self.store.database_path)
        try:
            live = self.store._live_principal(conn, actor); return self._response(conn, self._discussion(conn, live, turn_id))
        finally: conn.close()

    def list_turns(self, actor: dict[str, Any], project_id: str, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        conn = _connect(self.store.database_path)
        try:
            live = self.store._live_principal(conn, actor); self.store._project(conn, live, project_id)
            ids = conn.execute("SELECT turn_id FROM astra_status_discussions WHERE org_id=? AND project_id=? ORDER BY created_at,turn_id LIMIT ? OFFSET ?", (live["org_id"], project_id, limit, offset)).fetchall()
            return [self._response(conn, self._discussion(conn, live, row["turn_id"])) for row in ids]
        finally: conn.close()
