"""Private supervisor journal. Records effect identities, never prompts or credentials."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
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
        # Per-turn receipts of the multi-turn tool loop.
        "turn",
        "stop_reason",
        "tool_use_count",
        "unrun_tool_count",
        # Billed calls that halt the run: model_substituted, output_unrecorded, uncertain.
        "outcome_class",
        # Billed receipts settled on the control plane's model-spend ledger.
        "spend_ledger",
    }
)
# Fields added to a settled receipt after the fact: the final turn's output digest, or
# output_unrecorded when the completion could not be written.
_ANNOTATIONS = frozenset({"output_sha256", "outcome_class"})
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# A per-turn tool provenance record is written only while the run's outbox holds fewer
# records than this; the remaining slots of the 64-record bound are kept for state records.
TOOL_TURN_OUTBOX_LIMIT = 56
# A parked loop snapshot (conversation so far) may hold bounded tool results.
MAX_PENDING_BYTES = 16 * 1024 * 1024
SPEND_LEDGER_FIELDS = frozenset(
    {"call_id", "status", "settled_micro_cents", "over_reservation", "cap_exceeded"}
)
SPEND_STATES = frozenset({"reserved", "settled", "released", "denied"})
# Effect outcomes. ``not_sent`` is a definite upstream rejection (or a local refusal
# before sending): the request was not accepted, so a later attempt is not a replay.
EFFECT_OUTCOMES = frozenset({"acknowledged", "uncertain", "not_sent"})
# Tool-call receipt statuses (ADR 0083). An executed tool is journaled as ``started``
# (an intent row) before the bridge is called and then transitioned exactly once to
# ``succeeded``, ``error`` or ``uncertain``; a row left ``started`` by a dead process is
# marked ``uncertain`` on the next tick. Tools that are never executed are inserted
# directly as ``denied`` (not allowed or not offered) or ``refused`` (``skip_reason`` names
# why: a fence code tripped before the call, or ``turn_limit_reached`` for tools requested
# on the last allowed turn). There is no separate ``executed`` column: a call was executed
# (or may have been) iff its status is in EXECUTED_TOOL_STATUSES.
TOOL_STATUSES = frozenset({"started", "succeeded", "error", "uncertain", "refused", "denied"})
TOOL_COMPLETIONS = frozenset({"succeeded", "error", "uncertain"})
EXECUTED_TOOL_STATUSES = TOOL_COMPLETIONS
# Calls answered with a tool_result (and counted against max_tool_calls).
ANSWERED_TOOL_STATUSES = frozenset({"succeeded", "error", "denied"})
TURN_LIMIT_REACHED = "turn_limit_reached"
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TOOL_CALLS_TABLE = """
  CREATE TABLE tool_calls (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, turn INTEGER NOT NULL,
    tool_index INTEGER NOT NULL, name TEXT NOT NULL, input_sha256 TEXT NOT NULL,
    result_sha256 TEXT, is_error INTEGER, truncated INTEGER,
    allowed INTEGER NOT NULL, observed_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN
      ('started','succeeded','error','uncertain','refused','denied')),
    skip_reason TEXT, completed_at REAL,
    CHECK((result_sha256 IS NOT NULL) = (status IN ('succeeded','error','denied'))),
    CHECK((is_error IS NOT NULL) = (status IN ('succeeded','error','denied'))),
    CHECK((truncated IS NOT NULL) = (status IN ('succeeded','error','denied'))),
    CHECK((skip_reason IS NOT NULL) = (status = 'refused')),
    PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
"""


_LOOP_ENDED = frozenset({"completed", "failed", "stopping", "stop_acknowledged", "stop_unknown"})


# Seconds a connection waits for another writer's lock (several supervisors may share one
# state dir) before an SQLite write fails.
BUSY_TIMEOUT_SECONDS = 30.0


class DailyCapRefused(Exception):
    """A reservation would take the UTC day over a daily cap; nothing was written."""

    def __init__(self):
        super().__init__("daily_cap_exceeded")


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).date().isoformat()


def tool_turn_receipts(rows: list[dict]) -> list[dict]:
    """The canonical per-call projection a tool-turn provenance digest covers."""
    fields = (
        "index",
        "name",
        "input_sha256",
        "result_sha256",
        "is_error",
        "truncated",
        "allowed",
        "status",
    )
    return [{k: row[k] for k in fields} for row in rows]


def tool_turn_digest(receipts: list[dict]) -> str:
    encoded = json.dumps(receipts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


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
        self.db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self._migrate()
        self.db.executescript(
            """
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
            status TEXT NOT NULL DEFAULT 'settled'
              CHECK(status IN ('pending','settled','void')),
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS model_spend_reservations (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, call_id TEXT NOT NULL,
            state TEXT NOT NULL, reserved_cents REAL NOT NULL, resolution TEXT,
            settled_micro_cents INTEGER,
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS model_rate_limits (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, condition TEXT NOT NULL,
            backoff_until REAL NOT NULL, observed_at REAL NOT NULL,
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS loop_parks (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, turn INTEGER NOT NULL,
            state_sha256 TEXT NOT NULL, observed_at REAL NOT NULL,
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
          CREATE TABLE IF NOT EXISTS loop_resumes (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, turn INTEGER NOT NULL,
            state_sha256 TEXT NOT NULL, restored_from_disk INTEGER NOT NULL,
            observed_at REAL NOT NULL,
            PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
        """
            + _TOOL_CALLS_TABLE.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS")
        )

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
        calls = self._columns("model_calls")
        if calls and "status" not in calls:
            # Rows written before billed calls were reserved are all settled receipts.
            self.db.execute(
                "ALTER TABLE model_calls ADD COLUMN status TEXT NOT NULL DEFAULT 'settled' "
                "CHECK(status IN ('pending','settled','void'))"
            )
            self.db.commit()
        reservations = self._columns("model_spend_reservations")
        if reservations and "resolution" not in reservations:
            # State dirs from before the report learned the control plane's resolution.
            self.db.execute("ALTER TABLE model_spend_reservations ADD COLUMN resolution TEXT")
            self.db.commit()
        if reservations and "settled_micro_cents" not in reservations:
            # State dirs from before the report kept the settled amount.
            self.db.execute(
                "ALTER TABLE model_spend_reservations ADD COLUMN settled_micro_cents INTEGER"
            )
            self.db.commit()
        effects = self._columns("effects")
        if effects and "attempt" not in effects:
            self.db.execute("ALTER TABLE effects ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
            self.db.commit()
        tools = self._columns("tool_calls")
        if tools and "status" not in tools:
            # Two legacy shapes, both completed after the call. Pre-status rows: a
            # disallowed row was never executed (denied); the rest succeeded or errored.
            # Rows with ``executed``/``skip_reason``: a skipped (unrun last-turn) row
            # becomes refused with its skip_reason and no result flags.
            skip = "skip_reason" if "skip_reason" in tools else "NULL"
            refused = f"({skip} IS NOT NULL)"
            self.db.executescript(
                """
              BEGIN IMMEDIATE;
              ALTER TABLE tool_calls RENAME TO tool_calls_v1;
            """
                + _TOOL_CALLS_TABLE
                + f"""
              INSERT INTO tool_calls(run_id,seq,turn,tool_index,name,input_sha256,
                result_sha256,is_error,truncated,allowed,observed_at,status,skip_reason,
                completed_at)
                SELECT run_id,seq,turn,tool_index,name,input_sha256,
                  CASE WHEN {refused} THEN NULL ELSE result_sha256 END,
                  CASE WHEN {refused} THEN NULL ELSE is_error END,
                  CASE WHEN {refused} THEN NULL ELSE truncated END,
                  allowed,observed_at,
                  CASE WHEN {refused} THEN 'refused' WHEN allowed=0 THEN 'denied'
                       WHEN is_error THEN 'error' ELSE 'succeeded' END,
                  {skip},
                  observed_at
                FROM tool_calls_v1;
              DROP TABLE tool_calls_v1;
              COMMIT;
            """
            )

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
        if state in _LOOP_ENDED:
            # A parked loop never continues past completion, a halt or a stop.
            self.discard_pending(run_id)
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
                        "tool_call_cap_exceeded",
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
        self.discard_pending(run_id)
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
        encoded = self._receipt_json(receipt, tokens_delta, cost_cents_delta)
        with self.db:
            return self._insert_model_call(
                run_id, encoded, now, tokens_delta, cost_cents_delta, only_if_changed
            )

    def record_model_call(
        self,
        run_id: str,
        receipt: dict,
        now: float,
        *,
        run_tokens: int,
        run_cost: float,
        tokens_delta: int = 0,
        cost_cents_delta: float = 0.0,
    ) -> int:
        """Set the run's cumulative usage and append its receipt row in ONE transaction.

        ``run_tokens``/``run_cost`` are the run's new cumulative totals (monotonic, as in
        ``usage``); the deltas feed the day's (UTC) ledger, which lives in the same
        ``model_calls`` row.  Either the run totals, the receipt and the ledger entry are all
        written or none is, so a crash never leaves a run cost without its receipt.
        """
        previous = self.get(run_id)
        if run_tokens < previous["tokens"] or run_cost < previous["cost"]:
            raise ValueError("Upstream usage regressed.")
        encoded = self._receipt_json(receipt, tokens_delta, cost_cents_delta)
        with self.db:
            self.db.execute(
                "UPDATE runs SET tokens=?,cost=? WHERE id=?", (run_tokens, run_cost, run_id)
            )
            return self._insert_model_call(
                run_id, encoded, now, tokens_delta, cost_cents_delta, False
            )

    @staticmethod
    def _receipt_json(receipt: dict, tokens_delta: int, cost_cents_delta: float) -> str:
        if not isinstance(receipt, dict) or set(receipt) - MODEL_CALL_FIELDS:
            raise ValueError("Unknown model-call receipt field.")
        ledger = receipt.get("spend_ledger")
        if ledger is not None and (
            not isinstance(ledger, dict)
            or set(ledger) - SPEND_LEDGER_FIELDS
            or any(not isinstance(v, str | int | bool) for v in ledger.values())
        ):
            raise ValueError("Invalid spend-ledger receipt field.")
        if (
            type(tokens_delta) is not int
            or tokens_delta < 0
            or type(cost_cents_delta) not in {int, float}
            or not math.isfinite(cost_cents_delta)
            or cost_cents_delta < 0
        ):
            raise ValueError("Invalid model-call usage delta.")
        return json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _insert_model_call(
        self,
        run_id,
        encoded,
        now,
        tokens_delta,
        cost_cents_delta,
        only_if_changed,
        status="settled",
    ) -> int | None:
        """Insert one receipt row; the caller holds the transaction (``with self.db``)."""
        latest = self.db.execute(
            "SELECT receipt_json FROM model_calls WHERE run_id=? AND status='settled' "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if only_if_changed and latest is not None and latest[0] == encoded:
            return None
        seq = self.next_model_call_seq(run_id)
        self.db.execute(
            "INSERT INTO model_calls(run_id,seq,receipt_json,observed_at,day,tokens_delta,"
            "cost_cents_delta,status) VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                seq,
                encoded,
                now,
                utc_day(now),
                tokens_delta,
                float(cost_cents_delta),
                status,
            ),
        )
        return seq

    # -- billed-call reservations (ADR 0083 §5) ----------------------------------------------
    # Before each billed request a ``pending`` row holds the call's worst case (counted input +
    # output cap, cost_cents_estimate_max), counted in the run totals and the day's ledger in
    # the same transaction.  The response settles that row (``settled``: actual, substituted
    # worst case, or the reservation kept for an unknown outcome); a definite rejection that
    # sent nothing billable voids it (``void``: zero tokens and cost, kept for audit).  A row
    # still ``pending`` after its process died becomes a settled uncertain receipt.

    def reserve_model_call(
        self,
        run_id: str,
        receipt: dict,
        now: float,
        *,
        tokens_delta: int,
        cost_cents_delta: float,
        daily_token_cap: int | None = None,
        daily_cost_cap_cents: float | None = None,
    ) -> int:
        """Check the daily caps and insert a ``pending`` reservation: ONE write transaction.

        The transaction takes the write lock first (``BEGIN IMMEDIATE``), re-reads the UTC
        day's ledger (settled and pending rows of every run in this state dir) and raises
        ``DailyCapRefused`` (writing nothing) if this reservation would take the day over
        either cap. Otherwise it fills ``daily_*_after`` from that same read, inserts the row
        and adds it to the run totals. Two supervisors sharing the state dir serialize here,
        so they cannot both pass and jointly exceed a cap; this is the authoritative check.
        """
        self.get(run_id)
        self._receipt_json(receipt, tokens_delta, cost_cents_delta)
        with self._immediate():
            day = self.daily_usage(utc_day(now))
            tokens_after = day["tokens"] + tokens_delta
            cents_after = day["cost_cents"] + float(cost_cents_delta)
            if (daily_token_cap is not None and tokens_after > daily_token_cap) or (
                daily_cost_cap_cents is not None and cents_after > daily_cost_cap_cents
            ):
                raise DailyCapRefused()
            if "daily_tokens_after" in receipt or daily_token_cap is not None:
                receipt = dict(receipt) | {
                    "daily_tokens_after": tokens_after,
                    "daily_cost_cents_after": cents_after,
                }
            encoded = self._receipt_json(receipt, tokens_delta, cost_cents_delta)
            seq = self._insert_model_call(
                run_id, encoded, now, tokens_delta, cost_cents_delta, False, status="pending"
            )
            self.db.execute(
                "UPDATE runs SET tokens=tokens+?,cost=cost+? WHERE id=?",
                (tokens_delta, float(cost_cents_delta), run_id),
            )
        return seq

    @contextlib.contextmanager
    def _immediate(self):
        """A write transaction that takes the database write lock before its first read."""
        if self.db.in_transaction:
            raise RuntimeError("Nested reasoning-store transaction.")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()

    def model_call_reservation(self, run_id: str, seq: int) -> dict:
        """A ``pending`` row's reserved tokens, cents and ledger day; ValueError otherwise."""
        found = self.db.execute(
            "SELECT tokens_delta,cost_cents_delta,day FROM model_calls "
            "WHERE run_id=? AND seq=? AND status='pending'",
            (run_id, seq),
        ).fetchone()
        if found is None:
            raise ValueError("Model-call settlement requires a pending reservation.")
        return {"tokens": found[0], "cost_cents": float(found[1]), "day": found[2]}

    def _release(self, run_id: str, seq: int, tokens_delta: int, cost_cents_delta: float):
        """Within a transaction: move the run totals from the reservation to the new charge."""
        held = self.model_call_reservation(run_id, seq)
        self.db.execute(
            "UPDATE runs SET tokens=MAX(tokens+?,0),cost=MAX(cost+?,0) WHERE id=?",
            (
                tokens_delta - held["tokens"],
                float(cost_cents_delta) - held["cost_cents"],
                run_id,
            ),
        )

    def settle_model_call(
        self,
        run_id: str,
        seq: int,
        receipt: dict,
        now: float,
        *,
        tokens_delta: int,
        cost_cents_delta: float,
    ):
        """Replace a pending reservation with the call's charge and receipt: ONE transaction.

        The run totals and the day's ledger move by the difference; the row keeps the
        reservation's ledger day.  Fails (and changes nothing) unless the row is pending.
        """
        encoded = self._receipt_json(receipt, tokens_delta, cost_cents_delta)
        with self.db:
            self._release(run_id, seq, tokens_delta, cost_cents_delta)
            self.db.execute(
                "UPDATE model_calls SET receipt_json=?,observed_at=?,tokens_delta=?,"
                "cost_cents_delta=?,status='settled' WHERE run_id=? AND seq=? AND status='pending'",
                (encoded, now, tokens_delta, float(cost_cents_delta), run_id, seq),
            )

    def void_model_call(self, run_id: str, seq: int, now: float):
        """A definite rejection sent nothing billable: release the reservation (``void``)."""
        with self.db:
            self._release(run_id, seq, 0, 0.0)
            self.db.execute(
                "UPDATE model_calls SET observed_at=?,tokens_delta=0,cost_cents_delta=0,"
                "status='void' WHERE run_id=? AND seq=? AND status='pending'",
                (now, run_id, seq),
            )

    def model_calls_interrupted(self, run_id: str, now: float) -> int:
        """Settle rows still ``pending`` (their process died mid-call) as uncertain; count them.

        The reservation is already in the run totals and the day's ledger, so it becomes
        the receipt as is (``reserved_worst_case_estimate``, ``uncertain``): nothing is
        counted twice and nothing is resent.
        """
        with self.db:
            rows = self.db.execute(
                "SELECT seq,receipt_json FROM model_calls WHERE run_id=? AND status='pending'",
                (run_id,),
            ).fetchall()
            for seq, encoded in rows:
                receipt = json.loads(encoded) | {
                    "cost_basis": "reserved_worst_case_estimate",
                    "outcome_class": "uncertain",
                }
                self.db.execute(
                    "UPDATE model_calls SET receipt_json=?,observed_at=?,status='settled' "
                    "WHERE run_id=? AND seq=?",
                    (
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                        now,
                        run_id,
                        seq,
                    ),
                )
            return len(rows)

    def annotate_model_call(self, run_id: str, seq: int, fields: dict):
        """Add ``output_sha256``/``outcome_class`` to a settled receipt (no usage change)."""
        if not isinstance(fields, dict) or not fields or set(fields) - _ANNOTATIONS:
            raise ValueError("Unknown model-call annotation.")
        with self.db:
            found = self.db.execute(
                "SELECT receipt_json FROM model_calls "
                "WHERE run_id=? AND seq=? AND status='settled'",
                (run_id, seq),
            ).fetchone()
            if found is None:
                raise ValueError("Model-call annotation requires a settled receipt.")
            receipt = json.loads(found[0]) | fields
            self.db.execute(
                "UPDATE model_calls SET receipt_json=? WHERE run_id=? AND seq=?",
                (json.dumps(receipt, sort_keys=True, separators=(",", ":")), run_id, seq),
            )

    # -- model-spend reservations (the control plane's ledger holds the truth) -----------------
    def spend_next_seq(self, run_id: str) -> int:
        """The next reservation sequence: it advances once a reservation or a refusal row exists, so
        a retry after a crash or a lost answer reuses the sequence (and so the ledger's idempotent
        answer), while a refused attempt is never replayed."""
        return self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM model_spend_reservations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]

    def spend_reserved(self, run_id: str, seq: int, call_id: str, reserved_cents: float):
        self.get(run_id)
        with self.db:
            self.db.execute(
                "INSERT INTO model_spend_reservations(run_id,seq,call_id,state,reserved_cents)"
                " VALUES(?,?,?,'reserved',?)",
                (run_id, seq, call_id, float(reserved_cents)),
            )

    def spend_denied(self, run_id: str, seq: int):
        """Record a refused reservation durably (no call id, nothing held): the next attempt uses
        a new sequence and so a new idempotency key, instead of replaying the stored refusal
        after the cause (a missing limit, a full day) is gone."""
        self.get(run_id)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO model_spend_reservations(run_id,seq,call_id,state,"
                "reserved_cents) VALUES(?,?,'','denied',0)",
                (run_id, seq),
            )

    def spend_resolve(
        self,
        run_id: str,
        seq: int,
        state: str,
        resolution: str | None = None,
        settled_micro_cents: int | None = None,
    ):
        """Close an open reservation locally. ``resolution`` is how the control plane closed it
        (a person may have) and ``settled_micro_cents`` the amount it recorded (None when
        released): both are shown in the report and never change the run."""
        if state not in SPEND_STATES - {"reserved", "denied"}:
            raise ValueError("Invalid reservation state.")
        with self.db:
            self.db.execute(
                "UPDATE model_spend_reservations SET state=?, resolution=?, "
                "settled_micro_cents=? WHERE run_id=? AND seq=? AND state='reserved'",
                (state, resolution, settled_micro_cents, run_id, seq),
            )

    def spend_open(self, run_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT seq,call_id,reserved_cents FROM model_spend_reservations "
            "WHERE run_id=? AND state='reserved' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None

    def spend_rows(self, run_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT seq,call_id,state,reserved_cents,resolution,settled_micro_cents "
                "FROM model_spend_reservations "
                "WHERE run_id=? ORDER BY seq",
                (run_id,),
            )
        ]

    def next_model_call_seq(self, run_id: str) -> int:
        return self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    def model_calls(self, run_id: str, *, every: bool = False) -> list[dict]:
        """Settled receipt rows in order; ``every`` also returns pending and void rows."""
        where = "" if every else " AND status='settled'"
        return [
            {
                **json.loads(row["receipt_json"]),
                "seq": row["seq"],
                "observed_at": row["observed_at"],
                "status": row["status"],
            }
            for row in self.db.execute(
                "SELECT seq,receipt_json,observed_at,status FROM model_calls WHERE run_id=?"
                + where
                + " ORDER BY seq",
                (run_id,),
            )
        ]

    @staticmethod
    def _valid_position(call: dict) -> bool:
        return (
            all(type(call[k]) is int and call[k] >= 0 for k in ("turn", "index"))
            and isinstance(call["name"], str)
            and bool(_TOOL_NAME.fullmatch(call["name"]))
            and isinstance(call["input_sha256"], str)
            and bool(_DIGEST.fullmatch(call["input_sha256"]))
            and type(call["allowed"]) is bool
        )

    def _insert_tool_row(self, run_id: str, call: dict, now: float, **fields) -> int:
        with self.db:
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM tool_calls WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO tool_calls(run_id,seq,turn,tool_index,name,input_sha256,"
                "result_sha256,is_error,truncated,allowed,observed_at,status,skip_reason,"
                "completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    seq,
                    call["turn"],
                    call["index"],
                    call["name"],
                    call["input_sha256"],
                    fields.get("result_sha256"),
                    fields.get("is_error"),
                    fields.get("truncated"),
                    int(call["allowed"]),
                    now,
                    fields["status"],
                    fields.get("skip_reason"),
                    fields.get("completed_at"),
                ),
            )
        return seq

    def tool_call_begin(self, run_id: str, call: dict, now: float) -> int:
        """Journal an allowed tool call as ``started`` before it is executed; returns its seq.

        Only digests and positions are stored: never the tool input.
        """
        self.get(run_id)
        if (
            not isinstance(call, dict)
            or set(call) != {"turn", "index", "name", "input_sha256", "allowed"}
            or not self._valid_position(call)
            or call["allowed"] is not True
            or not math.isfinite(now)
        ):
            raise ValueError("Invalid tool-call intent.")
        return self._insert_tool_row(run_id, call, now, status="started")

    def tool_call_finish(
        self,
        run_id: str,
        seq: int,
        status: str,
        now: float,
        *,
        result_sha256: str | None = None,
        is_error: bool | None = None,
        truncated: bool | None = None,
    ):
        """Transition a ``started`` row exactly once to succeeded, error or uncertain."""
        executed = status in {"succeeded", "error"}
        if (
            type(seq) is not int
            or status not in TOOL_COMPLETIONS
            or not math.isfinite(now)
            or (
                executed
                and (
                    not isinstance(result_sha256, str)
                    or not _DIGEST.fullmatch(result_sha256)
                    or is_error is not (status == "error")
                    or type(truncated) is not bool
                )
            )
            or (not executed and (result_sha256, is_error, truncated) != (None, None, None))
        ):
            raise ValueError("Invalid tool-call completion.")
        with self.db:
            cursor = self.db.execute(
                "UPDATE tool_calls SET status=?,result_sha256=?,is_error=?,truncated=?,"
                "completed_at=? WHERE run_id=? AND seq=? AND status='started'",
                (
                    status,
                    result_sha256,
                    None if is_error is None else int(is_error),
                    None if truncated is None else int(truncated),
                    now,
                    run_id,
                    seq,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Tool-call completion requires a started intent.")

    def tool_calls_interrupted(self, run_id: str, now: float) -> int:
        """Mark rows still ``started`` (their process died mid-call) uncertain; count them.

        In the same transaction each affected turn, now fully terminal, gets its tool-turn
        provenance record (subject to the outbox bound).
        """
        with self.db:
            turns = [
                found[0]
                for found in self.db.execute(
                    "SELECT DISTINCT turn FROM tool_calls WHERE run_id=? AND status='started' "
                    "ORDER BY turn",
                    (run_id,),
                )
            ]
            marked = self.db.execute(
                "UPDATE tool_calls SET status='uncertain',completed_at=? "
                "WHERE run_id=? AND status='started'",
                (now, run_id),
            ).rowcount
            for turn in turns:
                try:
                    self._tool_turn(run_id, turn)
                except ValueError:
                    pass  # Refused before any insert: the turn stays journal-only.
            return marked

    def tool_call(self, run_id: str, call: dict, now: float) -> int:
        """Append one never-executed tool-call receipt row: ``denied`` or ``refused``.

        Only digests, flags, positions and codes are stored: never the tool input or
        result. Executed tools use ``tool_call_begin``/``tool_call_finish`` instead.
        """
        self.get(run_id)
        if (
            not isinstance(call, dict)
            or set(call)
            != {
                "turn",
                "index",
                "name",
                "input_sha256",
                "result_sha256",
                "is_error",
                "truncated",
                "allowed",
                "status",
                "skip_reason",
            }
            or not self._valid_position(call)
            or not math.isfinite(now)
        ):
            raise ValueError("Invalid tool-call receipt.")
        if call["status"] == "denied":
            valid = (
                call["allowed"] is False
                and isinstance(call["result_sha256"], str)
                and bool(_DIGEST.fullmatch(call["result_sha256"]))
                and call["is_error"] is True
                and type(call["truncated"]) is bool
                and call["skip_reason"] is None
            )
            fields = {
                "result_sha256": call["result_sha256"],
                "is_error": 1,
                "truncated": int(call["truncated"]),
            }
        elif call["status"] == "refused":
            valid = (
                (call["result_sha256"], call["is_error"], call["truncated"]) == (None, None, None)
                and isinstance(call["skip_reason"], str)
                and bool(_REASON_CODE.fullmatch(call["skip_reason"]))
            )
            fields = {"skip_reason": call["skip_reason"]}
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid tool-call receipt.")
        return self._insert_tool_row(
            run_id, call, now, status=call["status"], completed_at=now, **fields
        )

    def tool_calls(self, run_id: str) -> list[dict]:
        def flag(value):
            return None if value is None else bool(value)

        return [
            {
                "seq": row["seq"],
                "turn": row["turn"],
                "index": row["tool_index"],
                "name": row["name"],
                "status": row["status"],
                "skip_reason": row["skip_reason"],
                "input_sha256": row["input_sha256"],
                "result_sha256": row["result_sha256"],
                "is_error": flag(row["is_error"]),
                "truncated": flag(row["truncated"]),
                "allowed": bool(row["allowed"]),
                "observed_at": row["observed_at"],
                "completed_at": row["completed_at"],
            }
            for row in self.db.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY seq", (run_id,)
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

    # -- durable paused tool loop ----------------------------------------------------------------
    def _pending_path(self, run_id: str, *, create: bool) -> Path | None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run identity.")
        directory = self.directory / "pending"
        if create:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Pending loop directory must be private and not a symlink.")
        return directory / f"{run_id}.json"

    def write_pending(self, run_id: str, data: bytes) -> str:
        """Atomically replace the run's private parked-loop file; returns its sha256.

        The content (the conversation so far) lives only in this 0600 file; the journal
        keeps its digest.
        """
        if not isinstance(data, bytes) or not data or len(data) > MAX_PENDING_BYTES:
            raise ValueError("Invalid pending loop state.")
        target = self._pending_path(run_id, create=True)
        temporary = target.with_name(f".{run_id}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return hashlib.sha256(data).hexdigest()

    def read_pending(self, run_id: str) -> bytes | None:
        """The parked-loop file's bytes, or None when absent. Refuses non-private files."""
        path = self._pending_path(run_id, create=False)
        if path is None:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
                or info.st_size > MAX_PENDING_BYTES
            ):
                raise ValueError("Pending loop state must be a private regular file.")
            chunks, size = [], 0
            while chunk := os.read(fd, 1024 * 1024):
                size += len(chunk)
                if size > MAX_PENDING_BYTES:
                    raise ValueError("Pending loop state is too large.")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def discard_pending(self, run_id: str):
        path = self._pending_path(run_id, create=False)
        if path is not None:
            path.unlink(missing_ok=True)

    def loop_park(self, run_id: str, turn: int, state_sha256: str, now: float) -> int:
        """Journal a parked loop by digest only (never its content)."""
        self.get(run_id)
        if (
            type(turn) is not int
            or turn < 1
            or not isinstance(state_sha256, str)
            or not _DIGEST.fullmatch(state_sha256)
            or not math.isfinite(now)
        ):
            raise ValueError("Invalid loop park receipt.")
        with self.db:
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM loop_parks WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO loop_parks VALUES(?,?,?,?,?)", (run_id, seq, turn, state_sha256, now)
            )
        return seq

    def loop_parks(self, run_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT seq,turn,state_sha256,observed_at FROM loop_parks WHERE run_id=? "
                "ORDER BY seq",
                (run_id,),
            )
        ]

    def latest_loop_park(self, run_id: str) -> dict | None:
        found = self.loop_parks(run_id)
        return found[-1] if found else None

    def loop_resume(
        self, run_id: str, turn: int, state_sha256: str, restored_from_disk: bool, now: float
    ) -> int:
        """The resume receipt, with its provenance record in the same transaction."""
        self.get(run_id)
        if (
            type(turn) is not int
            or turn < 1
            or not isinstance(state_sha256, str)
            or not _DIGEST.fullmatch(state_sha256)
            or type(restored_from_disk) is not bool
            or not math.isfinite(now)
        ):
            raise ValueError("Invalid loop resume receipt.")
        with self.db:
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM loop_resumes WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            earlier = self.db.execute(
                "SELECT COUNT(*) FROM loop_resumes WHERE run_id=? AND turn=?", (run_id, turn)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO loop_resumes VALUES(?,?,?,?,?,?)",
                (run_id, seq, turn, state_sha256, int(restored_from_disk), now),
            )
            self._record(
                run_id,
                self._attempt_name(f"resume.{turn}", earlier + 1),
                "agent",
                "started",
                content_digest="sha256:" + state_sha256,
            )
        return seq

    def loop_resumes(self, run_id: str) -> list[dict]:
        return [
            {**dict(row), "restored_from_disk": bool(row["restored_from_disk"])}
            for row in self.db.execute(
                "SELECT seq,turn,state_sha256,restored_from_disk,observed_at FROM loop_resumes "
                "WHERE run_id=? ORDER BY seq",
                (run_id,),
            )
        ]

    # -- per-turn tool provenance ----------------------------------------------------------------
    def tool_turn(self, run_id: str, turn: int) -> str | None:
        """One aggregated, content-free provenance record for a turn's journaled tool calls.

        Its content_digest is the sha256 of the canonical JSON list of that turn's tool
        receipts. Written only once every row of the turn is terminal (none ``started``);
        a turn cut off mid-call gets its record when ``tool_calls_interrupted`` marks the
        rows uncertain. Written only while the run's outbox is below
        TOOL_TURN_OUTBOX_LIMIT; the first turn past it writes one overflow record and
        later turns stay journal-only. Returns the transition written, or None.
        """
        with self.db:
            return self._tool_turn(run_id, turn)

    def _tool_turn(self, run_id: str, turn: int) -> str | None:
        """tool_turn inside the caller's transaction."""
        rows = self.db.execute(
            "SELECT tool_index,name,input_sha256,result_sha256,is_error,truncated,allowed,"
            "status,skip_reason FROM tool_calls WHERE run_id=? AND turn=? ORDER BY seq",
            (run_id, turn),
        ).fetchall()
        if not rows or any(r["status"] == "started" for r in rows):
            return None
        row = self.get(run_id)

        def exists(transition):
            record_id = (
                "runtime_" + hashlib.sha256((row["digest"] + ":" + transition).encode()).hexdigest()
            )
            return self.db.execute(
                "SELECT 1 FROM provenance_outbox WHERE record_id=?", (record_id,)
            ).fetchone()

        transition = f"tool_turn.{turn}"
        if exists("tool_turn.overflow") or exists(transition):
            return None

        def flag(value):
            return None if value is None else bool(value)

        receipts = tool_turn_receipts(
            [
                {
                    "index": r["tool_index"],
                    "name": r["name"],
                    "input_sha256": r["input_sha256"],
                    "result_sha256": r["result_sha256"],
                    "is_error": flag(r["is_error"]),
                    "truncated": flag(r["truncated"]),
                    "allowed": bool(r["allowed"]),
                    "status": r["status"],
                }
                for r in rows
            ]
        )
        statuses = [r["status"] for r in rows]
        unrun = sum(
            r["status"] == "refused" and r["skip_reason"] == TURN_LIMIT_REACHED for r in rows
        )
        summary = {
            "turn": turn,
            "calls": len(rows),
            "errors": statuses.count("error"),
            "denied": statuses.count("denied"),
            "truncated": sum(bool(r["truncated"]) for r in rows),
            "unrun": unrun,
            "refused": statuses.count("refused") - unrun,
            "uncertain": statuses.count("uncertain"),
        }
        summary = {k: min(v, 64) if k != "turn" else v for k, v in summary.items()}
        count = self.db.execute(
            "SELECT COUNT(*) FROM provenance_outbox WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        if count < TOOL_TURN_OUTBOX_LIMIT:
            if summary["uncertain"]:
                outcome = "unknown"
            elif summary["errors"]:
                outcome = "failed"
            else:
                outcome = "succeeded"
            self._record(
                run_id,
                transition,
                "tool",
                outcome,
                tool="tessera_tool_turn",
                content_digest="sha256:" + tool_turn_digest(receipts),
                tool_turn=summary,
            )
            return transition
        self._record(
            run_id,
            "tool_turn.overflow",
            "tool",
            "unknown",
            tool="tessera_tool_turn_overflow",
            tool_turn=summary,
        )
        return "tool_turn.overflow"

    def _record(
        self,
        run_id,
        transition,
        kind,
        outcome,
        *,
        tool=None,
        error=None,
        rate_limit=None,
        content_digest=None,
        tool_turn=None,
    ):
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
        if content_digest is not None:
            record["content_digest"] = content_digest
        if tool_turn is not None:
            record["tool_turn"] = tool_turn
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
