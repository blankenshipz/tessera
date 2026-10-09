# ruff: noqa: E501
# fmt: off
"""Durable, provider-disabled coordinator records for Astra's unified interface.

This module deliberately records only discussion and non-authoritative proposals.
It neither reads credentials nor invokes providers, tools, skills, approval, grants,
or dispatch paths.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .astra_models import (
    CancelCoordinatorTurn,
    ClaimCoordinatorTurn,
    CompleteCoordinatorTurn,
    ConsumerHeartbeat,
    FailCoordinatorTurn,
    RegisterProposalDesign,
    RequestCoordinatorTurn,
)
from .store import Conflict, Forbidden, NotFound, Store, _connect, utc_now

_CLAIM_LEASE_SECONDS = 300
_MAX_CLAIM_ATTEMPTS = 3
_CONSUMER_HEALTH_SECONDS = 60


def create_astra_schema(
    connection: sqlite3.Connection, *, include_proposal_promotion: bool = True
) -> None:
    """Install empty additive conversation/coordinator records without authority."""
    proposal_table = """CREATE TABLE IF NOT EXISTS astra_proposals (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            conversation_revision INTEGER NOT NULL CHECK (conversation_revision >= 1),
            turn_id TEXT NOT NULL UNIQUE REFERENCES astra_coordinator_turns(id),
            proposal_type TEXT NOT NULL CHECK (proposal_type = 'human_review_needed'),
            body TEXT NOT NULL CHECK (length(body) <= 4000), recap TEXT NOT NULL CHECK (length(recap) <= 4000),
            status_json TEXT NOT NULL, provider_disabled INTEGER NOT NULL CHECK (provider_disabled = 1),
            authority_effect TEXT NOT NULL CHECK (authority_effect = 'none')"""
    if include_proposal_promotion:
        proposal_table += ", design_json TEXT"
    proposal_table += ", created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id))"
    statements = (
        """CREATE TABLE IF NOT EXISTS astra_project_conversations (
            project_id TEXT PRIMARY KEY REFERENCES projects(id),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            revision INTEGER NOT NULL CHECK (revision >= 0),
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        )""",
        """CREATE TABLE IF NOT EXISTS astra_conversation_messages (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            conversation_revision INTEGER NOT NULL CHECK (conversation_revision >= 1),
            sender_kind TEXT NOT NULL CHECK (sender_kind IN ('human','coordinator')),
            body TEXT NOT NULL CHECK (length(body) <= 4000),
            turn_id TEXT, context_snapshot_id TEXT,
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            UNIQUE(project_id, conversation_revision)
        )""",
        "CREATE INDEX IF NOT EXISTS astra_conversation_messages_by_project ON astra_conversation_messages(project_id, conversation_revision, id)",
        """CREATE TABLE IF NOT EXISTS astra_selected_context_snapshots (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            conversation_revision INTEGER NOT NULL CHECK (conversation_revision >= 1),
            selected_context_json TEXT NOT NULL, selected_context_digest TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        )""",
        "CREATE INDEX IF NOT EXISTS astra_context_snapshots_by_project ON astra_selected_context_snapshots(project_id, created_at, id)",
        """CREATE TABLE IF NOT EXISTS astra_coordinator_turns (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            input_message_id TEXT NOT NULL UNIQUE REFERENCES astra_conversation_messages(id),
            context_snapshot_id TEXT NOT NULL REFERENCES astra_selected_context_snapshots(id),
            state TEXT NOT NULL CHECK (state IN ('requested','claimed','completed','failed','cancelled')),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            claimed_by_principal_id TEXT REFERENCES principals(id), claimed_at TEXT,
            completion_message_id TEXT UNIQUE REFERENCES astra_conversation_messages(id),
            proposal_id TEXT UNIQUE, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        )""",
        "CREATE INDEX IF NOT EXISTS astra_coordinator_turns_by_project ON astra_coordinator_turns(project_id, state, created_at, id)",
        proposal_table,
        "CREATE INDEX IF NOT EXISTS astra_proposals_by_project ON astra_proposals(project_id, created_at, id)",
        """CREATE TRIGGER IF NOT EXISTS astra_context_snapshots_no_update BEFORE UPDATE ON astra_selected_context_snapshots
        BEGIN SELECT RAISE(ABORT, 'Astra selected-context snapshots are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_context_snapshots_no_delete BEFORE DELETE ON astra_selected_context_snapshots
        BEGIN SELECT RAISE(ABORT, 'Astra selected-context snapshots are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_proposals_no_update BEFORE UPDATE ON astra_proposals
        BEGIN SELECT RAISE(ABORT, 'Astra proposals are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_proposals_no_delete BEFORE DELETE ON astra_proposals
        BEGIN SELECT RAISE(ABORT, 'Astra proposals are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_conversation_messages_no_update BEFORE UPDATE ON astra_conversation_messages
        BEGIN SELECT RAISE(ABORT, 'Astra conversation messages are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS astra_conversation_messages_no_delete BEFORE DELETE ON astra_conversation_messages
        BEGIN SELECT RAISE(ABORT, 'Astra conversation messages are immutable'); END""",
    )
    if include_proposal_promotion:
        statements += (
            """CREATE TABLE IF NOT EXISTS astra_proposal_design_registrations (
                proposal_id TEXT PRIMARY KEY REFERENCES astra_proposals(id),
                org_id TEXT NOT NULL REFERENCES organizations(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                experiment_id TEXT NOT NULL UNIQUE REFERENCES research_experiments(id),
                design_revision INTEGER NOT NULL CHECK (design_revision = 1),
                context_digest TEXT NOT NULL,
                created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
            )""",
            "CREATE INDEX IF NOT EXISTS astra_proposal_design_registrations_by_project ON astra_proposal_design_registrations(project_id, created_at, proposal_id)",
        )
    for statement in statements:
        connection.execute(statement)


def create_astra_consumer_schema(connection: sqlite3.Connection) -> None:
    """Add lease/retry metadata without inferring a live ordinary consumer."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(astra_coordinator_turns)")}
    if "claim_expires_at" not in columns:
        connection.execute("ALTER TABLE astra_coordinator_turns ADD COLUMN claim_expires_at TEXT")
    if "claim_attempts" not in columns:
        connection.execute(
            "ALTER TABLE astra_coordinator_turns ADD COLUMN claim_attempts INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS astra_coordinator_turns_claim_lease "
        "ON astra_coordinator_turns(project_id,state,claim_expires_at,created_at,id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS astra_consumer_health (
        agent_principal_id TEXT PRIMARY KEY REFERENCES principals(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        heartbeat_at TEXT NOT NULL, last_outcome TEXT NOT NULL CHECK (
            last_outcome IN ('idle','claimed','completed','retrying')),
        updated_at TEXT NOT NULL)"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS astra_consumer_health_by_project "
        "ON astra_consumer_health(project_id,heartbeat_at,agent_principal_id)"
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AstraStore:
    """Transactional project conversation and provider-disabled coordinator state."""

    def __init__(self, store: Store):
        self.store = store

    def _mutate(self, actor: dict[str, Any], key: str, method: str, path: str, payload: dict[str, Any], command: Any):
        return self.store._mutate(actor, key, method, path, _digest(payload), command)

    @staticmethod
    def _require_human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("Only an authenticated human can request or cancel an Astra coordinator turn.")

    @staticmethod
    def _require_agent(actor: dict[str, Any]) -> None:
        if actor["role"] != "agent":
            raise Forbidden("Only a scoped coordinator agent can claim or settle an Astra coordinator turn.")

    @staticmethod
    def _conversation_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {"project_id": value["project_id"], "revision": value["revision"], "created_at": value["created_at"]}

    @staticmethod
    def _message_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {"id": value["id"], "project_id": value["project_id"], "conversation_revision": value["conversation_revision"], "sender_kind": value["sender_kind"], "body": value["body"], "turn_id": value["turn_id"], "context_snapshot_id": value["context_snapshot_id"], "created_at": value["created_at"]}

    @staticmethod
    def _snapshot_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {"id": value["id"], "project_id": value["project_id"], "conversation_revision": value["conversation_revision"], "selected_context": json.loads(value["selected_context_json"]), "selected_context_digest": value["selected_context_digest"], "created_at": value["created_at"]}

    @staticmethod
    def _turn_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {"id": value["id"], "project_id": value["project_id"], "input_message_id": value["input_message_id"], "context_snapshot_id": value["context_snapshot_id"], "state": value["state"], "revision": value["revision"], "claimed_by_principal_id": value["claimed_by_principal_id"], "claimed_at": value["claimed_at"], "claim_expires_at": value.get("claim_expires_at"), "claim_attempts": value.get("claim_attempts", 0), "completion_message_id": value["completion_message_id"], "proposal_id": value["proposal_id"], "error_code": value["error_code"], "created_at": value["created_at"], "updated_at": value["updated_at"]}

    @staticmethod
    def _proposal_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        registration = None if value.get("registered_experiment_id") is None else {
            "experiment_id": value["registered_experiment_id"],
            "design_revision": value["registered_design_revision"],
            "created_at": value["registration_created_at"],
        }
        return {"id": value["id"], "project_id": value["project_id"], "conversation_revision": value["conversation_revision"], "turn_id": value["turn_id"], "proposal_type": value["proposal_type"], "body": value["body"], "recap": value["recap"], "status": json.loads(value["status_json"]), "provider_disabled": bool(value["provider_disabled"]), "authority_effect": value["authority_effect"], "execution_authorized": False, "design": json.loads(value["design_json"]) if value.get("design_json") else None, "context_digest": value.get("context_digest"), "registration": registration, "created_at": value["created_at"]}

    def _validate_context(self, conn: sqlite3.Connection, actor: dict[str, Any], project_id: str, refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            if ref["kind"] == "task":
                record = self.store._task(conn, actor, ref["record_id"])
                revision = record["revision"]
            else:
                row = conn.execute("SELECT current_design_revision FROM research_experiments WHERE id=? AND project_id=?", (ref["record_id"], project_id)).fetchone()
                if row is None:
                    raise NotFound("Selected experiment context was not found.")
                revision = row["current_design_revision"]
            if (ref["kind"] == "task" and record["project_id"] != project_id) or revision != ref["expected_revision"]:
                raise Conflict("Selected context is stale or outside the project.")

    def _turn(self, conn: sqlite3.Connection, actor: dict[str, Any], turn_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM astra_coordinator_turns WHERE id=? AND org_id=?", (turn_id, actor["org_id"])).fetchone()
        if row is None or (actor["project_id"] is not None and row["project_id"] != actor["project_id"]):
            raise NotFound("Astra coordinator turn was not found.")
        return dict(row)

    def _fresh_context(self, conn: sqlite3.Connection, turn: dict[str, Any]) -> None:
        snapshot = conn.execute("SELECT conversation_revision FROM astra_selected_context_snapshots WHERE id=? AND project_id=?", (turn["context_snapshot_id"], turn["project_id"])).fetchone()
        conversation = conn.execute("SELECT revision FROM astra_project_conversations WHERE project_id=?", (turn["project_id"],)).fetchone()
        if snapshot is None or conversation is None or snapshot["conversation_revision"] != conversation["revision"]:
            raise Conflict("Astra coordinator context is stale.")

    @staticmethod
    def _lease_expiry() -> str:
        return (datetime.now(UTC) + timedelta(seconds=_CLAIM_LEASE_SECONDS)).isoformat()

    @staticmethod
    def _lease_is_live(turn: dict[str, Any]) -> bool:
        expiry = turn.get("claim_expires_at")
        if not isinstance(expiry, str):
            return False
        try:
            return datetime.fromisoformat(expiry.replace("Z", "+00:00")) > datetime.now(UTC)
        except ValueError:
            return False

    def _claim(self, conn: sqlite3.Connection, live: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
        if turn["claim_attempts"] >= _MAX_CLAIM_ATTEMPTS:
            raise Conflict("Astra coordinator turn retry budget is exhausted.")
        self._fresh_context(conn, turn)
        now, expiry = utc_now(), self._lease_expiry()
        conn.execute(
            """UPDATE astra_coordinator_turns SET state='claimed',revision=revision+1,
            claimed_by_principal_id=?,claimed_at=?,claim_expires_at=?,claim_attempts=claim_attempts+1,
            updated_at=? WHERE id=?""",
            (live["id"], now, expiry, now, turn["id"]),
        )
        turn.update(
            state="claimed",
            revision=turn["revision"] + 1,
            claimed_by_principal_id=live["id"],
            claimed_at=now,
            claim_expires_at=expiry,
            claim_attempts=turn["claim_attempts"] + 1,
            updated_at=now,
        )
        self.store._event(
            conn,
            live,
            turn["project_id"],
            None,
            "astra_coordinator_turn_claimed",
            turn["revision"],
            {
                "turn_id": turn["id"],
                "claim_attempt": turn["claim_attempts"],
                "provider_disabled": True,
            },
        )
        return turn

    def _next_claimable_turn(self, conn: sqlite3.Connection, live: dict[str, Any]) -> dict[str, Any] | None:
        """Return this agent's oldest request or safely reclaim its expired lease."""
        project_id = live["project_id"]
        if project_id is None:
            raise Forbidden("A coordinator credential must be scoped to one project.")
        now = utc_now()
        rows = conn.execute(
            """SELECT * FROM astra_coordinator_turns WHERE org_id=? AND project_id=? AND
            NOT EXISTS (SELECT 1 FROM astra_status_discussions d WHERE d.turn_id=astra_coordinator_turns.id) AND
            (state='requested' OR (state='claimed' AND claimed_by_principal_id=?) OR
             (state='claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at<=?))
            ORDER BY created_at,id LIMIT 17""",
            (live["org_id"], project_id, live["id"], now),
        ).fetchall()
        for row in rows:
            turn = dict(row)
            if turn["state"] == "claimed":
                if turn["claimed_by_principal_id"] == live["id"] and self._lease_is_live(turn):
                    return turn
                if turn["claim_attempts"] >= _MAX_CLAIM_ATTEMPTS:
                    conn.execute(
                        "UPDATE astra_coordinator_turns SET state='failed',revision=revision+1,"
                        "error_code='claim_retry_exhausted',updated_at=? WHERE id=?",
                        (now, turn["id"]),
                    )
                    self.store._event(conn, live, project_id, None, "astra_coordinator_turn_failed", turn["revision"] + 1, {"turn_id": turn["id"], "error_code": "claim_retry_exhausted", "provider_disabled": True})
                    continue
                conn.execute(
                    "UPDATE astra_coordinator_turns SET state='requested',claimed_by_principal_id=NULL,"
                    "claimed_at=NULL,claim_expires_at=NULL,updated_at=? WHERE id=? AND state='claimed'",
                    (now, turn["id"]),
                )
                turn.update(state="requested", claimed_by_principal_id=None, claimed_at=None, claim_expires_at=None, updated_at=now)
            try:
                return self._claim(conn, live, turn)
            except Conflict:
                # A stale selected context is terminal for this deterministic run;
                # it cannot be retried with a changed authority snapshot.
                conn.execute(
                    "UPDATE astra_coordinator_turns SET state='failed',revision=revision+1,"
                    "error_code='stale_context',updated_at=? WHERE id=? AND state='requested'",
                    (now, turn["id"]),
                )
        return None

    def request_turn(self, actor: dict[str, Any], key: str, project_id: str, **payload: Any):
        body = RequestCoordinatorTurn(**payload)
        payload = body.model_dump(mode="json")

        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._require_human(live)
            self.store._project(conn, live, project_id)
            conversation = conn.execute("SELECT * FROM astra_project_conversations WHERE project_id=?", (project_id,)).fetchone()
            if conversation is None:
                if body.expected_conversation_revision != 0:
                    raise Conflict("Astra conversation revision is stale.")
                now = utc_now()
                conn.execute("INSERT INTO astra_project_conversations VALUES (?,?,?,?,?)", (project_id, live["org_id"], 0, now, live["id"]))
                conversation = {"project_id": project_id, "org_id": live["org_id"], "revision": 0, "created_at": now}
            if conversation["revision"] != body.expected_conversation_revision:
                raise Conflict("Astra conversation revision is stale.")
            refs = [item.model_dump(mode="json") for item in body.selected_context]
            self._validate_context(conn, live, project_id, refs)
            now, revision = utc_now(), conversation["revision"] + 1
            message_id, snapshot_id, turn_id = "astra_message_" + uuid.uuid4().hex, "astra_context_" + uuid.uuid4().hex, "astra_turn_" + uuid.uuid4().hex
            encoded = json.dumps(refs, sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO astra_conversation_messages VALUES (?,?,?,?,?,?,?,?,?)", (message_id, project_id, revision, "human", body.prompt, None, snapshot_id, now, live["id"]))
            conn.execute("INSERT INTO astra_selected_context_snapshots VALUES (?,?,?,?,?,?,?,?)", (snapshot_id, live["org_id"], project_id, revision, encoded, _digest(refs), now, live["id"]))
            conn.execute("""INSERT INTO astra_coordinator_turns
                (id,org_id,project_id,input_message_id,context_snapshot_id,state,revision,
                 claimed_by_principal_id,claimed_at,claim_expires_at,claim_attempts,
                 completion_message_id,proposal_id,error_code,created_at,updated_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (turn_id, live["org_id"], project_id, message_id, snapshot_id, "requested", 1, None, None, None, 0, None, None, None, now, now, live["id"]))
            conn.execute("UPDATE astra_project_conversations SET revision=? WHERE project_id=?", (revision, project_id))
            self.store._event(conn, live, project_id, None, "astra_coordinator_turn_requested", revision, {"turn_id": turn_id, "context_snapshot_id": snapshot_id, "provider_disabled": True})
            return 201, {"conversation": {**self._conversation_response(conversation), "revision": revision}, "input_message": self._message_response({"id": message_id, "project_id": project_id, "conversation_revision": revision, "sender_kind": "human", "body": body.prompt, "turn_id": None, "context_snapshot_id": snapshot_id, "created_at": now}), "context_snapshot": self._snapshot_response({"id": snapshot_id, "project_id": project_id, "conversation_revision": revision, "selected_context_json": encoded, "selected_context_digest": _digest(refs), "created_at": now}), "turn": self._turn_response({"id": turn_id, "project_id": project_id, "input_message_id": message_id, "context_snapshot_id": snapshot_id, "state": "requested", "revision": 1, "claimed_by_principal_id": None, "claimed_at": None, "completion_message_id": None, "proposal_id": None, "error_code": None, "created_at": now, "updated_at": now})}

        return self._mutate(actor, key, "POST", f"/astra/projects/{project_id}/turns", payload, command)

    def claim_turn(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = ClaimCoordinatorTurn(**payload)

        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._require_agent(live)
            turn = self._turn(conn, live, turn_id)
            if conn.execute("SELECT 1 FROM astra_status_discussions WHERE turn_id=?", (turn_id,)).fetchone():
                raise Conflict("Explain status turns require the mediated status protocol.")
            if turn["state"] != "requested" or turn["revision"] != body.expected_revision:
                raise Conflict("Astra coordinator turn is no longer claimable.")
            return 200, self._turn_response(self._claim(conn, live, turn))

        return self._mutate(actor, key, "POST", f"/astra/turns/{turn_id}/claim", body.model_dump(mode="json"), command)

    def claim_next_turn(self, actor: dict[str, Any], key: str):
        """Atomically take the next project-scoped turn without a caller-supplied ID."""
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._require_agent(live)
            turn = self._next_claimable_turn(conn, live)
            if turn is None:
                return 200, {"turn": None, "availability": "idle", "provider_disabled": True}
            input_row = conn.execute("SELECT body FROM astra_conversation_messages WHERE id=?", (turn["input_message_id"],)).fetchone()
            snapshot = conn.execute("SELECT selected_context_json FROM astra_selected_context_snapshots WHERE id=?", (turn["context_snapshot_id"],)).fetchone()
            return 200, {"turn": self._turn_response(turn), "prompt": input_row["body"], "selected_context": json.loads(snapshot["selected_context_json"]), "availability": "claimed", "provider_disabled": True}
        return self._mutate(actor, key, "POST", "/astra/turns/next-claim", {}, command)

    def consumer_heartbeat(self, actor: dict[str, Any], key: str, **payload: Any):
        ConsumerHeartbeat(**payload)
        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._require_agent(live)
            if live["project_id"] is None:
                raise Forbidden("A coordinator credential must be scoped to one project.")
            now = utc_now()
            conn.execute("""INSERT INTO astra_consumer_health
                (agent_principal_id,org_id,project_id,credential_id,heartbeat_at,last_outcome,updated_at)
                VALUES (?,?,?,?,?,'idle',?) ON CONFLICT(agent_principal_id) DO UPDATE SET
                credential_id=excluded.credential_id,heartbeat_at=excluded.heartbeat_at,
                last_outcome='idle',updated_at=excluded.updated_at""", (live["id"], live["org_id"], live["project_id"], live["credential_id"], now, now))
            return 200, {"availability": "available", "health": "healthy", "provider_disabled": True, "heartbeat_at": now}
        return self._mutate(actor, key, "POST", "/astra/consumer/heartbeat", {}, command)

    def consumer_status(self, actor: dict[str, Any], project_id: str) -> dict[str, Any]:
        conn = _connect(self.store.database_path)
        try:
            live = self.store._live_principal(conn, actor)
            self.store._project(conn, live, project_id)
            # A durable heartbeat is not proof that the credential which made it
            # is still valid. Join the exact recorded credential and principal so
            # a revoked or expired consumer cannot be displayed as available.
            rows = conn.execute(
                """SELECT h.heartbeat_at,h.last_outcome,c.expires_at,c.revoked_at,
                p.revoked_at AS principal_revoked_at
                FROM astra_consumer_health h
                JOIN credentials c ON c.id=h.credential_id
                JOIN principals p ON p.id=h.agent_principal_id
                WHERE h.org_id=? AND h.project_id=?
                ORDER BY h.heartbeat_at DESC,h.agent_principal_id DESC LIMIT 100""",
                (live["org_id"], project_id),
            ).fetchall()
            now = datetime.now(UTC)
            observations: list[dict[str, Any]] = []
            for row in rows:
                freshness = "unknown"
                try:
                    fresh = datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00")) > now - timedelta(seconds=_CONSUMER_HEALTH_SECONDS)
                    freshness = "fresh" if fresh else "stale"
                except (AttributeError, ValueError):
                    fresh = False
                credential_status = "active"
                if row["principal_revoked_at"] is not None or row["revoked_at"] is not None:
                    credential_status = "revoked"
                elif row["expires_at"] is not None:
                    try:
                        if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) <= now:
                            credential_status = "expired"
                    except ValueError:
                        credential_status = "unknown"
                observations.append({
                    "heartbeat_at": row["heartbeat_at"], "last_outcome": row["last_outcome"],
                    "freshness": freshness, "credential_status": credential_status,
                    "credential_expires_at": row["expires_at"],
                })
            usable = [item for item in observations if item["freshness"] == "fresh" and item["credential_status"] == "active"]
            latest = observations[0] if observations else None
            if usable:
                availability, health = "available", "healthy"
            elif latest is None:
                availability, health = "unavailable", "unavailable"
            elif latest["credential_status"] in {"revoked", "expired"}:
                availability, health = "unavailable", latest["credential_status"]
            elif latest["freshness"] == "stale":
                availability, health = "unavailable", "stale"
            else:
                availability, health = "unavailable", "unknown"
            return {
                "availability": availability, "health": health,
                "heartbeat_at": latest["heartbeat_at"] if latest else None,
                "freshness": latest["freshness"] if latest else "unknown",
                "credential_status": latest["credential_status"] if latest else "not_recorded",
                "credential_expires_at": latest["credential_expires_at"] if latest else None,
                "last_outcome": latest["last_outcome"] if latest else None,
                "observed_consumers": len(observations),
                "provider_disabled": True, "provider_enabled": False,
            }
        finally:
            conn.close()

    def complete_turn(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = CompleteCoordinatorTurn(**payload)
        payload = body.model_dump(mode="json")

        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            self._require_agent(live)
            turn = self._turn(conn, live, turn_id)
            if conn.execute("SELECT 1 FROM astra_status_discussions WHERE turn_id=?", (turn_id,)).fetchone():
                raise Conflict("Explain status turns require the mediated status protocol.")
            if turn["state"] != "claimed" or turn["claimed_by_principal_id"] != live["id"] or turn["revision"] != body.expected_revision or not self._lease_is_live(turn):
                raise Conflict("Astra coordinator turn is no longer completable.")
            self._fresh_context(conn, turn)
            now, message_revision = utc_now(), conn.execute("SELECT revision FROM astra_project_conversations WHERE project_id=?", (turn["project_id"],)).fetchone()["revision"] + 1
            message_id, proposal_id = "astra_message_" + uuid.uuid4().hex, "astra_proposal_" + uuid.uuid4().hex
            completion = f"Status: {' | '.join(body.status)}\nRecap: {body.recap}\nProposal: {body.proposal}\nUnsupported: approval, grant, dispatch, tool, shell, skill_loading."
            conn.execute("INSERT INTO astra_conversation_messages VALUES (?,?,?,?,?,?,?,?,?)", (message_id, turn["project_id"], message_revision, "coordinator", completion, turn_id, turn["context_snapshot_id"], now, live["id"]))
            design = body.design.model_dump(mode="json") if body.design is not None else None
            conn.execute("""INSERT INTO astra_proposals
                (id,org_id,project_id,conversation_revision,turn_id,proposal_type,body,recap,
                 status_json,provider_disabled,authority_effect,design_json,created_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (proposal_id, live["org_id"], turn["project_id"], message_revision, turn_id, body.proposal_type, body.proposal, body.recap, json.dumps(body.status, separators=(",", ":")), 1, "none", json.dumps(design, sort_keys=True, separators=(",", ":")) if design is not None else None, now, live["id"]))
            conn.execute("UPDATE astra_project_conversations SET revision=? WHERE project_id=?", (message_revision, turn["project_id"]))
            conn.execute("UPDATE astra_coordinator_turns SET state='completed',revision=revision+1,completion_message_id=?,proposal_id=?,claim_expires_at=NULL,updated_at=? WHERE id=?", (message_id, proposal_id, now, turn_id))
            turn.update(state="completed", revision=turn["revision"] + 1, completion_message_id=message_id, proposal_id=proposal_id, claim_expires_at=None, updated_at=now)
            self.store._event(conn, live, turn["project_id"], None, "astra_coordinator_turn_completed", turn["revision"], {"turn_id": turn_id, "proposal_id": proposal_id, "provider_disabled": True, "authority_effect": "none"})
            return 200, {"turn": self._turn_response(turn), "proposal": self._proposal_response({"id": proposal_id, "project_id": turn["project_id"], "conversation_revision": message_revision, "turn_id": turn_id, "proposal_type": body.proposal_type, "body": body.proposal, "recap": body.recap, "status_json": json.dumps(body.status), "provider_disabled": 1, "authority_effect": "none", "design_json": json.dumps(design) if design is not None else None, "context_digest": _digest(json.loads(conn.execute("SELECT selected_context_json FROM astra_selected_context_snapshots WHERE id=?", (turn["context_snapshot_id"],)).fetchone()["selected_context_json"])), "created_at": now})}

        return self._mutate(actor, key, "POST", f"/astra/turns/{turn_id}/complete", payload, command)

    def fail_turn(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = FailCoordinatorTurn(**payload)
        return self._settle_terminal(actor, key, turn_id, body.expected_revision, "failed", body.error_code)

    def cancel_turn(self, actor: dict[str, Any], key: str, turn_id: str, **payload: Any):
        body = CancelCoordinatorTurn(**payload)
        return self._settle_terminal(actor, key, turn_id, body.expected_revision, "cancelled", None)

    def _settle_terminal(self, actor: dict[str, Any], key: str, turn_id: str, expected_revision: int, state: str, error_code: str | None):
        payload = {"expected_revision": expected_revision, "state": state, "error_code": error_code}

        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            if state == "cancelled":
                self._require_human(live)
            else:
                self._require_agent(live)
            turn = self._turn(conn, live, turn_id)
            if conn.execute("SELECT 1 FROM astra_status_discussions WHERE turn_id=?", (turn_id,)).fetchone():
                raise Conflict("Explain status turns require the mediated status protocol.")
            permitted = turn["state"] == "claimed" and turn["claimed_by_principal_id"] == live["id"] and self._lease_is_live(turn) if state == "failed" else turn["state"] in {"requested", "claimed"}
            if not permitted or turn["revision"] != expected_revision:
                raise Conflict("Astra coordinator turn is no longer mutable.")
            now = utc_now()
            conn.execute("UPDATE astra_coordinator_turns SET state=?,revision=revision+1,error_code=?,claim_expires_at=NULL,updated_at=? WHERE id=?", (state, error_code, now, turn_id))
            turn.update(state=state, revision=turn["revision"] + 1, error_code=error_code, claim_expires_at=None, updated_at=now)
            self.store._event(conn, live, turn["project_id"], None, f"astra_coordinator_turn_{state}", turn["revision"], {"turn_id": turn_id, "error_code": error_code, "provider_disabled": True})
            return 200, self._turn_response(turn)

        return self._mutate(actor, key, "POST", f"/astra/turns/{turn_id}/{state}", payload, command)

    def get_conversation(self, actor: dict[str, Any], project_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if not 0 <= offset <= 100_000 or not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds.")
        conn = _connect(self.store.database_path)
        try:
            live = self.store._live_principal(conn, actor)
            self.store._project(conn, live, project_id)
            conversation = conn.execute("SELECT * FROM astra_project_conversations WHERE project_id=? AND org_id=?", (project_id, live["org_id"])).fetchone()
            if conversation is None:
                return {"conversation": None, "messages": []}
            messages = conn.execute("SELECT * FROM astra_conversation_messages WHERE project_id=? ORDER BY conversation_revision LIMIT ? OFFSET ?", (project_id, limit, offset)).fetchall()
            return {"conversation": self._conversation_response(conversation), "messages": [self._message_response(row) for row in messages]}
        finally:
            conn.close()

    def get_turn(self, actor: dict[str, Any], turn_id: str) -> dict[str, Any]:
        conn = _connect(self.store.database_path)
        try:
            return self._turn(conn, self.store._live_principal(conn, actor), turn_id) | {}
        finally:
            conn.close()

    def list_proposals(self, actor: dict[str, Any], project_id: str, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        if not 0 <= offset <= 100_000 or not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds.")
        conn = _connect(self.store.database_path)
        try:
            live = self.store._live_principal(conn, actor)
            self.store._project(conn, live, project_id)
            rows = conn.execute("""SELECT p.*,s.selected_context_digest AS context_digest,
                r.experiment_id AS registered_experiment_id,r.design_revision AS registered_design_revision,
                r.created_at AS registration_created_at
                FROM astra_proposals p
                JOIN astra_coordinator_turns t ON t.id=p.turn_id
                JOIN astra_selected_context_snapshots s ON s.id=t.context_snapshot_id
                LEFT JOIN astra_proposal_design_registrations r ON r.proposal_id=p.id
                WHERE p.org_id=? AND p.project_id=? ORDER BY p.created_at,p.id LIMIT ? OFFSET ?""", (live["org_id"], project_id, limit, offset)).fetchall()
            return [self._proposal_response(row) for row in rows]
        finally:
            conn.close()

    def register_proposal_design(self, actor: dict[str, Any], key: str, proposal_id: str, **payload: Any):
        """Atomically turn one reviewed immutable proposal into one registered design.

        This is deliberately not an approval or admission: no task, grant, lease,
        dispatch, operation, result, or conclusion is created here.
        """
        body = RegisterProposalDesign(**payload)
        payload = body.model_dump(mode="json")

        def command(conn: sqlite3.Connection, live: dict[str, Any]):
            from .experiments import ExperimentStore
            from .experiments_models import CreateExperiment

            self._require_human(live)
            row = conn.execute("""SELECT p.*,s.selected_context_json,s.selected_context_digest
                FROM astra_proposals p JOIN astra_coordinator_turns t ON t.id=p.turn_id
                JOIN astra_selected_context_snapshots s ON s.id=t.context_snapshot_id
                WHERE p.id=? AND p.org_id=? AND (? IS NULL OR p.project_id=?)""",
                (proposal_id, live["org_id"], live["project_id"], live["project_id"])).fetchone()
            if row is None:
                raise NotFound("Astra proposal was not found.")
            proposal = dict(row)
            if proposal["design_json"] is None:
                raise Conflict("Astra proposal has no complete experiment design to register.")
            if proposal["selected_context_digest"] != body.expected_context_digest:
                raise Conflict("Astra proposal context digest is stale.")
            refs = json.loads(proposal["selected_context_json"])
            self._validate_context(conn, live, proposal["project_id"], refs)
            try:
                design = CreateExperiment.model_validate(json.loads(proposal["design_json"])).model_dump(mode="json")
            except Exception as error:
                raise Conflict("Astra proposal design is malformed.") from error
            existing = conn.execute("SELECT experiment_id FROM astra_proposal_design_registrations WHERE proposal_id=?", (proposal_id,)).fetchone()
            if existing is not None:
                raise Conflict("Astra proposal design has already been registered.")
            records = ExperimentStore(self.store.database_path.parent)
            records._project(conn, live, proposal["project_id"])
            records._validate_variant_execution_plans(conn, live, proposal["project_id"], design["variants"])
            now, experiment_id = utc_now(), "experiment_" + uuid.uuid4().hex
            experiment = {"id": experiment_id, "project_id": proposal["project_id"], "goal": design["goal"], "success_criterion": design["success_criterion"], "current_design_revision": 1, "created_at": now, "updated_at": now, "created_by_principal_id": live["id"]}
            conn.execute("""INSERT INTO research_experiments
                (id,project_id,goal,success_criterion,current_design_revision,created_at,updated_at,created_by_principal_id)
                VALUES (:id,:project_id,:goal,:success_criterion,:current_design_revision,:created_at,:updated_at,:created_by_principal_id)""", experiment)
            records._insert_design(conn, live, experiment, 1, design["protocol"], design["variants"], now)
            conn.execute("INSERT INTO astra_proposal_design_registrations VALUES (?,?,?,?,?,?,?,?)", (proposal_id, live["org_id"], proposal["project_id"], experiment_id, 1, body.expected_context_digest, now, live["id"]))
            records._event(conn, live, experiment, "experiment.design_registered", 1, {"experiment_id": experiment_id, "design_revision": 1, "variant_kinds": ["baseline", "intervention"], "astra_proposal_id": proposal_id})
            self.store._event(conn, live, proposal["project_id"], None, "astra_proposal_design_registered", 1, {"proposal_id": proposal_id, "experiment_id": experiment_id, "design_revision": 1, "authority_effect": "none", "execution_authorized": False})
            return 201, {"registration": {"proposal_id": proposal_id, "experiment_id": experiment_id, "design_revision": 1, "context_digest": body.expected_context_digest, "created_at": now}, "experiment": records._experiment_response(conn, experiment), "execution_authorized": False}

        return self._mutate(actor, key, "POST", f"/astra/proposals/{proposal_id}/register-design", payload, command)
