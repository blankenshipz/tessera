"""Private durable A2A reference ledger; never an execution-authority store."""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class PeerConflict(ValueError):
    pass


class PeerNotFound(ValueError):
    pass


GENERATED_KEYS = frozenset(
    (
        "record_id",
        "collector_sequence",
        "artifact_id",
        "manifest_id",
        "operation_id",
        "task_id",
        "run_id",
        "session_id",
        "harness_instance_id",
        "context_id",
        "recipient_context_id",
        "critic_credential_id",
        "model_id",
        "context_digest",
        "skill_digest",
        "config_digest",
        "content_digest",
        "manifest_digest",
        "source_event_id",
        "source_sequence",
    )
)


RESEARCH_GENERATED_KEYS = frozenset(
    (
        "family",
        "output_id",
        "disclosure_id",
        "actor_id",
        "critic_credential_id",
        "org_id",
        "project_id",
        "model_id",
        "run_id",
        "session_id",
        "harness_instance_id",
        "context_id",
        "context_digest",
        "skill_digest",
        "config_digest",
        "task_id",
        "operation_id",
        "source_event_id",
        "source_sequence",
        "content_digest",
        "registration_grant_id",
        "registration_revision",
        "output_kind",
    )
)


def generated_reference(pins):
    return (
        pins["output_id"] if pins.get("family") == "research-generated-v1" else pins["artifact_id"]
    )


def validate_generated_pins(value):
    """Closed content-free identities, not proof of registration or reasoning."""
    if not isinstance(value, dict):
        raise PeerConflict("closed generated output pins required")
    research = value.get("family") == "research-generated-v1"
    expected = RESEARCH_GENERATED_KEYS if research else GENERATED_KEYS
    if research and ("capture_record_id" in value or "capture_sequence" in value):
        expected = expected | {"capture_record_id", "capture_sequence"}
    if set(value) != expected:
        raise PeerConflict("closed generated output pins required")
    for key, item in value.items():
        if research and key == "operation_id" and item is None:
            valid = True
        elif key == "family":
            valid = item == "research-generated-v1"
        elif key == "output_kind":
            valid = item == "critic_review"
        elif key in {
            "collector_sequence",
            "capture_sequence",
            "source_sequence",
            "registration_revision",
        }:
            valid = type(item) in {int, float} and 1 <= item <= 2**53 - 1 and int(item) == item
        elif key == "content_digest":
            valid = isinstance(item, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", item)
        elif key.endswith("digest"):
            valid = isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item)
        else:
            valid = isinstance(item, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", item
            )
        if not valid:
            raise PeerConflict("invalid generated output pin")
    return value


class A2AStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS peer_tasks (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, agent TEXT NOT NULL,
                    body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS peer_messages (
                    tenant TEXT NOT NULL, agent TEXT NOT NULL, id TEXT NOT NULL,
                    digest TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY(tenant, agent, id));
                CREATE TABLE IF NOT EXISTS peer_events (
                    sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, timestamp TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS peer_reasoning_links (
                    task_id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
                    binding TEXT NOT NULL, state TEXT NOT NULL, runtime_state TEXT,
                    session_id TEXT, message_id TEXT);
                CREATE TABLE IF NOT EXISTS peer_provenance_outbox (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL,
                    delivery_state TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS peer_provenance_delivery (
                    id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
                    status TEXT NOT NULL, code TEXT NOT NULL, status_code INTEGER,
                    sequence INTEGER, attempts INTEGER NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, tenant, agent, task_id):
        row = db.execute(
            "SELECT body FROM peer_tasks WHERE id=? AND tenant=? AND agent=?",
            (task_id, tenant, agent),
        ).fetchone()
        if row is None:
            raise PeerNotFound("task not found")
        return json.loads(row["body"])

    def get(self, tenant, agent, task_id):
        with self.connect() as db:
            return self._read(db, tenant, agent, task_id)

    def list(self, tenant, agent, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM peer_tasks WHERE tenant=? AND agent=? "
                "ORDER BY rowid LIMIT ? OFFSET ?",
                (tenant, agent, limit, offset),
            ).fetchall()
            total = db.execute(
                "SELECT count(*) FROM peer_tasks WHERE tenant=? AND agent=?", (tenant, agent)
            ).fetchone()[0]
            return [json.loads(row["body"]) for row in rows], total

    def send(
        self,
        tenant,
        agent,
        message_id,
        envelope,
        task_id="",
        *,
        provenance_origin=None,
        generated_receipt=None,
    ):
        generated = envelope.get("generated")
        if generated is not None:
            validate_generated_pins(generated)
            if (
                generated_receipt != generated
                or provenance_origin is None
                or envelope["reference"] != generated_reference(generated)
            ):
                raise PeerConflict("registered public output verification required")
        digest = hashlib.sha256(
            json.dumps([task_id, envelope], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT digest,task_id FROM peer_messages WHERE tenant=? AND agent=? AND id=?",
                (tenant, agent, message_id),
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise PeerConflict("message identifier reused with different content")
                task = self._read(db, tenant, agent, previous["task_id"])
                self._peer_origin(task, provenance_origin)
                return task
            now = datetime.now(UTC).isoformat()
            if not task_id:
                if envelope["operation"] != "submit":
                    raise PeerConflict("new tasks require submit")
                task_id = str(uuid.uuid4())
                task = {
                    "id": task_id,
                    "contextId": str(uuid.uuid4()),
                    "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": now},
                    "metadata": {
                        "authority": envelope["authority"],
                        "reference": envelope["reference"],
                    },
                    "history": [],
                }
            else:
                task = self._read(db, tenant, agent, task_id)
                if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
                    raise PeerConflict("terminal task cannot accept new messages")
                if task["metadata"]["authority"] != envelope["authority"]:
                    raise PeerConflict("task authority cannot be replaced")
                if envelope["operation"] != "result":
                    raise PeerConflict("existing task requires result reference")
                task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": now}
                task["artifacts"] = [
                    {
                        "artifactId": str(uuid.uuid4()),
                        "name": "untrusted-peer-result-reference",
                        "parts": [
                            {"data": {"reference": envelope["reference"], "verified": False}}
                        ],
                    }
                ]
            previous_message = task["history"][-1]["messageId"] if task["history"] else None
            self._peer_origin(task, provenance_origin)
            if task["history"] and generated != task["metadata"].get("generated"):
                raise PeerConflict("generated delivery pins cannot be rebound")
            if generated is not None:
                if (
                    task["metadata"].get("generated_delivery_state")
                    == "awaiting_planner_consumption"
                ):
                    raise PeerConflict("registered result already delivered")
                task["metadata"]["generated"] = dict(generated)
                task["metadata"]["authenticated_sender_id"] = agent
                if envelope["operation"] == "result":
                    task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
                    task["metadata"]["generated_delivery_state"] = "awaiting_planner_consumption"
                    task["artifacts"][0]["parts"][0]["data"] = {
                        "reference": generated_reference(generated),
                        "verified": False,
                        "registered_public_output": dict(generated),
                        "attribution": "registered_self_reported_generated_output",
                    }
            task["history"].append(
                {
                    "messageId": message_id,
                    "role": "ROLE_USER",
                    "taskId": task_id,
                    "contextId": task["contextId"],
                    "parts": [{"data": envelope}],
                }
            )
            db.execute(
                "INSERT INTO peer_tasks VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (task_id, tenant, agent, json.dumps(task)),
            )
            db.execute(
                "INSERT INTO peer_messages VALUES (?,?,?,?,?)",
                (tenant, agent, message_id, digest, task_id),
            )
            db.execute(
                "INSERT INTO peer_events(task_id,kind,timestamp) VALUES (?,?,?)",
                (task_id, envelope["operation"], now),
            )
            if provenance_origin:
                self._peer_record(
                    db, task, agent, envelope["operation"], message_id, previous_message
                )
                if envelope["operation"] == "submit":
                    self._peer_record(db, task, agent, "delegation", message_id, message_id)
                else:
                    self._peer_record(db, task, agent, "result-reference", message_id, message_id)
            return task

    def consumption_task(self, tenant, planner_id, task_id):
        """Only enrolled receiving planner can cross the sender-owned task view."""
        with self.connect() as db:
            row = db.execute(
                "SELECT agent FROM peer_tasks WHERE tenant=? AND id=?", (tenant, task_id)
            ).fetchone()
            if row is None:
                raise PeerNotFound("task not found")
            task = self._read(db, tenant, row["agent"], task_id)
            if (
                not task["metadata"].get("generated")
                or task["metadata"].get("peer_provenance_origin", {}).get("receiver_peer_id")
                != planner_id
            ):
                raise PeerNotFound("task not found")
            return task

    def consume(self, tenant, planner_id, message_id, task_id, envelope, *, generated_receipt):
        """Explicit authenticated planner ACK, not implicit model consumption."""
        task = self.consumption_task(tenant, planner_id, task_id)
        sender = task["metadata"]["authenticated_sender_id"]
        generated = validate_generated_pins(envelope.get("generated"))
        if (
            envelope["operation"] != "consume"
            or envelope["reference"] != generated_reference(generated)
            or generated_receipt != generated
            or generated != task["metadata"]["generated"]
            or envelope["authority"] != task["metadata"]["authority"]
        ):
            raise PeerConflict("planner consumption must match frozen registered output")
        digest = hashlib.sha256(
            json.dumps([task_id, envelope], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, tenant, sender, task_id)
            previous = db.execute(
                "SELECT digest,task_id FROM peer_messages WHERE tenant=? AND agent=? AND id=?",
                (tenant, planner_id, message_id),
            ).fetchone()
            if previous:
                if previous["digest"] != digest or previous["task_id"] != task_id:
                    raise PeerConflict("planner acknowledgement identifier rebound")
                return task
            if (
                task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED"
                or task["metadata"].get("generated_delivery_state")
                != "awaiting_planner_consumption"
            ):
                raise PeerConflict("task is not awaiting planner consumption")
            now = datetime.now(UTC).isoformat()
            task["metadata"]["generated_delivery_state"] = "planner_acknowledged"
            task["metadata"]["planner_consumption"] = {
                "planner_id": planner_id,
                "message_id": message_id,
                "generated": generated,
            }
            task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": now}
            causation_id = task["history"][-1]["messageId"]
            task["history"].append(
                {
                    "messageId": message_id,
                    "role": "ROLE_USER",
                    "taskId": task_id,
                    "contextId": task["contextId"],
                    "parts": [{"data": envelope}],
                }
            )
            db.execute(
                "INSERT INTO peer_messages VALUES (?,?,?,?,?)",
                (tenant, planner_id, message_id, digest, task_id),
            )
            db.execute(
                "UPDATE peer_tasks SET body=? WHERE tenant=? AND agent=? AND id=?",
                (json.dumps(task), tenant, sender, task_id),
            )
            db.execute(
                "INSERT INTO peer_events(task_id,kind,timestamp) VALUES (?,?,?)",
                (task_id, "consume", now),
            )
            self._peer_record(db, task, planner_id, "consume", message_id, causation_id)
            return task

    def cancel(self, tenant, agent, task_id, *, provenance_origin=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, tenant, agent, task_id)
            self._peer_origin(task, provenance_origin)
            if task["status"]["state"] == "TASK_STATE_CANCELED":
                return task
            if task["status"]["state"] not in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_WORKING"}:
                raise PeerConflict("terminal task cannot be canceled")
            now = datetime.now(UTC).isoformat()
            task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now}
            db.execute("UPDATE peer_tasks SET body=? WHERE id=?", (json.dumps(task), task_id))
            db.execute(
                "INSERT INTO peer_events(task_id,kind,timestamp) VALUES (?,?,?)",
                (task_id, "cancel", now),
            )
            if provenance_origin:
                message_id = "peer_cancel_" + hashlib.sha256(task_id.encode()).hexdigest()
                self._peer_record(
                    db, task, agent, "cancel", message_id, task["history"][-1]["messageId"]
                )
            return task

    @staticmethod
    def _peer_origin(task, origin):
        if origin is None:
            return
        if set(origin) != {"receiver_peer_id", "credential_id", "project_id", "destination"}:
            raise PeerConflict("closed receiving-peer provenance identity required")
        previous = task["metadata"].get("peer_provenance_origin")
        if previous is not None and previous != origin:
            raise PeerConflict("receiving-peer provenance identity cannot be rebound")
        task["metadata"]["peer_provenance_origin"] = dict(origin)

    def _peer_record(self, db, task, sender_id, operation, message_id, causation_id):
        """Only protocol/reference acceptance; never model execution or result validity."""
        from tessera_agent.provenance_client import sanitize_envelope

        fence = task["metadata"]["authority"]
        origin = task["metadata"]["peer_provenance_origin"]
        record_id = (
            "peer_provenance_"
            + hashlib.sha256(
                json.dumps([task["id"], operation, message_id], separators=(",", ":")).encode()
            ).hexdigest()
        )
        result_id = task["artifacts"][0]["artifactId"] if operation == "result-reference" else None
        record = {
            "record_id": record_id,
            "kind": "permission"
            if operation == "cancel"
            else "delegation"
            if operation == "delegation"
            else "agent"
            if result_id
            else "message",
            "outcome": "canceled"
            if operation == "cancel"
            else "requested"
            if operation == "submit"
            else "succeeded",
            "correlation_id": result_id or task["id"],
            "causation_id": causation_id,
            "tool_id": "a2a.reference." + operation,
            "model_id": None,
            "references": {
                "task_id": fence["binding"]["task_id"],
                "variant_id": fence["binding"]["variant_id"],
                "grant_id": fence["grant_id"],
                **(
                    {
                        "provenance_record_id": task["metadata"]["generated"].get("record_id")
                        or task["metadata"]["generated"]["capture_record_id"]
                    }
                    if (
                        task["metadata"].get("generated", {}).get("record_id")
                        or task["metadata"].get("generated", {}).get("capture_record_id")
                    )
                    else {}
                ),
            },
            "remote": {
                "task_id": task["id"],
                "message_id": message_id,
                "peer_id": sender_id,
                "destination_digest": "sha256:"
                + hashlib.sha256(origin["destination"].encode()).hexdigest(),
            },
            "error_code": "canceled" if operation == "cancel" else None,
        }
        proof = {
            "grant_id": fence["grant_id"],
            "expected_revision": int(fence["revision"]),
            "expected_epoch": int(fence["epoch"]),
        }
        payload = sanitize_envelope(record, proof)
        self._outbox(
            db,
            task["id"],
            {
                **payload,
                "links": {
                    **(
                        {"generated_registration": task["metadata"]["generated"]}
                        if task["metadata"].get("generated")
                        else {}
                    ),
                    "receiver_peer_id": origin["receiver_peer_id"],
                    "subject_credential_id": origin["credential_id"],
                    "project_id": origin["project_id"],
                    "sender_peer_id": sender_id,
                    "result_reference_id": result_id,
                    "verified": False,
                    "reasoning_exercised": False,
                },
            },
        )

    @staticmethod
    def _outbox(db, task_id, record):
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record_id = record["record"]["record_id"]
        previous = db.execute(
            "SELECT body FROM peer_provenance_outbox WHERE id=?", (record_id,)
        ).fetchone()
        if previous and previous[0] != encoded:
            raise PeerConflict("provenance identifier cannot be rebound")
        db.execute(
            "INSERT OR IGNORE INTO peer_provenance_outbox(id,task_id,body) VALUES (?,?,?)",
            (record_id, task_id, encoded),
        )

    def link_reasoning(self, tenant, agent, task_id, binding, record, delegation_record=None):
        """Immutable task/run claim and provenance intent in the same transaction."""
        encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, tenant, agent, task_id)
            previous = db.execute(
                "SELECT * FROM peer_reasoning_links WHERE task_id=?", (task_id,)
            ).fetchone()
            if previous:
                if previous["binding"] != encoded:
                    raise PeerConflict("reasoning profile/task cannot be rebound")
                return dict(previous)
            if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
                raise PeerConflict("only pending reference tasks can start reasoning")
            try:
                db.execute(
                    "INSERT INTO peer_reasoning_links(task_id,run_id,binding,state) "
                    "VALUES (?,?,?,'claimed')",
                    (task_id, binding["run_id"], encoded),
                )
            except sqlite3.IntegrityError as exc:
                raise PeerConflict("reasoning run already belongs to another task") from exc
            task["status"] = {
                "state": "TASK_STATE_WORKING",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            task["metadata"]["reasoning_run_id"] = binding["run_id"]
            task["metadata"]["reasoning_profile_sha256"] = binding["profile_sha256"]
            db.execute("UPDATE peer_tasks SET body=? WHERE id=?", (json.dumps(task), task_id))
            self._outbox(db, task_id, record)
            if delegation_record:
                self._outbox(db, task_id, delegation_record)
            return dict(
                db.execute(
                    "SELECT * FROM peer_reasoning_links WHERE task_id=?", (task_id,)
                ).fetchone()
            )

    def reasoning_link(self, tenant, agent, task_id):
        with self.connect() as db:
            self._read(db, tenant, agent, task_id)
            row = db.execute(
                "SELECT * FROM peer_reasoning_links WHERE task_id=?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def reasoning_observation(
        self, tenant, agent, task_id, row, record, denial_record=None, session_record=None
    ):
        """Publish only content-free runtime state; cancellation always wins."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, tenant, agent, task_id)
            link = db.execute(
                "SELECT run_id FROM peer_reasoning_links WHERE task_id=?", (task_id,)
            ).fetchone()
            if link is None or link[0] != row["id"]:
                raise PeerConflict("runtime observation does not match task")
            db.execute(
                "UPDATE peer_reasoning_links SET state='observed',runtime_state=?,"
                "session_id=?,message_id=? WHERE task_id=?",
                (row["state"], row["session_id"], row["message_id"], task_id),
            )
            task["metadata"]["reasoning_runtime_state"] = row["state"]
            task["metadata"]["reasoning_session_id"] = row["session_id"]
            task["metadata"]["reasoning_message_id"] = row["message_id"]
            task["metadata"]["termination_verified"] = False
            if task["status"]["state"] != "TASK_STATE_CANCELED":
                state = {
                    "completed": "TASK_STATE_COMPLETED",
                    "failed": "TASK_STATE_FAILED",
                    "stop_acknowledged": "TASK_STATE_FAILED",
                    "stop_unknown": "TASK_STATE_FAILED",
                }.get(row["state"], "TASK_STATE_WORKING")
                task["status"] = {"state": state, "timestamp": datetime.now(UTC).isoformat()}
                if row["state"] == "completed":
                    task["artifacts"] = [
                        {
                            "artifactId": "runtime_" + row["id"],
                            "name": "reasoning-session-outcome-reference",
                            "parts": [
                                {
                                    "data": {
                                        "run_id": row["id"],
                                        "session_id": row["session_id"],
                                        "model_outcome": "succeeded",
                                        "content_collected": False,
                                        "scientifically_verified": False,
                                    }
                                }
                            ],
                        }
                    ]
            db.execute("UPDATE peer_tasks SET body=? WHERE id=?", (json.dumps(task), task_id))
            self._outbox(db, task_id, record)
            if denial_record:
                self._outbox(db, task_id, denial_record)
            if session_record:
                self._outbox(db, task_id, session_record)
            return task

    @contextmanager
    def reasoning_effect_gate(self, tenant, agent, task_id):
        """Linearize create/prompt with durable peer cancellation, not remote revocation."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, tenant, agent, task_id)
            if task["status"]["state"] != "TASK_STATE_WORKING":
                raise PeerConflict("peer task no longer admits reasoning effects")
            yield

    def provenance_outbox(self, tenant, agent, task_id, *, pending_only=False, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100 or type(pending_only) is not bool:
            raise ValueError("invalid outbox bound")
        with self.connect() as db:
            self._read(db, tenant, agent, task_id)
            return [
                {
                    "envelope": json.loads(r["body"]),
                    "delivery_state": r["delivery_state"],
                    "last_delivery": r["status"],
                    "delivery_code": r["code"],
                    "receipt_sequence": r["sequence"],
                    "delivery_attempts": r["attempts"] or 0,
                }
                for r in db.execute(
                    "SELECT o.body,o.delivery_state,d.status,d.code,d.sequence,d.attempts "
                    "FROM peer_provenance_outbox o "
                    "LEFT JOIN peer_provenance_delivery d ON d.id=o.id "
                    "WHERE o.task_id=? AND (?=0 OR o.delivery_state='pending') "
                    "ORDER BY o.rowid LIMIT ?",
                    (task_id, int(pending_only), limit),
                )
            ]

    def provenance_delivery(self, tenant, agent, task_id, payload, result, *, started=False):
        """Atomic exact-payload receipt acknowledgement; denial never changes outcome."""
        record_id = payload["record"]["record_id"]
        code = result.code
        allowed_codes = {
            "delivery_started",
            "collector_rejected",
            "collector_unavailable",
            "response_bound",
            "invalid_acknowledgement",
            "collector_acknowledged",
            "transport_failure",
            "invalid_envelope",
            "unknown_result",
        }
        if code not in allowed_codes:
            code = "unknown_result"
        status = (
            result.status if result.status in {"accepted", "rejected", "unknown"} else "unknown"
        )
        sequence = result.sequence
        if result.record_id != record_id or (
            status == "accepted" and (type(sequence) is not int or sequence < 1)
        ):
            status, code, sequence = "unknown", "invalid_acknowledgement", None
        if status != "accepted":
            sequence = None
        status_code = result.status_code
        if type(status_code) is not int or not 100 <= status_code <= 599:
            status_code = None
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._read(db, tenant, agent, task_id)
            row = db.execute(
                "SELECT body,delivery_state FROM peer_provenance_outbox WHERE id=? AND task_id=?",
                (record_id, task_id),
            ).fetchone()
            if row is None:
                raise PeerNotFound("outbox record not found")
            envelope = json.loads(row["body"])
            if payload != {key: envelope[key] for key in ("record", "grant_proof")}:
                raise PeerConflict("delivery body differs from immutable outbox")
            previous = db.execute(
                "SELECT * FROM peer_provenance_delivery WHERE id=?", (record_id,)
            ).fetchone()
            if previous and previous["request_digest"] != digest:
                raise PeerConflict("delivery body cannot be rebound")
            if row["delivery_state"] == "acknowledged":
                if status == "accepted" and previous["sequence"] != sequence:
                    raise PeerConflict("collector receipt changed")
                return
            attempts = (previous["attempts"] if previous else 0) + int(started)
            db.execute(
                "INSERT INTO peer_provenance_delivery VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status,code=excluded.code,"
                "status_code=excluded.status_code,sequence=excluded.sequence,attempts=excluded.attempts",
                (record_id, digest, status, code, status_code, sequence, attempts),
            )
            if status == "accepted":
                db.execute(
                    "UPDATE peer_provenance_outbox SET delivery_state='acknowledged' WHERE id=?",
                    (record_id,),
                )

    def provenance_summary(self, tenant, agent, task_id):
        with self.connect() as db:
            self._read(db, tenant, agent, task_id)
            row = db.execute(
                "SELECT COUNT(*) AS total,SUM(delivery_state='acknowledged') AS acknowledged "
                "FROM peer_provenance_outbox WHERE task_id=?",
                (task_id,),
            ).fetchone()
            total, acknowledged = row["total"], row["acknowledged"] or 0
            return {
                "total": total,
                "acknowledged": acknowledged,
                "pending": total - acknowledged,
                "outbox_fully_acknowledged": total == acknowledged,
            }
