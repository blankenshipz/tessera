"""Private supervisor journal. Records effect identities, never prompts or credentials."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from datetime import UTC, datetime
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


RATE_LIMIT_CONDITIONS = frozenset({"model_rate_limited", "model_quota_exhausted"})
MODEL_CALL_FIELDS = frozenset(
    {
        "model_configured",
        "model_reported",
        "model_identity_basis",
        "tier",
        "prompt_sha256",
        "profile_sha256",
        "input_sha256",
        "input_digests",
        "policy_sha256",
        "input_class",
        "tokens",
        "cost",
        "cost_basis",
        # Billed (Anthropic Messages API) receipts only.
        "limits",
        "daily_tokens_after",
        "daily_cost_cents_after",
        "cost_cents_estimate_max",
        "output_sha256",
        # Billed calls that halt the run: model_substituted, output_unrecorded, uncertain.
        "outcome_class",
    }
)
# Effect outcomes. ``not_sent`` is a definite upstream rejection (or a local refusal
# before sending): the request was not accepted, so a later attempt is not a replay.
EFFECT_OUTCOMES = frozenset({"acknowledged", "uncertain", "not_sent"})


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).date().isoformat()


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
        self.directory = directory
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self._migrate()
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, binding TEXT NOT NULL, digest TEXT NOT NULL,
            session_id TEXT NOT NULL, message_id TEXT NOT NULL, started REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'ready', stop_requested INTEGER NOT NULL DEFAULT 0,
            reason TEXT, tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS effects (
            run_id TEXT NOT NULL, boundary TEXT NOT NULL, outcome TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(run_id,boundary), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS admitted_prompt_inputs (
            run_id TEXT PRIMARY KEY, digest TEXT NOT NULL, references_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS provenance_outbox (
            record_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, key TEXT NOT NULL,
            body TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending',
            code TEXT, sequence INTEGER);
          CREATE TABLE IF NOT EXISTS model_calls (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, receipt_json TEXT NOT NULL,
            observed_at REAL NOT NULL, day TEXT NOT NULL,
            tokens_delta INTEGER NOT NULL DEFAULT 0, cost_cents_delta REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS model_rate_limits (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, condition TEXT NOT NULL,
            backoff_until REAL NOT NULL, observed_at REAL NOT NULL,
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
        """)

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self):
        """Open older state dirs: per-run model_calls become append-only (run_id, seq)."""
        old = self._columns("model_calls")
        if old and "seq" not in old:
            # Legacy rows were included-usage receipts: their token totals count toward
            # the day's ledger; their cost was never billed in cents.
            self.db.executescript("""
              BEGIN IMMEDIATE;
              ALTER TABLE model_calls RENAME TO model_calls_v1;
              CREATE TABLE model_calls (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL, receipt_json TEXT NOT NULL,
                observed_at REAL NOT NULL, day TEXT NOT NULL,
                tokens_delta INTEGER NOT NULL DEFAULT 0, cost_cents_delta REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
              INSERT INTO model_calls(run_id,seq,receipt_json,observed_at,day,tokens_delta)
                SELECT run_id, 1, receipt_json, observed_at,
                  strftime('%Y-%m-%d', observed_at, 'unixepoch'),
                  CASE WHEN json_type(receipt_json,'$.tokens.total')='integer'
                         THEN json_extract(receipt_json,'$.tokens.total')
                       WHEN json_type(receipt_json,'$.tokens')='integer'
                         THEN json_extract(receipt_json,'$.tokens')
                       ELSE 0 END
                FROM model_calls_v1;
              DROP TABLE model_calls_v1;
              COMMIT;
            """)
        effects = self._columns("effects")
        if effects and "attempt" not in effects:
            self.db.execute("ALTER TABLE effects ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
            self.db.commit()

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

    def attempts(self, run_id: str, boundary: str) -> int:
        row = self.db.execute(
            "SELECT attempt FROM effects WHERE run_id=? AND boundary=?", (run_id, boundary)
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _attempt_name(name: str, attempt: int) -> str:
        return name if attempt <= 1 else f"{name}.{attempt}"

    def begin(self, run_id: str, boundary: str, tool_prefix: str = "opencode.session.") -> bool:
        """A previous pending/uncertain effect is not permission to replay it.

        Only a definitely rejected (``not_sent``) intent may be attempted again.
        """
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO effects(run_id,boundary,outcome) VALUES(?,?,'pending')",
                (run_id, boundary),
            )
            if not cursor.rowcount:
                cursor = self.db.execute(
                    "UPDATE effects SET outcome='pending', attempt=attempt+1 "
                    "WHERE run_id=? AND boundary=? AND outcome='not_sent'",
                    (run_id, boundary),
                )
            if cursor.rowcount:
                self._record(
                    run_id,
                    self._attempt_name(boundary + ".requested", self.attempts(run_id, boundary)),
                    "tool",
                    "requested",
                    tool=tool_prefix + boundary,
                )
        return cursor.rowcount == 1

    def finish(
        self,
        run_id: str,
        boundary: str,
        outcome: str,
        *,
        error: str | None = None,
        tool_prefix: str = "opencode.session.",
    ):
        if outcome not in EFFECT_OUTCOMES:
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
                self._attempt_name(boundary + "." + outcome, self.attempts(run_id, boundary)),
                "tool",
                "succeeded"
                if outcome == "acknowledged"
                else "failed"
                if outcome == "not_sent"
                else "unknown",
                tool=tool_prefix + boundary,
                error="transport_failure"
                if outcome == "uncertain"
                else (error or "provider_failure")
                if outcome == "not_sent"
                else None,
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
                    if reason
                    in {
                        "budget_exhausted",
                        "usage_fence",
                        "daily_cap_exceeded",
                        "model_call_cap_exceeded",
                    }
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

    def model_call(
        self,
        run_id: str,
        receipt: dict,
        now: float,
        *,
        tokens_delta: int = 0,
        cost_cents_delta: float = 0.0,
        only_if_changed: bool = False,
    ) -> int | None:
        """Append one metadata-only model-call receipt row (run_id, seq); never updates.

        Only the allowlisted fields are stored; callers never pass prompts,
        completions or credentials, and unknown keys are refused. The deltas feed the
        day's (UTC) ledger. ``only_if_changed`` skips a row identical to the run's
        latest receipt (a re-polled cumulative session observation is not a new call).
        Returns the new seq, or None when skipped.
        """
        self.get(run_id)
        if not isinstance(receipt, dict) or set(receipt) - MODEL_CALL_FIELDS:
            raise ValueError("Unknown model-call receipt field.")
        if (
            type(tokens_delta) is not int
            or tokens_delta < 0
            or type(cost_cents_delta) not in {int, float}
            or not math.isfinite(cost_cents_delta)
            or cost_cents_delta < 0
        ):
            raise ValueError("Invalid model-call usage delta.")
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.db:
            latest = self.db.execute(
                "SELECT receipt_json FROM model_calls WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if only_if_changed and latest is not None and latest[0] == encoded:
                return None
            seq = self.next_model_call_seq(run_id)
            self.db.execute(
                "INSERT INTO model_calls(run_id,seq,receipt_json,observed_at,day,tokens_delta,"
                "cost_cents_delta) VALUES(?,?,?,?,?,?,?)",
                (run_id, seq, encoded, now, utc_day(now), tokens_delta, float(cost_cents_delta)),
            )
        return seq

    def next_model_call_seq(self, run_id: str) -> int:
        return self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    def model_calls(self, run_id: str) -> list[dict]:
        return [
            {
                **json.loads(row["receipt_json"]),
                "seq": row["seq"],
                "observed_at": row["observed_at"],
            }
            for row in self.db.execute(
                "SELECT seq,receipt_json,observed_at FROM model_calls WHERE run_id=? ORDER BY seq",
                (run_id,),
            )
        ]

    def daily_usage(self, day: str) -> dict:
        """The UTC day's model-call tokens and billed cents across every run in this state dir."""
        tokens, cents = self.db.execute(
            "SELECT COALESCE(SUM(tokens_delta),0), COALESCE(SUM(cost_cents_delta),0) "
            "FROM model_calls WHERE day=?",
            (day,),
        ).fetchone()
        return {"tokens": int(tokens), "cost_cents": float(cents)}

    def rate_limit(self, run_id: str, condition: str, backoff_until: float, now: float):
        if condition not in RATE_LIMIT_CONDITIONS or not math.isfinite(backoff_until):
            raise ValueError("Invalid rate-limit observation.")
        with self.db:
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM model_rate_limits WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO model_rate_limits VALUES(?,?,?,?,?)",
                (run_id, seq, condition, backoff_until, now),
            )
            receipt = self.db.execute(
                "SELECT receipt_json FROM model_calls WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            known = json.loads(receipt[0]) if receipt else {}
            observed = {"backoff_until": float(backoff_until)}
            for field in ("tier", "policy_sha256"):
                if known.get(field) is not None:
                    observed[field] = known[field]
            # One outbox record per event row, in the same transaction as the event.
            self._record(
                run_id,
                f"rate_limit.{seq}",
                "model",
                "failed",
                error=condition,
                rate_limit=observed,
            )

    def latest_rate_limit(self, run_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT condition,backoff_until FROM model_rate_limits WHERE run_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else {"condition": row[0], "backoff_until": row[1]}

    def _record(self, run_id, transition, kind, outcome, *, tool=None, error=None, rate_limit=None):
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
        if rate_limit is not None:
            record["rate_limit"] = rate_limit
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
