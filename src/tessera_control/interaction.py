"""Durable, tenant-scoped conversations and human decision inbox records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .interaction_models import CreateDecision, CreateMessage, CreateThread, ResolveDecision
from .store import Conflict, Forbidden, NotFound, Store, _connect, utc_now


def create_interaction_schema(connection: sqlite3.Connection) -> None:
    """Install interaction tables on an already initialized Tessera database."""
    statements = (
        """CREATE TABLE IF NOT EXISTS interaction_threads (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id), task_id TEXT REFERENCES tasks(id),
            title TEXT NOT NULL, created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        )""",
        """CREATE INDEX IF NOT EXISTS interaction_threads_by_project
        ON interaction_threads(project_id, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS interaction_messages (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES interaction_threads(id),
            sender_principal_id TEXT NOT NULL REFERENCES principals(id), intent TEXT NOT NULL
                CHECK (intent IN ('question','direction','status','evidence')),
            body TEXT NOT NULL CHECK (length(body) <= 8000),
            reply_to TEXT REFERENCES interaction_messages(id),
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS interaction_messages_by_thread
        ON interaction_messages(thread_id, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS decision_requests (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id), task_id TEXT REFERENCES tasks(id),
            kind TEXT NOT NULL CHECK (kind IN ('direction','spending','access','publication')),
            scope_json TEXT NOT NULL, scope_digest TEXT NOT NULL, expected_task_revision INTEGER,
            reviewer_principal_id TEXT NOT NULL REFERENCES principals(id),
            requester_principal_id TEXT NOT NULL REFERENCES principals(id),
            status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired')),
            revision INTEGER NOT NULL, expires_at TEXT NOT NULL,
            priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
            created_at TEXT NOT NULL, resolved_at TEXT,
            resolved_by_principal_id TEXT REFERENCES principals(id), rationale TEXT
        )""",
        """CREATE INDEX IF NOT EXISTS decision_requests_pending
        ON decision_requests(project_id, status, priority, created_at)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS decision_requests_pending_dedup
        ON decision_requests(project_id, kind, scope_digest, task_id, expected_task_revision)
        WHERE status = 'pending'""",
    )
    for statement in statements:
        connection.execute(statement)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class InteractionStore:
    """Composition around :class:`Store`; owns no authentication or task state."""

    def __init__(self, store: Store):
        self.store = store

    def _mutate(
        self,
        principal: dict[str, Any],
        key: str,
        method: str,
        path: str,
        digest: str,
        callback: Any,
    ):
        return self.store._mutate(principal, key, method, path, digest, callback)

    @staticmethod
    def _decision_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        decision = dict(row)
        return {
            "id": decision["id"],
            "project_id": decision["project_id"],
            "task_id": decision["task_id"],
            "kind": decision["kind"],
            "scope": json.loads(decision["scope_json"]),
            "expires_at": decision["expires_at"],
            "priority": decision["priority"],
            "expected_task_revision": decision["expected_task_revision"],
            "reviewer_principal_id": decision["reviewer_principal_id"],
            "requester_principal_id": decision["requester_principal_id"],
            "status": decision["status"],
            "revision": decision["revision"],
            "created_at": decision["created_at"],
            "resolved_at": decision["resolved_at"],
            "resolved_by_principal_id": decision["resolved_by_principal_id"],
            "rationale": decision["rationale"],
        }

    @staticmethod
    def _thread(conn: sqlite3.Connection, actor: dict[str, Any], thread_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM interaction_threads WHERE id = ? AND org_id = ?",
            (thread_id, actor["org_id"]),
        ).fetchone()
        if row is None or (
            actor["project_id"] is not None and row["project_id"] != actor["project_id"]
        ):
            raise NotFound("Thread was not found.")
        return dict(row)

    def create_thread(self, actor, key, project_id, title, task_id=None):
        request = CreateThread(project_id=project_id, title=title, task_id=task_id)
        project_id, title, task_id = request.project_id, request.title, request.task_id
        payload = {"project_id": project_id, "title": title, "task_id": task_id}

        def command(conn, live):
            self.store._project(conn, live, project_id)
            if (
                task_id is not None
                and self.store._task(conn, live, task_id)["project_id"] != project_id
            ):
                raise NotFound("Task was not found.")
            ident, now = str(uuid.uuid4()), utc_now()
            conn.execute(
                "INSERT INTO interaction_threads VALUES (?,?,?,?,?,?,?)",
                (ident, live["org_id"], project_id, task_id, title, now, live["id"]),
            )
            self.store._event(
                conn,
                live,
                project_id,
                task_id,
                "interaction_thread_created",
                0,
                {"thread_id": ident},
            )
            return 201, {
                "id": ident,
                **payload,
                "created_at": now,
                "created_by_principal_id": live["id"],
            }

        return self._mutate(actor, key, "POST", "/interaction/threads", _digest(payload), command)

    def send_message(self, actor, key, thread_id, intent, body, reply_to=None):
        request = CreateMessage(thread_id=thread_id, intent=intent, body=body, reply_to=reply_to)
        thread_id, intent, body, reply_to = (
            request.thread_id,
            request.intent,
            request.body,
            request.reply_to,
        )
        payload = {"thread_id": thread_id, "intent": intent, "body": body, "reply_to": reply_to}

        def command(conn, live):
            thread = self._thread(conn, live, thread_id)
            if reply_to is not None:
                reply = conn.execute(
                    "SELECT thread_id FROM interaction_messages WHERE id = ?", (reply_to,)
                ).fetchone()
                if reply is None or reply["thread_id"] != thread_id:
                    raise Conflict("Reply must belong to the same thread.")
            ident, now = str(uuid.uuid4()), utc_now()
            conn.execute(
                "INSERT INTO interaction_messages VALUES (?,?,?,?,?,?,?)",
                (ident, thread_id, live["id"], intent, body, reply_to, now),
            )
            self.store._event(
                conn,
                live,
                thread["project_id"],
                thread["task_id"],
                "interaction_message_queued",
                0,
                {"message_id": ident, "intent": intent},
            )
            return 201, {
                "id": ident,
                **payload,
                "sender_principal_id": live["id"],
                "created_at": now,
                "delivery_status": "queued",
            }

        return self._mutate(
            actor,
            key,
            "POST",
            f"/interaction/threads/{thread_id}/messages",
            _digest(payload),
            command,
        )

    def list_messages(self, actor, thread_id, offset=0, limit=50):
        if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds.")
        conn = _connect(self.store.database_path)
        try:
            actor = self.store._live_principal(conn, actor)
            self._thread(conn, actor, thread_id)
            return [
                {**dict(row), "delivery_status": "queued"}
                for row in conn.execute(
                    """SELECT * FROM interaction_messages WHERE thread_id = ?
                    ORDER BY created_at, id LIMIT ? OFFSET ?""",
                    (thread_id, limit, offset),
                )
            ]
        finally:
            conn.close()

    def list_threads(self, actor, project_id, offset=0, limit=50):
        if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds.")
        conn = _connect(self.store.database_path)
        try:
            actor = self.store._live_principal(conn, actor)
            self.store._project(conn, actor, project_id)
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM interaction_threads
                    WHERE org_id = ? AND project_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?""",
                    (actor["org_id"], project_id, limit, offset),
                )
            ]
        finally:
            conn.close()

    def request_decision(
        self,
        actor,
        key,
        project_id,
        kind,
        scope,
        expires_at,
        priority=50,
        task_id=None,
        expected_task_revision=None,
        reviewer_principal_id=None,
    ):
        request = CreateDecision(
            project_id=project_id,
            kind=kind,
            scope=scope,
            expires_at=expires_at,
            priority=priority,
            task_id=task_id,
            expected_task_revision=expected_task_revision,
            reviewer_principal_id=reviewer_principal_id,
        )
        project_id, kind, scope, expires_at = (
            request.project_id,
            request.kind,
            request.scope,
            request.expires_at,
        )
        priority, task_id = request.priority, request.task_id
        expected_task_revision = request.expected_task_revision
        reviewer_principal_id = request.reviewer_principal_id
        payload = {
            "project_id": project_id,
            "kind": kind,
            "scope": scope,
            "expires_at": expires_at,
            "priority": priority,
            "task_id": task_id,
            "expected_task_revision": expected_task_revision,
            "reviewer_principal_id": reviewer_principal_id,
        }

        def command(conn, live):
            self.store._project(conn, live, project_id)
            task = self.store._task(conn, live, task_id) if task_id else None
            if task and task["project_id"] != project_id:
                raise NotFound("Task was not found.")
            if task and expected_task_revision != task["revision"]:
                raise Conflict("Task revision is stale.")
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise Conflict("expires_at must be an ISO timestamp.") from error
            if expiry.tzinfo is None:
                raise Conflict("expires_at must include a timezone.")
            expiry = expiry.astimezone(UTC)
            now = datetime.now(UTC)
            if expiry <= now or expiry > now + timedelta(hours=24):
                raise Conflict("Decision expiry must be within 24 hours.")
            reviewer = reviewer_principal_id or (live["id"] if live["role"] == "human" else None)
            if reviewer is None:
                raise Forbidden("An explicit human reviewer is required.")
            rp = conn.execute(
                """SELECT p.id, p.org_id, p.role, c.id AS credential_id, c.kind,
                c.audience, c.project_id, c.expires_at, c.revoked_at, c.parent_id
                FROM principals p JOIN credentials c ON c.principal_id = p.id
                WHERE p.id = ? AND p.revoked_at IS NULL""",
                (reviewer,),
            ).fetchone()
            if rp is None:
                raise Forbidden("Reviewer must be a live human in this organization.")
            reviewer_live = Store._credential_principal(conn, rp, "control")
            if reviewer_live["org_id"] != live["org_id"] or reviewer_live["role"] != "human":
                raise Forbidden("Reviewer must be a live human in this organization.")
            if (
                reviewer_live["project_id"] is not None
                and reviewer_live["project_id"] != project_id
            ):
                raise Forbidden("Reviewer is not eligible for this project.")
            pending = conn.execute(
                """SELECT * FROM decision_requests WHERE project_id = ? AND status = 'pending'
                AND kind = ? AND scope_digest = ? AND task_id IS ?
                AND expected_task_revision IS ?""",
                (project_id, kind, _digest(scope), task_id, expected_task_revision),
            ).fetchone()
            if pending:
                return 200, self._decision_response(pending)
            if (
                conn.execute(
                    """SELECT count(*) FROM decision_requests
                    WHERE project_id = ? AND status = 'pending'""",
                    (project_id,),
                ).fetchone()[0]
                >= 50
            ):
                raise Conflict("Decision inbox is full.")
            ident, timestamp = str(uuid.uuid4()), utc_now()
            conn.execute(
                "INSERT INTO decision_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    live["org_id"],
                    project_id,
                    task_id,
                    kind,
                    json.dumps(scope, sort_keys=True, separators=(",", ":")),
                    _digest(scope),
                    expected_task_revision,
                    reviewer,
                    live["id"],
                    "pending",
                    1,
                    expires_at,
                    priority,
                    timestamp,
                    None,
                    None,
                    None,
                ),
            )
            self.store._event(
                conn,
                live,
                project_id,
                task_id,
                "decision_requested",
                expected_task_revision or 0,
                {"decision_id": ident, "kind": kind, "scope_digest": _digest(scope)},
            )
            created = {
                "id": ident,
                "project_id": project_id,
                "task_id": task_id,
                "kind": kind,
                "scope_json": json.dumps(scope, sort_keys=True, separators=(",", ":")),
                "expires_at": expires_at,
                "priority": priority,
                "expected_task_revision": expected_task_revision,
                "reviewer_principal_id": reviewer,
                "requester_principal_id": live["id"],
                "status": "pending",
                "revision": 1,
                "created_at": timestamp,
                "resolved_at": None,
                "resolved_by_principal_id": None,
                "rationale": None,
            }
            return 201, self._decision_response(created)

        return self._mutate(actor, key, "POST", "/interaction/decisions", _digest(payload), command)

    def list_decisions(self, actor, project_id, offset=0, limit=50, status="pending"):
        if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds.")
        conn = _connect(self.store.database_path)
        try:
            actor = self.store._live_principal(conn, actor)
            self.store._project(conn, actor, project_id)
            rows = conn.execute(
                """SELECT * FROM decision_requests
                WHERE org_id = ? AND project_id = ? AND status = ?
                ORDER BY priority DESC, created_at, id LIMIT ? OFFSET ?""",
                (actor["org_id"], project_id, status, limit, offset),
            )
            result = []
            for row in rows:
                result.append(self._decision_response(row))
            return result
        finally:
            conn.close()

    def resolve_decision(
        self, actor, key, decision_id, expected_revision, decision, rationale=None
    ):
        request = ResolveDecision(
            decision_id=decision_id,
            expected_revision=expected_revision,
            decision=decision,
            rationale=rationale,
        )
        decision_id, expected_revision, decision, rationale = (
            request.decision_id,
            request.expected_revision,
            request.decision,
            request.rationale,
        )
        payload = {
            "decision_id": decision_id,
            "expected_revision": expected_revision,
            "decision": decision,
            "rationale": rationale,
        }

        def command(conn, live):
            row = conn.execute(
                "SELECT * FROM decision_requests WHERE id = ? AND org_id = ?",
                (decision_id, live["org_id"]),
            ).fetchone()
            if row is None or (
                live["project_id"] is not None and row["project_id"] != live["project_id"]
            ):
                raise NotFound("Decision was not found.")
            if live["role"] != "human" or (
                live["id"] != row["reviewer_principal_id"] and not self.store._is_root_owner(live)
            ):
                raise Forbidden(
                    "Only the designated human reviewer or local owner may resolve a decision."
                )
            if row["revision"] != expected_revision:
                raise Conflict("Decision revision is stale.")
            if row["status"] != "pending":
                raise Conflict("Decision is no longer pending.")
            expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.astimezone(UTC) <= datetime.now(UTC):
                raise Conflict("Decision has expired.")
            if row["task_id"]:
                task = self.store._task(conn, live, row["task_id"])
                if row["expected_task_revision"] != task["revision"]:
                    raise Conflict("Decision task basis is stale.")
            now = utc_now()
            status = {"approve": "approved", "reject": "rejected"}[decision]
            conn.execute(
                """UPDATE decision_requests SET status = ?, revision = revision + 1,
                resolved_at = ?, resolved_by_principal_id = ?, rationale = ? WHERE id = ?""",
                (status, now, live["id"], rationale, decision_id),
            )
            self.store._event(
                conn,
                live,
                row["project_id"],
                row["task_id"],
                "decision_resolved",
                row["revision"] + 1,
                {
                    "decision_id": decision_id,
                    "decision": status,
                    "scope_digest": row["scope_digest"],
                },
            )
            result = dict(row)
            result.update(
                status=status,
                revision=row["revision"] + 1,
                resolved_at=now,
                resolved_by_principal_id=live["id"],
                rationale=rationale,
            )
            return 200, self._decision_response(result)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/interaction/decisions/{decision_id}/resolve",
            _digest(payload),
            command,
        )

    def expire_decisions(self, actor, key, project_id):
        """Explicitly account for expired pending requests in one transaction."""

        def command(conn, live):
            self.store._project(conn, live, project_id)
            now = datetime.now(UTC)
            rows = conn.execute(
                "SELECT * FROM decision_requests WHERE project_id = ? AND status = 'pending'",
                (project_id,),
            ).fetchall()
            expired = []
            for row in rows:
                expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                if expiry.tzinfo is not None and expiry.astimezone(UTC) <= now:
                    conn.execute(
                        """UPDATE decision_requests SET status = 'expired',
                        revision = revision + 1 WHERE id = ?""",
                        (row["id"],),
                    )
                    expired.append(row["id"])
                    self.store._event(
                        conn,
                        live,
                        project_id,
                        row["task_id"],
                        "decision_expired",
                        row["revision"] + 1,
                        {"decision_id": row["id"], "scope_digest": row["scope_digest"]},
                    )
            return 200, {"expired": expired}

        return self._mutate(
            actor,
            key,
            "POST",
            f"/interaction/projects/{project_id}/decisions/expire",
            _digest({"project_id": project_id}),
            command,
        )
