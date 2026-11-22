"""Default research/review job creation when a plan is admitted or advanced.

The planner submits bounded review jobs (``SubmitReviewJob`` bodies, ADR 0084)
through an injected submitter and keeps a small private sqlite ledger of what it created
or skipped. It stores identities and budgets only: never prompts, credentials or
findings. It exposes no way to accept, approve or decide findings, or to write a gate.

Provenance: ``created_by`` ("agent_loop") is a ledger-recorded constant. The review-job
submit (ADR 0084) has no created_by field; the actor is the loop's own enrolled agent credential.

Private inputs (ADR 0047): the planner passes each input's data_class through and sets
egress to "model_runtime" when any input is non-public, else "none"; it never widens
egress. Whether a destination may receive private data is the submitter's destination
policy (ADR 0084 ``policy.check``); its 409 refusals arrive as ``ReviewSubmitRefused`` and are
recorded as final skips.

Skips: a refusal or cap/disabled/sealed skip is FINAL and deliberately permanent per
(trigger, class); it is never retried or overwritten. Only ``submit_failed`` (any other
submitter exception) is retryable, and a later final skip or created job replaces it.

Concurrency: the write lock is NEVER held across the submitter call. Each (trigger, class)
is processed reserve-then-submit-then-commit. (1) A short ``BEGIN IMMEDIATE`` transaction
re-checks for an existing job or final skip, counts caps over committed jobs PLUS live
reservations, and inserts a ``reservations`` row (or records a final skip); (2) the submit
runs with no lock held, so a slow submit never blocks another writer; (3) a second short
transaction deletes the reservation and records the job, a final skip (refusal) or a
retryable ``submit_failed``. Because reservations count toward ``max_jobs_total`` and
``max_total_budget_cents``, two processes on one ledger file cannot overshoot the caps.
The guarantee: caps are never exceeded by new reservations; a job that already exists
upstream is always recorded, and is flagged ``over_cap`` if an operator released its
reservation and the slot was taken. A reservation is never ignored or deleted
because of its age: a reservation older than ``reservation_ttl_seconds`` (default 900) is
"stale" but KEEPS counting against both caps, so a crashed or slow submit may hold a slot
until it is replayed or released, never longer than that and never at the caps' expense.
A stale reservation is released only by (a) a replay of the SAME (trigger, class), which
takes it over (timestamp reset), re-submits with the same deterministic idempotency key
(the submitter dedupes) and completes, or (b) the operator call
``AutoJobLedger.release_reservation``. Each reserve/takeover issues a random owner token
stored on the reservation row and ``complete`` acts only while it still matches: a
takeover orphans the previous owner, whose late failure/refusal/submit_failed changes
nothing (result skipped/``stale_owner``, retryable) so it can never free or overwrite the
current owner's slot. Every such dropped failure/refusal leaves a receipt row in the
``stale_completions`` table (``list_stale_completions``). A stale owner's late success is
recorded if no job exists yet (same idempotency key, same upstream job) and drops the
reservation, so the slot is converted to a job exactly once; the current owner's later
complete is then a no-op ``existing``. If the reservation row is already gone (operator
release) and other jobs took the slot, the late success is STILL recorded exactly once
(the job exists upstream and must be on the record) and, when that pushes committed jobs
or cents above the caps, the ``jobs`` row carries ``over_cap=1`` and the planner result
carries ``over_cap: True``.
Legacy ledger files are migrated in place (nullable ``owner`` column, NULL matches
any; ``over_cap`` column on ``jobs``; ``stale_completions`` table).
A reservation younger than the ttl for the same key
yields a retryable ``skipped``/``in_flight`` result. The submitter must bound its own
duration (see ``AutoJobPlanner.submit_timeout_seconds``).
A "database is locked" error on a short transaction is retried a few times with backoff,
then returns retryable ``skipped``/``ledger_busy``. If the submit succeeded but the
completing transaction still cannot get the lock, the result is ``created_unrecorded``
carrying the job id (never dropped); the reservation stays and counts, and a replay of the
same trigger re-submits with the same key and records the job. Retryable results
(``in_flight``, ``ledger_busy``, ``submit_failed``) are not final.

Ledger files are created mode 0600; an existing file readable by group/other is refused.
Jobs are retained for audit and never pruned; ``AutoJobLedger.prune`` removes only old skip
rows (final skips and ``submit_failed``) and old stale-completion receipts, never jobs or
reservations. Pruning a final skip
makes that (trigger, class) eligible for processing again.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

DEFAULT_JOB_CLASSES = (
    "plan_review",
    "dataset_scout",
    "technique_scout",
    "pre_gate_review",
)
TIERS = ("haiku", "sonnet", "opus", "fable")
DATA_CLASSES = ("public", "private_derived", "private_raw")
MAX_BUDGET_CENTS = 10000
CREATED_BY = "agent_loop"
EVENTS = ("admitted", "advanced")
_RETRYABLE = "submit_failed"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
BUSY_TIMEOUT_SECONDS = 5.0
DEFAULT_RESERVATION_TTL_SECONDS = 900.0
BUSY_RETRIES = 3
BUSY_BACKOFF_SECONDS = 0.05


class ReviewSubmitRefused(Exception):
    """Policy refusal from the submitter (mirrors ADR 0084's ReviewRefused, HTTP 409)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ResearchJobSubmitter(Protocol):
    def submit(self, project_id: str, key: str, payload: dict) -> dict:
        """Submit a SubmitReviewJob body; ``key`` is the Idempotency-Key.

        Returns a job record dict with "id". Raises ReviewSubmitRefused on a 409.
        """
        ...


def idempotency_key(project_id: str, trigger_id: str, kind: str) -> str:
    raw = f"agent_loop|{project_id}|{trigger_id}|{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _need_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string.")


def _need_digest(name: str, value: object) -> None:
    if not isinstance(value, str) or not _SHA256.match(value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest.")


@dataclass(frozen=True)
class AutoJobPolicy:
    project_id: str
    classes: tuple[str, ...]
    tier: str
    per_job_cap_cents: int
    max_jobs_total: int
    max_total_budget_cents: int
    destination_id: str
    prompt_digests: Mapping[str, str]
    enabled: bool = True

    def __post_init__(self):
        _need_text("project_id", self.project_id)
        _need_text("destination_id", self.destination_id)
        if len(self.destination_id) > 200:
            raise ValueError("destination_id must be at most 200 characters.")
        if self.tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}.")
        if not isinstance(self.classes, tuple) or not self.classes:
            raise ValueError("classes must be a non-empty tuple.")
        if len(set(self.classes)) != len(self.classes):
            raise ValueError("classes must not repeat.")
        if any(c not in DEFAULT_JOB_CLASSES for c in self.classes):
            raise ValueError("classes must be a subset of the closed default set.")
        if not isinstance(self.prompt_digests, Mapping):
            raise ValueError("prompt_digests must be a mapping.")
        for c in self.classes:
            _need_digest(f"prompt_digests[{c}]", self.prompt_digests.get(c))
        for name in ("per_job_cap_cents", "max_jobs_total", "max_total_budget_cents"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer.")
        if not 0 < self.per_job_cap_cents <= MAX_BUDGET_CENTS:
            raise ValueError("per_job_cap_cents must be in 1..10000.")
        if self.max_jobs_total < 0 or self.max_total_budget_cents < 0:
            raise ValueError("totals must not be negative.")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean.")


@dataclass(frozen=True)
class PlanTrigger:
    trigger_id: str
    event: str
    subject_type: str
    subject_id: str
    subject_digest: str
    inputs: tuple[tuple[str, str, str], ...]
    sealed: bool = False

    def __post_init__(self):
        for name in ("trigger_id", "subject_type", "subject_id", "subject_digest"):
            _need_text(name, getattr(self, name))
        if self.event not in EVENTS:
            raise ValueError("event must be 'admitted' or 'advanced'.")
        if not isinstance(self.sealed, bool):
            raise ValueError("sealed must be a boolean.")
        if not isinstance(self.inputs, tuple) or not 1 <= len(self.inputs) <= 16:
            raise ValueError("inputs must be a tuple of 1 to 16 entries.")
        for item in self.inputs:
            if not isinstance(item, tuple) or len(item) != 3:
                raise ValueError("each input must be (snapshot_id, digest, data_class).")
            snapshot_id, digest, data_class = item
            _need_text("snapshot_id", snapshot_id)
            if len(snapshot_id) > 200:
                raise ValueError("snapshot_id must be at most 200 characters.")
            _need_digest("input digest", digest)
            if data_class not in DATA_CLASSES:
                raise ValueError(f"data_class must be one of {DATA_CLASSES}.")


class AutoJobLedger:
    """Private journal of auto-created jobs and recorded skips."""

    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self._clock = clock
        if str(path) != ":memory:":
            path = os.fspath(path)
            try:
                os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                pass  # existing, or another process just created it: verify mode below
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                raise ValueError("ledger file must not be accessible by group or others.")
        self.db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
        self._depth = 0
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS jobs (
            project_id TEXT NOT NULL, trigger_id TEXT NOT NULL, job_class TEXT NOT NULL,
            job_id TEXT NOT NULL, created_by TEXT NOT NULL, state TEXT NOT NULL,
            cost_cap_cents INTEGER NOT NULL, created_at REAL NOT NULL,
            over_cap INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(project_id, trigger_id, job_class));
          CREATE TABLE IF NOT EXISTS skips (
            project_id TEXT NOT NULL, trigger_id TEXT NOT NULL, job_class TEXT NOT NULL,
            reason TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(project_id, trigger_id, job_class));
          CREATE TABLE IF NOT EXISTS reservations (
            project_id TEXT NOT NULL, trigger_id TEXT NOT NULL, job_class TEXT NOT NULL,
            cost_cap_cents INTEGER NOT NULL, created_at REAL NOT NULL, owner TEXT,
            PRIMARY KEY(project_id, trigger_id, job_class));
          CREATE TABLE IF NOT EXISTS stale_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, trigger_id TEXT NOT NULL, job_class TEXT NOT NULL,
            kind TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
        """)
        self._add_column("reservations", "owner", "TEXT")  # from before owner tokens
        self._add_column("jobs", "over_cap", "INTEGER NOT NULL DEFAULT 0")

    def _add_column(self, table: str, column: str, decl: str) -> None:
        """Migrate an older ledger file in place (idempotent, safe against a racing opener)."""
        cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            return
        try:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():  # another process won the race
                raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """BEGIN IMMEDIATE ... COMMIT (re-entrant); rolls back on any exception."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self.db.execute("ROLLBACK")
            raise
        self._depth = 0
        self.db.execute("COMMIT")

    def close(self):
        self.db.close()

    def has_job(self, project_id: str, trigger_id: str, job_class: str) -> bool:
        return self.get_job(project_id, trigger_id, job_class) is not None

    def get_job(self, project_id: str, trigger_id: str, job_class: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE project_id=? AND trigger_id=? AND job_class=?",
            (project_id, trigger_id, job_class),
        ).fetchone()
        return dict(row) if row else None

    def get_skip(self, project_id: str, trigger_id: str, job_class: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM skips WHERE project_id=? AND trigger_id=? AND job_class=?",
            (project_id, trigger_id, job_class),
        ).fetchone()
        return dict(row) if row else None

    def apply(self, project_id: str, trigger_id: str, jobs: list[dict], skips: list[dict]) -> None:
        """Write every job and skip for one trigger in a single transaction."""
        now = self._clock()
        with self.transaction():
            for j in jobs:
                self.db.execute(
                    "INSERT INTO jobs(project_id, trigger_id, job_class, job_id, created_by,"
                    " state, cost_cap_cents, created_at, over_cap) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        trigger_id,
                        j["job_class"],
                        j["job_id"],
                        j["created_by"],
                        j.get("state", "submitted"),
                        j["cost_cap_cents"],
                        now,
                        1 if j.get("over_cap") else 0,
                    ),
                )
                # A created job supersedes any earlier skip row for the class.
                self.db.execute(
                    "DELETE FROM skips WHERE project_id=? AND trigger_id=? AND job_class=?",
                    (project_id, trigger_id, j["job_class"]),
                )
            for s in skips:
                # Replaces only a retryable submit_failed row; never another final skip.
                self.db.execute(
                    "INSERT INTO skips VALUES(?,?,?,?,?) "
                    "ON CONFLICT(project_id, trigger_id, job_class) DO UPDATE SET "
                    "reason=excluded.reason, created_at=excluded.created_at "
                    "WHERE skips.reason=?",
                    (project_id, trigger_id, s["job_class"], s["reason"], now, _RETRYABLE),
                )

    def reserve(
        self,
        project_id: str,
        trigger_id: str,
        job_class: str,
        cost_cap_cents: int,
        max_jobs_total: int,
        max_total_budget_cents: int,
        ttl: float,
        now: float,
        static_skip: str | None = None,
    ) -> tuple[str, str | None]:
        """One short IMMEDIATE txn. Returns (outcome, detail).

        Outcomes: "existing" (detail job_id), "skipped" (detail reason, final),
        "in_flight" (a reservation for this key younger than ``ttl``), "reserved" (detail is
        the owner token the caller must pass to ``complete`` after submitting; includes
        taking over a stale reservation for the same key, which issues a NEW token and so
        orphans the previous owner). Reservations of any age count toward the caps.
        """
        with self.transaction():
            job = self.get_job(project_id, trigger_id, job_class)
            if job:
                return "existing", job["job_id"]
            prior = self.get_skip(project_id, trigger_id, job_class)
            if prior and prior["reason"] != _RETRYABLE:
                return "skipped", prior["reason"]
            own = self.db.execute(
                "SELECT created_at FROM reservations "
                "WHERE project_id=? AND trigger_id=? AND job_class=?",
                (project_id, trigger_id, job_class),
            ).fetchone()
            if own and own["created_at"] > now - ttl:
                return "in_flight", None
            if static_skip is not None:
                if own:  # stale: this replay settles it
                    self.release_reservation(project_id, trigger_id, job_class)
                self.record_skip(project_id, trigger_id, job_class, static_skip)
                return "skipped", static_skip
            if own:
                # Stale, same key: take over. Its slot is already counted in the caps.
                token = uuid.uuid4().hex
                self.db.execute(
                    "UPDATE reservations SET created_at=?, owner=? "
                    "WHERE project_id=? AND trigger_id=? AND job_class=?",
                    (now, token, project_id, trigger_id, job_class),
                )
                return "reserved", token
            count, cents = self.totals(project_id)
            rcount, rcents = self.reserved_totals(project_id)
            if (
                count + rcount >= max_jobs_total
                or cents + rcents + cost_cap_cents > max_total_budget_cents
            ):
                self.record_skip(project_id, trigger_id, job_class, "cap_exhausted")
                return "skipped", "cap_exhausted"
            token = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO reservations(project_id, trigger_id, job_class, cost_cap_cents,"
                " created_at, owner) VALUES(?,?,?,?,?,?)",
                (project_id, trigger_id, job_class, cost_cap_cents, now, token),
            )
            return "reserved", token

    def complete(
        self,
        project_id: str,
        trigger_id: str,
        job_class: str,
        cost_cap_cents: int,
        job_id: str | None = None,
        skip_reason: str | None = None,
        owner: str | None = None,
        max_jobs_total: int | None = None,
        max_total_budget_cents: int | None = None,
    ) -> str:
        """One short IMMEDIATE txn settling a reservation. Returns the outcome.

        ``owner`` is the token ``reserve`` returned. Outcomes: "recorded" (job or skip
        written, reservation dropped), "recorded_over_cap" (a job was recorded and, with the
        optional ``max_jobs_total``/``max_total_budget_cents`` given, committed jobs or cents
        now exceed them: the row has ``over_cap=1``), "existing" (a job already exists for the
        key; the
        reservation is dropped, nothing else written), "stale" (the reservation now belongs
        to another owner and this is a failure/refusal: nothing is changed except a receipt
        row in ``stale_completions``, written in the same transaction).

        A caller that omits ``owner`` against a reservation row that has a token never
        matches it, so its failures/refusals always return "stale" (fail-safe: it can
        never free or overwrite someone else's slot). Only a NULL-owner (legacy) row
        matches an omitted owner.

        A stale owner's late SUCCESS is still recorded (same key means the same upstream
        job) and the reservation is dropped regardless of owner, since the job now occupies
        the slot; the current owner's later complete is then "existing". A reservation row
        with a NULL owner (legacy) matches any token.
        """
        with self.transaction():
            row = self.db.execute(
                "SELECT owner FROM reservations "
                "WHERE project_id=? AND trigger_id=? AND job_class=?",
                (project_id, trigger_id, job_class),
            ).fetchone()
            current = row is None or row["owner"] is None or row["owner"] == owner
            if job_id is None and not current:
                self.db.execute(
                    "INSERT INTO stale_completions(project_id, trigger_id, job_class, kind,"
                    " reason, created_at) VALUES(?,?,?,?,?,?)",
                    (
                        project_id,
                        trigger_id,
                        job_class,
                        "refused" if skip_reason not in (None, _RETRYABLE) else "submit_failed",
                        skip_reason or _RETRYABLE,
                        self._clock(),
                    ),
                )
                return "stale"
            self.db.execute(
                "DELETE FROM reservations WHERE project_id=? AND trigger_id=? AND job_class=?",
                (project_id, trigger_id, job_class),
            )
            if self.has_job(project_id, trigger_id, job_class):
                return "existing"
            if job_id is not None:
                count, cents = self.totals(project_id)
                over = (max_jobs_total is not None and count + 1 > max_jobs_total) or (
                    max_total_budget_cents is not None
                    and cents + cost_cap_cents > max_total_budget_cents
                )
                self.record_job(
                    project_id,
                    trigger_id,
                    job_class,
                    job_id,
                    CREATED_BY,
                    cost_cap_cents,
                    over_cap=over,
                )
                return "recorded_over_cap" if over else "recorded"
            elif skip_reason is not None:
                self.record_skip(project_id, trigger_id, job_class, skip_reason)
            return "recorded"

    def reserved_totals(self, project_id: str) -> tuple[int, int]:
        """Count and cents of ALL reservations, stale ones included."""
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_cap_cents),0) FROM reservations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def release_reservation(self, project_id: str, trigger_id: str, job_class: str) -> bool:
        """Operator call: drop one reservation, freeing its slot. True if one existed.

        Use only when the owning submit is known to be dead; releasing a reservation whose
        submit is still running can let the caps be exceeded.
        """
        with self.transaction():
            cur = self.db.execute(
                "DELETE FROM reservations WHERE project_id=? AND trigger_id=? AND job_class=?",
                (project_id, trigger_id, job_class),
            )
            return cur.rowcount > 0

    def prune(self, project_id: str, before_ts: float) -> int:
        """Delete skip rows (final skips and submit_failed) and stale-completion receipts
        created before ``before_ts``.

        Jobs are retained for audit and live reservations are never touched. Returns the
        number of rows deleted.
        """
        with self.transaction():
            cur = self.db.execute(
                "DELETE FROM skips WHERE project_id=? AND created_at<?",
                (project_id, before_ts),
            )
            n = cur.rowcount
            cur = self.db.execute(
                "DELETE FROM stale_completions WHERE project_id=? AND created_at<?",
                (project_id, before_ts),
            )
            return n + cur.rowcount

    def list_stale_completions(self, project_id: str) -> list[dict]:
        """Receipts for failures/refusals dropped because their owner was orphaned."""
        rows = self.db.execute(
            "SELECT * FROM stale_completions WHERE project_id=? ORDER BY id",
            (project_id,),
        )
        return [dict(r) for r in rows]

    def list_reservations(self, project_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM reservations WHERE project_id=? ORDER BY created_at, trigger_id",
            (project_id,),
        )
        return [dict(r) for r in rows]

    def record_job(
        self,
        project_id,
        trigger_id,
        job_class,
        job_id,
        created_by,
        cost_cap_cents,
        state="submitted",
        over_cap=False,
    ):
        self.apply(
            project_id,
            trigger_id,
            [
                {
                    "job_class": job_class,
                    "job_id": job_id,
                    "created_by": created_by,
                    "cost_cap_cents": cost_cap_cents,
                    "state": state,
                    "over_cap": over_cap,
                }
            ],
            [],
        )

    def record_skip(self, project_id, trigger_id, job_class, reason):
        self.apply(project_id, trigger_id, [], [{"job_class": job_class, "reason": reason}])

    def totals(self, project_id: str) -> tuple[int, int]:
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_cap_cents),0) FROM jobs WHERE project_id=?",
            (project_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def list_jobs(self, project_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at, trigger_id, job_class",
            (project_id,),
        )
        return [dict(r) for r in rows]

    def list_skips(self, project_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM skips WHERE project_id=? ORDER BY created_at, trigger_id, job_class",
            (project_id,),
        )
        return [dict(r) for r in rows]


class AutoJobPlanner:
    """Creates the policy's default jobs for a trigger, at most once per class."""

    def __init__(
        self,
        policy: AutoJobPolicy,
        ledger: AutoJobLedger,
        submitter: ResearchJobSubmitter,
        clock: Callable[[], float] = time.time,
        reservation_ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        busy_retries: int = BUSY_RETRIES,
        submit_timeout_seconds: float | None = None,
    ):
        self.policy = policy
        self.ledger = ledger
        self.submitter = submitter
        self.clock = clock
        self.reservation_ttl_seconds = reservation_ttl_seconds
        self._sleep = sleep
        self._busy_retries = busy_retries
        # Contract the submitter must meet (not enforced here, no threads): a submit must
        # return or raise within this many seconds. Keep it well below
        # ``reservation_ttl_seconds``: a reservation older than the ttl is treated as stale
        # (still counted in the caps) and a replay of the same key may take it over.
        self.submit_timeout_seconds = submit_timeout_seconds

    def _short_txn(self, fn: Callable[[], object]) -> tuple[bool, object]:
        """Run a short ledger txn, retrying 'database is locked'. -> (ok, value)."""
        for attempt in range(self._busy_retries + 1):
            try:
                return True, fn()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                if attempt < self._busy_retries:
                    self._sleep(BUSY_BACKOFF_SECONDS * (attempt + 1))
        return False, None

    def _static_skip(self, trigger: PlanTrigger) -> str | None:
        if not self.policy.enabled:
            return "disabled"
        if trigger.sealed:
            return "input_sealed"
        return None

    def _payload(self, trigger: PlanTrigger, kind: str) -> dict:
        p = self.policy
        private = any(dc != "public" for _, _, dc in trigger.inputs)
        title = f"{kind} for {trigger.subject_type} {trigger.subject_id}"[:200]
        return {
            "kind": kind,
            "title": title,
            # ADR 0084: control derives each input's data class from its own record and refuses
            # a body that supplies one, so only the snapshot id and digest are sent. The class
            # given on the trigger decides ``egress`` here and nothing else.
            "inputs": [{"snapshot_id": sid, "digest": dg} for sid, dg, _ in trigger.inputs],
            "prompt_digest": p.prompt_digests[kind],
            "destination_id": p.destination_id,
            "tier": p.tier,
            "budget_cents": p.per_job_cap_cents,
            "egress": "model_runtime" if private else "none",
        }

    def on_trigger(self, trigger: PlanTrigger) -> list[dict]:
        p = self.policy
        static = self._static_skip(trigger)
        results: list[dict] = []
        for job_class in p.classes:
            results.append(self._one(trigger, job_class, static))
        return results

    def _one(self, trigger: PlanTrigger, job_class: str, static: str | None) -> dict:
        p = self.policy
        ledger = self.ledger
        busy = {"job_class": job_class, "status": "skipped", "reason": "ledger_busy"}
        ok, out = self._short_txn(
            lambda: ledger.reserve(
                p.project_id,
                trigger.trigger_id,
                job_class,
                p.per_job_cap_cents,
                p.max_jobs_total,
                p.max_total_budget_cents,
                self.reservation_ttl_seconds,
                self.clock(),
                static,
            )
        )
        if not ok:
            return busy
        outcome, detail = out  # type: ignore[misc]
        token = detail if outcome == "reserved" else None
        if outcome == "existing":
            return {"job_class": job_class, "status": "existing", "job_id": detail}
        if outcome == "skipped":
            return {"job_class": job_class, "status": "skipped", "reason": detail}
        if outcome == "in_flight":
            return {"job_class": job_class, "status": "skipped", "reason": "in_flight"}
        # Reserved: submit with NO ledger lock held.
        key = idempotency_key(p.project_id, trigger.trigger_id, job_class)
        job_id = None
        reason = None
        try:
            resp = self.submitter.submit(p.project_id, key, self._payload(trigger, job_class))
            job_id = resp["id"]
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("submitter returned no job id")
        except ReviewSubmitRefused as exc:
            job_id, reason = None, str(exc.reason)
        except Exception:
            job_id, reason = None, _RETRYABLE

        def complete() -> str:
            return ledger.complete(
                p.project_id,
                trigger.trigger_id,
                job_class,
                p.per_job_cap_cents,
                job_id=job_id,
                skip_reason=reason,
                owner=token,
                max_jobs_total=p.max_jobs_total,
                max_total_budget_cents=p.max_total_budget_cents,
            )

        ok, settled = self._short_txn(complete)
        if not ok:
            # One more, final attempt after a longer backoff.
            self._sleep(BUSY_BACKOFF_SECONDS * (self._busy_retries + 2))
            try:
                settled = complete()
                ok = True
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
        if not ok:
            # The reservation stays (still counted); replay re-submits with the same key.
            if job_id is not None:
                return {
                    "job_class": job_class,
                    "status": "created_unrecorded",
                    "job_id": job_id,
                    "reason": "ledger_busy",
                }
            return busy
        if settled == "stale":
            # Another replay took over this reservation: leave it and the ledger alone.
            return {"job_class": job_class, "status": "skipped", "reason": "stale_owner"}
        if job_id is not None:
            status = "existing" if settled == "existing" else "created"
            res = {"job_class": job_class, "status": status, "job_id": job_id}
            if settled == "recorded_over_cap":
                res["over_cap"] = True
            return res
        return {"job_class": job_class, "status": "skipped", "reason": reason}
