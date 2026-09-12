# ruff: noqa: E501
"""Durable, tenant-scoped conversations and human decision inbox records."""

# fmt: off

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .interaction_models import (
    AcknowledgeHarnessDelivery,
    CancelHarnessDelivery,
    ClaimHarnessDelivery,
    CreateDecision,
    CreateMessage,
    CreateThread,
    EnqueueHarnessDelivery,
    ReportHarnessDelivery,
    ResolveDecision,
)
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
        """CREATE TABLE IF NOT EXISTS interaction_message_deliveries (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE REFERENCES interaction_messages(id),
            org_id TEXT NOT NULL REFERENCES organizations(id), project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT NOT NULL REFERENCES tasks(id),
            admitted_attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
            attempt_revision INTEGER NOT NULL, context_revision INTEGER NOT NULL,
            epoch INTEGER NOT NULL, worker_principal_id TEXT NOT NULL REFERENCES principals(id),
            harness_instance_id TEXT NOT NULL, upstream_session_id TEXT NOT NULL,
            message_digest TEXT NOT NULL, context_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('queued','delivered','acknowledged','uncertain','failed','cancelled')),
            claimed_by_principal_id TEXT REFERENCES principals(id), upstream_message_id TEXT,
            upstream_readback_digest TEXT, error_code TEXT, revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS interaction_message_deliveries_by_worker
        ON interaction_message_deliveries(worker_principal_id, state, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS interaction_message_delivery_states (
            delivery_id TEXT NOT NULL REFERENCES interaction_message_deliveries(id),
            sequence INTEGER NOT NULL, state TEXT NOT NULL,
            context_digest TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
            error_code TEXT, recorded_at TEXT NOT NULL,
            PRIMARY KEY (delivery_id, sequence)
        )""",
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
        *,
        audience: str = "control",
    ):
        return self.store._mutate(
            principal, key, method, path, digest, callback, audience=audience
        )

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

    @staticmethod
    def _delivery_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "delivery_id": value["id"], "state": value["state"], "revision": value["revision"],
            "admitted_attempt_id": value["admitted_attempt_id"],
            "attempt_revision": value["attempt_revision"], "context_revision": value["context_revision"],
            "epoch": value["epoch"], "harness_instance_id": value["harness_instance_id"],
            "upstream_session_id": value["upstream_session_id"], "upstream_message_id": value["upstream_message_id"],
            "upstream_readback_digest": value["upstream_readback_digest"],
            "context_digest": value["context_digest"], "error_code": value["error_code"],
            "created_at": value["created_at"], "updated_at": value["updated_at"],
        }

    @staticmethod
    def _require_human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("Only an authenticated human can enqueue or cancel a harness delivery.")

    @staticmethod
    def _append_delivery_state(
        conn: sqlite3.Connection, delivery: dict[str, Any], state: str, actor: dict[str, Any], error_code: str | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        revision = delivery["revision"] + 1
        conn.execute(
            """UPDATE interaction_message_deliveries SET state=?,revision=?,updated_at=?,error_code=?,
            claimed_by_principal_id=CASE WHEN ?='delivered' THEN ? ELSE claimed_by_principal_id END WHERE id=?""",
            (state, revision, now, error_code, state, actor["id"], delivery["id"]),
        )
        conn.execute(
            """INSERT INTO interaction_message_delivery_states
            (delivery_id,sequence,state,context_digest,actor_principal_id,error_code,recorded_at)
            VALUES (?,?,?,?,?,?,?)""",
            (delivery["id"], revision, state, delivery["context_digest"], actor["id"], error_code, now),
        )
        delivery.update(state=state, revision=revision, updated_at=now, error_code=error_code)
        return delivery

    def enqueue_harness_delivery(self, actor, key, **payload):
        body = EnqueueHarnessDelivery(**payload)
        payload = body.model_dump(mode="json")

        def command(conn, live):
            self._require_human(live)
            thread = self._thread(conn, live, body.thread_id)
            attempt_row = conn.execute(
                """SELECT a.*,e.project_id FROM research_experiment_admitted_attempts a
                JOIN research_experiments e ON e.id=a.experiment_id
                WHERE a.id=? AND e.project_id=?""",
                (body.admitted_attempt_id, thread["project_id"]),
            ).fetchone()
            if attempt_row is None or attempt_row["revision"] != body.expected_attempt_revision:
                raise Conflict("Admitted attempt is missing or its revision is stale.")
            attempt = dict(attempt_row)
            operation = conn.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (attempt["operation_id"],)
            ).fetchone()
            if (
                operation is None or operation["epoch"] != body.expected_epoch
                or operation["worker_principal_id"] != body.worker_principal_id
                or operation["desired_state"] != "admitted"
            ):
                raise Conflict("Admitted attempt has no current worker/epoch delivery authority.")
            task_id = operation["task_id"]
            if thread["task_id"] is not None and thread["task_id"] != task_id:
                raise Conflict("Thread task does not match the admitted attempt.")
            worker = conn.execute(
                """SELECT p.id FROM principals p JOIN credentials c ON c.principal_id=p.id
                WHERE p.id=? AND p.org_id=? AND p.revoked_at IS NULL AND c.kind='worker'
                AND c.audience='worker' AND c.revoked_at IS NULL
                AND (c.project_id IS NULL OR c.project_id=?)""",
                (body.worker_principal_id, live["org_id"], thread["project_id"]),
            ).fetchone()
            if worker is None:
                raise NotFound("Designated worker was not found.")
            if body.reply_to is not None:
                reply = conn.execute("SELECT thread_id FROM interaction_messages WHERE id=?", (body.reply_to,)).fetchone()
                if reply is None or reply["thread_id"] != body.thread_id:
                    raise Conflict("Reply must belong to the same thread.")
            message_id, delivery_id, now = str(uuid.uuid4()), "delivery_" + uuid.uuid4().hex, utc_now()
            message_digest = _digest({"intent": body.intent, "body": body.body, "reply_to": body.reply_to})
            context_digest = _digest({
                "message_digest": message_digest, "admitted_attempt_id": body.admitted_attempt_id,
                "attempt_revision": body.expected_attempt_revision, "context_revision": body.context_revision,
                "epoch": body.expected_epoch, "harness_instance_id": body.harness_instance_id,
                "upstream_session_id": body.upstream_session_id,
            })
            conn.execute("INSERT INTO interaction_messages VALUES (?,?,?,?,?,?,?)", (message_id, body.thread_id, live["id"], body.intent, body.body, body.reply_to, now))
            row = {
                "id": delivery_id, "message_id": message_id, "org_id": live["org_id"], "project_id": thread["project_id"], "task_id": task_id,
                "admitted_attempt_id": body.admitted_attempt_id, "attempt_revision": body.expected_attempt_revision,
                "context_revision": body.context_revision, "epoch": body.expected_epoch, "worker_principal_id": body.worker_principal_id,
                "harness_instance_id": body.harness_instance_id, "upstream_session_id": body.upstream_session_id,
                "message_digest": message_digest, "context_digest": context_digest, "state": "queued", "claimed_by_principal_id": None,
                "upstream_message_id": None, "upstream_readback_digest": None, "error_code": None, "revision": 1, "created_at": now, "updated_at": now,
            }
            conn.execute("""INSERT INTO interaction_message_deliveries VALUES
            (:id,:message_id,:org_id,:project_id,:task_id,:admitted_attempt_id,:attempt_revision,:context_revision,:epoch,:worker_principal_id,:harness_instance_id,:upstream_session_id,:message_digest,:context_digest,:state,:claimed_by_principal_id,:upstream_message_id,:upstream_readback_digest,:error_code,:revision,:created_at,:updated_at)""", row)
            conn.execute("INSERT INTO interaction_message_delivery_states VALUES (?,?,?,?,?,?,?)", (delivery_id, 1, "queued", context_digest, live["id"], None, now))
            self.store._event(conn, live, thread["project_id"], task_id, "interaction_message_delivery_queued", body.context_revision, {"delivery_id": delivery_id, "context_digest": context_digest, "harness_instance_id": body.harness_instance_id})
            return 201, {"id": message_id, "thread_id": body.thread_id, "intent": body.intent, "body": body.body, "reply_to": body.reply_to, "sender_principal_id": live["id"], "created_at": now, "delivery": self._delivery_response(row)}
        return self._mutate(actor, key, "POST", "/interaction/deliveries", _digest(payload), command)

    def claim_harness_delivery(self, actor, key, delivery_id, **payload):
        body = ClaimHarnessDelivery(**payload)
        def command(conn, live):
            row = conn.execute("SELECT d.*,m.body,m.intent,m.reply_to FROM interaction_message_deliveries d JOIN interaction_messages m ON m.id=d.message_id WHERE d.id=? AND d.org_id=?", (delivery_id, live["org_id"])).fetchone()
            if row is None or row["worker_principal_id"] != live["id"]:
                raise NotFound("Harness delivery was not found.")
            delivery = dict(row)
            if delivery["epoch"] != body.expected_epoch or delivery["state"] in {"cancelled", "acknowledged", "uncertain", "failed"}:
                raise Conflict("Harness delivery is no longer claimable.")
            attempt = conn.execute(
                "SELECT revision,operation_id FROM research_experiment_admitted_attempts WHERE id=?",
                (delivery["admitted_attempt_id"],),
            ).fetchone()
            operation = conn.execute("SELECT operation_id,desired_state,epoch,worker_principal_id FROM external_operations WHERE operation_id=(SELECT operation_id FROM research_experiment_admitted_attempts WHERE id=?)", (delivery["admitted_attempt_id"],)).fetchone()
            if attempt is None or attempt["revision"] != delivery["attempt_revision"] or operation is None or operation["operation_id"] != attempt["operation_id"] or operation["desired_state"] != "admitted" or operation["epoch"] != delivery["epoch"] or operation["worker_principal_id"] != live["id"]:
                raise Conflict("Harness delivery authority is no longer current.")
            if delivery["state"] == "queued":
                delivery = self._append_delivery_state(conn, delivery, "delivered", live)
                self.store._event(conn, live, delivery["project_id"], delivery["task_id"], "interaction_message_delivery_delivered", delivery["context_revision"], {"delivery_id": delivery_id, "context_digest": delivery["context_digest"]})
            return 200, {"delivery": self._delivery_response(delivery) | {"organization_id": delivery["org_id"], "project_id": delivery["project_id"], "task_id": delivery["task_id"], "operation_id": operation["operation_id"], "worker_principal_id": live["id"], "body": row["body"], "intent": row["intent"], "reply_to": row["reply_to"]}}
        return self._mutate(actor, key, "POST", f"/worker/interaction-deliveries/{delivery_id}/claim", _digest(body.model_dump(mode="json")), command, audience="worker")

    def acknowledge_harness_delivery(self, actor, key, delivery_id, **payload):
        body = AcknowledgeHarnessDelivery(**payload)
        return self._settle_harness_delivery(actor, key, delivery_id, body, "acknowledged")

    def report_harness_delivery(self, actor, key, delivery_id, **payload):
        body = ReportHarnessDelivery(**payload)
        return self._settle_harness_delivery(actor, key, delivery_id, body, body.outcome)

    def _settle_harness_delivery(self, actor, key, delivery_id, body, state):
        def command(conn, live):
            row = conn.execute("SELECT * FROM interaction_message_deliveries WHERE id=? AND org_id=?", (delivery_id, live["org_id"])).fetchone()
            if row is None or row["worker_principal_id"] != live["id"]:
                raise NotFound("Harness delivery was not found.")
            delivery = dict(row)
            if delivery["context_digest"] != body.context_digest:
                raise Conflict("Harness delivery context digest does not match.")
            if delivery["state"] == state and delivery["revision"] == body.expected_revision:
                return 200, self._delivery_response(delivery)
            if delivery["state"] != "delivered" or delivery["revision"] != body.expected_revision:
                raise Conflict("Harness delivery state or revision is stale.")
            error_code = getattr(body, "error_code", None)
            delivery = self._append_delivery_state(conn, delivery, state, live, error_code)
            if state == "acknowledged":
                conn.execute("UPDATE interaction_message_deliveries SET upstream_message_id=?,upstream_readback_digest=? WHERE id=?", (body.upstream_message_id, body.upstream_readback_digest, delivery_id))
                delivery.update(upstream_message_id=body.upstream_message_id, upstream_readback_digest=body.upstream_readback_digest)
            self.store._event(conn, live, delivery["project_id"], delivery["task_id"], f"interaction_message_delivery_{state}", delivery["context_revision"], {"delivery_id": delivery_id, "context_digest": delivery["context_digest"], "error_code": error_code})
            return 200, self._delivery_response(delivery)
        return self._mutate(actor, key, "POST", f"/worker/interaction-deliveries/{delivery_id}/{'ack' if state == 'acknowledged' else 'report'}", _digest(body.model_dump(mode="json")), command, audience="worker")

    def cancel_harness_delivery(self, actor, key, delivery_id, **payload):
        body = CancelHarnessDelivery(**payload)

        def command(conn, live):
            self._require_human(live)
            row = conn.execute("SELECT * FROM interaction_message_deliveries WHERE id=? AND org_id=?", (delivery_id, live["org_id"])).fetchone()
            if row is None or (live["project_id"] is not None and row["project_id"] != live["project_id"]):
                raise NotFound("Harness delivery was not found.")
            delivery = dict(row)
            if delivery["state"] == "cancelled" and delivery["revision"] == body.expected_revision:
                return 200, self._delivery_response(delivery)
            if delivery["state"] not in {"queued", "delivered"} or delivery["revision"] != body.expected_revision:
                raise Conflict("Harness delivery state or revision is stale.")
            # This control-plane fence never waits for or calls OpenCode.
            delivery = self._append_delivery_state(conn, delivery, "cancelled", live)
            self.store._event(conn, live, delivery["project_id"], delivery["task_id"], "interaction_message_delivery_cancelled", delivery["context_revision"], {"delivery_id": delivery_id, "context_digest": delivery["context_digest"]})
            return 200, self._delivery_response(delivery)

        return self._mutate(actor, key, "POST", f"/interaction/deliveries/{delivery_id}/cancel", _digest(body.model_dump(mode="json")), command)

    def list_messages(self, actor, thread_id, offset=0, limit=50):
        if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds.")
        conn = _connect(self.store.database_path)
        try:
            actor = self.store._live_principal(conn, actor)
            self._thread(conn, actor, thread_id)
            messages = []
            for row in conn.execute(
                """SELECT m.*,d.id AS delivery_id,d.state AS delivery_state,d.revision AS delivery_revision,
                d.harness_instance_id,d.upstream_session_id,d.upstream_message_id,d.error_code,
                d.context_digest,d.updated_at AS delivery_updated_at
                FROM interaction_messages m LEFT JOIN interaction_message_deliveries d ON d.message_id=m.id
                WHERE m.thread_id=? ORDER BY m.created_at,m.id LIMIT ? OFFSET ?""",
                (thread_id, limit, offset),
            ):
                message = dict(row)
                if message.pop("delivery_id") is None:
                    for key in ("delivery_state", "delivery_revision", "harness_instance_id", "upstream_session_id", "upstream_message_id", "error_code", "context_digest", "delivery_updated_at"):
                        message.pop(key)
                    message.update(delivery_status="queued", delivery=None, delivery_legacy=True)
                else:
                    delivery = {
                        "delivery_id": row["delivery_id"], "state": row["delivery_state"],
                        "revision": row["delivery_revision"], "harness_instance_id": row["harness_instance_id"],
                        "upstream_session_id": row["upstream_session_id"], "upstream_message_id": row["upstream_message_id"],
                        "error_code": row["error_code"], "context_digest": row["context_digest"],
                        "updated_at": row["delivery_updated_at"],
                    }
                    for key in ("delivery_state", "delivery_revision", "harness_instance_id", "upstream_session_id", "upstream_message_id", "error_code", "context_digest", "delivery_updated_at"):
                        message.pop(key)
                    message.update(delivery_status=delivery["state"], delivery=delivery, delivery_legacy=False)
                messages.append(message)
            return messages
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
