"""Private supervisor journal. Records effect identities, never prompts or credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

from .provenance_client import sanitize_envelope, stable_key


def private_file(path: Path) -> bytes:
    """Read an explicitly supplied credential/config file without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Runtime files must be private regular files.")
        if info.st_uid != os.getuid() or info.st_size > 64 * 1024:
            raise ValueError("Runtime file ownership or size is invalid.")
        return os.read(fd, 64 * 1024 + 1)
    finally:
        os.close(fd)


class ReasoningStore:
    """Immutable bindings, crash-safe call intents and monotonic usage observations."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Journal directory must be private and not a symlink.")
        if info.st_uid != os.getuid():
            raise ValueError("Journal directory must belong to the runtime user.")
        path = directory / "reasoning.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError("Journal database must be a private owned regular file.")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, binding TEXT NOT NULL, digest TEXT NOT NULL,
            session_id TEXT NOT NULL, message_id TEXT NOT NULL, started REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'ready', stop_requested INTEGER NOT NULL DEFAULT 0,
            reason TEXT, tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS effects (
            run_id TEXT NOT NULL, boundary TEXT NOT NULL, outcome TEXT NOT NULL,
            PRIMARY KEY(run_id,boundary), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS admitted_prompt_inputs (
            run_id TEXT PRIMARY KEY, digest TEXT NOT NULL, references_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS provenance_outbox (
            record_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, key TEXT NOT NULL,
            body TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending',
            code TEXT, sequence INTEGER);
        """)

    def close(self):
        self.db.close()

    def bind(self, run_id: str, binding: dict, now: float):
        encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((run_id + "\n" + encoded).encode()).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO runs(id,binding,digest,session_id,message_id,started) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, encoded, digest, "ses_" + digest[:32], "msg_" + digest[32:], now),
            )
            if self.get(run_id)["digest"] != digest:
                raise ValueError("Run identity cannot be rebound.")
            self._record(run_id, "run.requested", "agent", "requested")
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown run.")
        return dict(row)

    def admit_input(self, run_id, digest, references):
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(references, dict)
            or set(references)
            != {
                "output_id",
                "disclosure_id",
                "content_digest",
                "input_context_id",
                "human_input_record_id",
            }
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
                for value in references.values()
            )
        ):
            raise ValueError("Only opaque admitted input references may be journaled.")
        encoded = json.dumps(references, sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO admitted_prompt_inputs VALUES(?,?,?)",
                (run_id, digest, encoded),
            )
            row = self.db.execute(
                "SELECT digest,references_json FROM admitted_prompt_inputs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if tuple(row) != (digest, encoded):
                raise ValueError("Admitted initial prompt references changed.")

    def input_digest(self, run_id):
        row = self.db.execute(
            "SELECT digest FROM admitted_prompt_inputs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row[0] if row else json.loads(self.get(run_id)["binding"])["prompt_sha256"]

    def effect(self, run_id: str, boundary: str) -> str | None:
        row = self.db.execute(
            "SELECT outcome FROM effects WHERE run_id=? AND boundary=?", (run_id, boundary)
        ).fetchone()
        return row[0] if row else None

    def begin(self, run_id: str, boundary: str) -> bool:
        """A previous pending/uncertain effect is not permission to replay it."""
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO effects VALUES(?,?,'pending')", (run_id, boundary)
            )
            if cursor.rowcount:
                self._record(
                    run_id,
                    boundary + ".requested",
                    "tool",
                    "requested",
                    tool="opencode.session." + boundary,
                )
        return cursor.rowcount == 1

    def finish(self, run_id: str, boundary: str, outcome: str):
        if outcome not in {"acknowledged", "uncertain"}:
            raise ValueError("Invalid effect outcome.")
        if self.effect(run_id, boundary) not in {"pending", "uncertain"}:
            raise ValueError("Effect completion requires a persisted intent.")
        with self.db:
            self.db.execute(
                "UPDATE effects SET outcome=? WHERE run_id=? AND boundary=?",
                (outcome, run_id, boundary),
            )
            self._record(
                run_id,
                boundary + "." + outcome,
                "tool",
                "succeeded" if outcome == "acknowledged" else "unknown",
                tool="opencode.session." + boundary,
                error="transport_failure" if outcome == "uncertain" else None,
            )
            if boundary == "create" and outcome == "acknowledged":
                self._record(run_id, "session.started", "agent", "started")
            if boundary == "prompt" and outcome == "acknowledged":
                self._record(run_id, "model.started", "model", "started")

    def state(self, run_id: str, state: str, reason: str | None = None):
        with self.db:
            self.db.execute("UPDATE runs SET state=?, reason=? WHERE id=?", (state, reason, run_id))
            outcome = {
                "running": "started",
                "completed": "succeeded",
                "failed": "failed",
                "stop_acknowledged": "unknown",
                "stop_unknown": "unknown",
            }.get(state)
            if outcome:
                self._record(
                    run_id,
                    "state." + state,
                    "model",
                    outcome,
                    error="outcome_unknown"
                    if state == "failed"
                    else "outcome_unknown"
                    if outcome == "unknown"
                    else None,
                )
            if state == "stopping":
                error = (
                    "expired"
                    if reason in {"grant_expired", "duration_expired"}
                    else "revoked"
                    if reason == "grant_revoked"
                    else "budget_exceeded"
                    if reason in {"budget_exhausted", "usage_fence"}
                    else "canceled"
                    if reason == "stop_requested"
                    else "permission_denied"
                )
                uncertain = reason == "invalid_authority_or_usage" or (
                    isinstance(reason, str) and reason.startswith("uncertain_")
                )
                self._record(
                    run_id,
                    "fence.unknown" if uncertain else "fence." + error,
                    "permission",
                    "unknown" if uncertain else "canceled" if error == "canceled" else "denied",
                    error="outcome_unknown" if uncertain else error,
                )

    def stop(self, run_id: str):
        self.get(run_id)
        with self.db:
            self.db.execute("UPDATE runs SET stop_requested=1 WHERE id=?", (run_id,))
            self._record(run_id, "stop.requested", "permission", "requested")

    def usage(self, run_id: str, tokens: int, cost: float):
        previous = self.get(run_id)
        if tokens < previous["tokens"] or cost < previous["cost"]:
            raise ValueError("Upstream usage regressed.")
        with self.db:
            self.db.execute("UPDATE runs SET tokens=?,cost=? WHERE id=?", (tokens, cost, run_id))

    def _record(self, run_id, transition, kind, outcome, *, tool=None, error=None):
        """Called in the same SQLite transaction as the observed state/effect."""
        row = self.get(run_id)
        binding = json.loads(row["binding"])
        record_id = (
            "runtime_" + hashlib.sha256((row["digest"] + ":" + transition).encode()).hexdigest()
        )
        if self.db.execute(
            "SELECT 1 FROM provenance_outbox WHERE record_id=?", (record_id,)
        ).fetchone():
            return
        if (
            self.db.execute(
                "SELECT COUNT(*) FROM provenance_outbox WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            >= 64
        ):
            raise ValueError("Runtime provenance transition bound exceeded.")
        grant_id = binding.get("grant_id")
        proof = (
            {
                "grant_id": grant_id,
                "expected_revision": binding["revision"],
                "expected_epoch": binding["epoch"],
            }
            if grant_id
            else None
        )
        record = {
            "record_id": record_id,
            "kind": kind,
            "outcome": outcome,
            "correlation_id": row["session_id"],
            "causation_id": run_id,
            "model_id": binding.get("model"),
            "tool_id": tool,
            "references": {"grant_id": grant_id},
            "remote": {"message_id": row["message_id"]},
            "error_code": error,
        }
        body = sanitize_envelope(record, proof)
        self.db.execute(
            "INSERT INTO provenance_outbox(record_id,run_id,key,body) VALUES(?,?,?,?)",
            (
                record_id,
                run_id,
                stable_key(record_id),
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            ),
        )

    def permission_current(self, run_id):
        with self.db:
            self._record(run_id, "fence.current", "permission", "succeeded")

    def provenance_pending(self, run_id, limit=8):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("Outbox batch must be bounded.")
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM provenance_outbox WHERE run_id=? AND delivery!='accepted' "
                "ORDER BY rowid LIMIT ?",
                (run_id, limit),
            )
        ]

    def provenance_receipt(self, record_id, result):
        if result.record_id != record_id or result.status not in {
            "accepted",
            "rejected",
            "unknown",
        }:
            raise ValueError("Invalid provenance delivery receipt.")
        if result.status == "accepted" and (
            type(result.sequence) is not int or result.sequence < 1
        ):
            raise ValueError("Accepted provenance requires a collector sequence acknowledgement.")
        with self.db:
            self.db.execute(
                "UPDATE provenance_outbox SET delivery=?,code=?,sequence=? WHERE record_id=? "
                "AND delivery!='accepted'",
                (result.status, result.code, result.sequence, record_id),
            )

    def provenance_coverage(self, run_id):
        pending = self.db.execute(
            "SELECT COUNT(*) FROM provenance_outbox WHERE run_id=? AND delivery!='accepted'",
            (run_id,),
        ).fetchone()[0]
        recorded = self.db.execute(
            "SELECT COUNT(*) FROM provenance_outbox WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        return {
            "pending": pending,
            "recorded": recorded,
            "delivery_complete": recorded > 0 and pending == 0,
            "coverage": "supervisor_observed_metadata_only",
        }

    def drain_provenance(self, run_id, client, project_id, limit=8):
        for item in self.provenance_pending(run_id, limit):
            body = json.loads(item["body"])
            result = client.append(project_id, body["record"], body["grant_proof"], item["key"])
            self.provenance_receipt(item["record_id"], result)
            if result.status != "accepted":
                break  # No hidden retry loop, rejection keeps exact immutable pins.
        return self.provenance_coverage(run_id)
