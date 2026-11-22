"""Standing control-plane loop: starts successor model calls under one fixed project policy.

Fail-closed and small. The loop only decides whether to START a new call; it never cancels,
interrupts or inspects in-flight work. Journal events carry ids, digests and reasons only,
never prompts or credentials.

Call safety. A durable `CallLedger` row is committed (`begin`) BEFORE `start_call` runs.
Only two outcomes are safely retryable: `QuotaExceeded` (provider refused before accepting)
and `CallNotStarted` (the callable asserts nothing started). Any other exception, or a ledger
row left in "started" by a crash, is `uncertain`: the paid call may have happened, so the loop
NEVER retries it automatically. It records an `uncertain_outcome` event, skips the item
(reason `uncertain_outcome`), lists it under `uncertain` in the tick summary, and waits for the
operator to call `resolve_uncertain(item_id, "succeeded" | "not_started")`.

"One call at a time" holds only if `start_call` blocks until the call is accepted or finished.
The loop starts no new call while ANY ledger row is still `started` (`started_items()`, whether
or not `observe()` still returns it); such a row can only be a crash leftover, is converted to
`uncertain`, and the tick starts nothing.

Durable by default. `StandingLoop` refuses a ledger that does not declare `durable = True`
(`LedgerNotDurable`). `InMemoryCallLedger` is accepted only with `allow_volatile_ledger=True`
(tests). The hourly call window (`starts_since`), the consecutive-failure count, the `halted`
flag and the quota backoff (`backoff_until`, `backoff_n`) are persisted through the ledger's
`loop_state` and rebuilt on construction and on every tick, so a restart can neither allow a
second `max_calls_per_hour` nor silently clear `halted`. Only `reset()` clears `halted`.

Single instance. `SqliteCallLedger` holds an exclusive advisory lock (`<path>.lock`) for its
lifetime, so a second ledger (hence loop) on the same file raises `LedgerInUse`. Consequently a
`started` row seen by a loop that holds the lock can only be a crash leftover, never a live call
of another loop. Close the ledger (`close()` / `with`) before reopening it in the same process.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WINDOW_SECONDS = 3600.0


class QuotaExceeded(Exception):
    """Raised by the model-call callable when the provider quota is exhausted."""

    def __init__(self, retry_after: float | None = None):
        super().__init__("quota exceeded")
        self.retry_after = retry_after


class CallNotStarted(Exception):
    """May be raised by the model-call callable: the call definitely did not start (retryable)."""


class LedgerNotDurable(ValueError):
    """The call ledger is missing or does not declare `durable = True`."""


class LedgerInUse(Exception):
    """Another live ledger/loop already holds the exclusive lock on this ledger file."""


class ApiKeyPresent(Exception):
    """A direct API key variable is set in the environment; the loop refuses to run."""


_API_KEY_VAR = "ANTHROPIC_API_KEY"


class SeenStore(Protocol):
    def has(self, item_id: str) -> bool: ...

    def add(self, item_id: str) -> None: ...


class CallLedger(Protocol):
    """Durable per-item call record. `begin` must be committed before it returns True.

    `begin` returns True if the item has no record or its last outcome was
    "failed_before_start" (retryable); it returns False for any other existing record.
    `state` is None, "started", "succeeded", "failed_before_start" or "uncertain".
    `begin` also logs a start timestamp (`now`, default time.time()) in the same commit.
    `durable` is True only if records survive a process restart. `starts_since(ts)` returns
    logged start times strictly greater than ts. `started_items()` lists every item whose row is
    "started". `loop_state_get/set` is a small durable string key/value store;
    `loop_state_set_many` writes several keys atomically (all or none).
    `prune_starts(before_ts)` drops start-log rows older than `before_ts`.
    """

    durable: bool

    def begin(self, item_id: str, now: float | None = None) -> bool: ...

    def finish(self, item_id: str, outcome: str) -> None: ...

    def state(self, item_id: str) -> str | None: ...

    def starts_since(self, ts: float) -> list[float]: ...

    def started_items(self) -> list[str]: ...

    def loop_state_get(self, key: str) -> str | None: ...

    def loop_state_set(self, key: str, value: str) -> None: ...

    def loop_state_set_many(self, mapping: Mapping[str, str]) -> None: ...

    def prune_starts(self, before_ts: float) -> None: ...


_OUTCOMES = ("succeeded", "failed_before_start", "uncertain")


class InMemoryCallLedger:
    """Non-durable ledger for tests (does not survive a crash); needs an explicit opt-in."""

    durable = False

    def __init__(self):
        self._rows: dict[str, str] = {}
        self._starts: list[float] = []
        self._kv: dict[str, str] = {}

    def begin(self, item_id: str, now: float | None = None) -> bool:
        if self._rows.get(item_id, "failed_before_start") != "failed_before_start":
            return False
        self._rows[item_id] = "started"
        self._starts.append(time.time() if now is None else now)
        return True

    def finish(self, item_id: str, outcome: str) -> None:
        if outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        self._rows[item_id] = outcome

    def state(self, item_id: str) -> str | None:
        return self._rows.get(item_id)

    def starts_since(self, ts: float) -> list[float]:
        return [t for t in self._starts if t > ts]

    def started_items(self) -> list[str]:
        return sorted(k for k, v in self._rows.items() if v == "started")

    def loop_state_get(self, key: str) -> str | None:
        return self._kv.get(key)

    def loop_state_set(self, key: str, value: str) -> None:
        self._kv[key] = value

    def loop_state_set_many(self, mapping: Mapping[str, str]) -> None:
        staged = dict(mapping)  # fully materialise first: all or none
        self._kv.update(staged)

    def prune_starts(self, before_ts: float) -> None:
        self._starts = [t for t in self._starts if t >= before_ts]


def _check_private(path: str) -> None:
    if os.stat(path).st_mode & 0o077:
        raise ValueError("ledger file must not be accessible by group or other (use mode 0600).")


class SqliteCallLedger:
    """sqlite ledger in a private (0600) file. Stores ids, state, timestamps and loop counters.

    An existing file with group/other permission bits is refused (ValueError), never chmodded.
    New files are created 0600. The ledger holds an exclusive non-blocking `flock` on
    `<path>.lock` until `close()` (or garbage collection); a second ledger on the same path
    raises `LedgerInUse`. So a `started` row seen by the lock holder can only be a crash leftover.

    Local filesystem only: `flock` is advisory and unreliable on network filesystems, and the lock
    is keyed on the path string, so a hard link or alternate path to the same file is not
    covered. Keep the ledger on a local disk and give the loop one canonical path.

    `calls` rows are retained for audit and never pruned; only the `starts` rate-window log is
    trimmed via `prune_starts`.
    """

    durable = True

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        self._lock_fd: int | None = None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            _check_private(self.path)  # existing (or raced-in) file: must already be private
        self._acquire_lock()
        try:
            with self._conn() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS calls ("
                    "item_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                    "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
                )
                conn.execute("CREATE TABLE IF NOT EXISTS starts (ts REAL NOT NULL)")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS loop_state "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
        except BaseException:
            self.close()
            raise

    def _acquire_lock(self) -> None:
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LedgerInUse("another loop already holds this ledger.") from exc
        self._lock_fd = fd

    def close(self) -> None:
        fd, self._lock_fd = getattr(self, "_lock_fd", None), None
        if fd is not None:
            os.close(fd)  # closing the descriptor releases the flock

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        return closing(conn)

    def begin(self, item_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO calls (item_id, state, created_at, updated_at) "
                "VALUES (?, 'started', ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
                "state='started', updated_at=excluded.updated_at "
                "WHERE calls.state='failed_before_start'",
                (item_id, now, now),
            )
            began = cur.rowcount == 1
            if began:
                conn.execute("INSERT INTO starts (ts) VALUES (?)", (now,))
            conn.execute("COMMIT")
            return began

    def finish(self, item_id: str, outcome: str) -> None:
        if outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO calls (item_id, state, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET state=excluded.state, "
                "updated_at=excluded.updated_at",
                (item_id, outcome, now, now),
            )

    def state(self, item_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT state FROM calls WHERE item_id=?", (item_id,)).fetchone()
        return row[0] if row else None

    def starts_since(self, ts: float) -> list[float]:
        with self._conn() as conn:
            rows = conn.execute("SELECT ts FROM starts WHERE ts > ? ORDER BY ts", (ts,)).fetchall()
        return [r[0] for r in rows]

    def started_items(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT item_id FROM calls WHERE state='started' ORDER BY item_id"
            ).fetchall()
        return [r[0] for r in rows]

    def loop_state_get(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM loop_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def loop_state_set(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO loop_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def loop_state_set_many(self, mapping: Mapping[str, str]) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key, value in mapping.items():
                    conn.execute(
                        "INSERT INTO loop_state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def prune_starts(self, before_ts: float) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM starts WHERE ts < ?", (before_ts,))


@dataclass(frozen=True)
class LoopPolicy:
    policy_digest: str
    identity_ref: str
    model: str
    tier: str
    max_calls_per_hour: int
    max_consecutive_failures: int
    base_backoff_seconds: float
    max_backoff_seconds: float

    def __post_init__(self):
        for name in ("policy_digest", "identity_ref", "model", "tier"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string.")
        for name in ("max_calls_per_hour", "max_consecutive_failures"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        for name in ("base_backoff_seconds", "max_backoff_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds.")


class KillSwitch:
    """Marker-file kill switch. Engaged if the file exists or its state is ambiguous."""

    def __init__(self, path: Path | None):
        self.path = path

    def engaged(self) -> bool:
        if self.path is None:
            return True
        try:
            os.lstat(self.path)
            return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
        try:
            return not self.path.parent.is_dir()
        except OSError:
            return True


class StandingLoop:
    def __init__(
        self,
        policy: LoopPolicy,
        kill_switch: KillSwitch,
        observe: Callable[[], list[dict]],
        start_call: Callable[..., object],
        record: Callable[[dict], None],
        clock: Callable[[], float],
        seen: SeenStore | None = None,
        environ: Mapping[str, str] | None = None,
        ledger: CallLedger | None = None,
        allow_volatile_ledger: bool = False,
    ):
        self.environ = os.environ if environ is None else environ
        if _API_KEY_VAR in self.environ:
            raise ApiKeyPresent(f"{_API_KEY_VAR} must not be set.")
        self.policy = policy
        self.kill_switch = kill_switch
        self.observe = observe
        self.start_call = start_call
        self.record = record
        self.clock = clock
        self.seen = seen
        if ledger is None and allow_volatile_ledger:
            ledger = InMemoryCallLedger()
        if ledger is None:
            raise LedgerNotDurable("a durable call ledger is required.")
        if getattr(ledger, "durable", False) is not True and not allow_volatile_ledger:
            raise LedgerNotDurable(
                "ledger is not durable; pass allow_volatile_ledger=True (tests)."
            )
        self.ledger: CallLedger = ledger
        self.state = "idle"
        self._uncertain: set[str] = set()
        self._uncertain_reported: set[str] = set()
        self._gate: str | None = None
        self._failures = 0
        self._quota_hits = 0
        self._backoff_until = 0.0
        self._kill_recorded = False
        self._load_state()

    def _load_state(self) -> None:
        """Rebuild failures, halted and backoff from the ledger (halted never clears here)."""
        get = self.ledger.loop_state_get
        self._failures = int(get("consecutive_failures") or 0)
        self._quota_hits = int(get("backoff_n") or 0)
        self._backoff_until = float(get("backoff_until") or 0.0)
        if get("halted") == "1":
            self.state = "halted"

    def _state_map(self) -> dict[str, str]:
        return {
            "consecutive_failures": str(self._failures),
            "backoff_n": str(self._quota_hits),
            "backoff_until": repr(self._backoff_until),
            "halted": "1" if self.state == "halted" else "0",
        }

    def _save_state(self) -> None:
        try:
            self.ledger.loop_state_set_many(self._state_map())
        except Exception as exc:  # in-memory halted stays sticky; fail closed
            self.record({"event": "ledger_error", "error": type(exc).__name__})

    def reset(self) -> None:
        """Explicit operator reset out of `halted`; clears the persisted flag too."""
        self.state = "idle"
        self._failures = 0
        self._quota_hits = 0
        self._backoff_until = 0.0
        self.ledger.loop_state_set_many(self._state_map())

    def _finished(self, item_id: str) -> bool:
        return self.ledger.state(item_id) == "succeeded" or (
            self.seen is not None and self.seen.has(item_id)
        )

    def resolve_uncertain(self, item_id: str, outcome: str) -> None:
        """Operator decision for an uncertain item: "succeeded" or "not_started" (retryable)."""
        if outcome not in ("succeeded", "not_started"):
            raise ValueError('outcome must be "succeeded" or "not_started".')
        if self.ledger.state(item_id) not in ("uncertain", "started"):
            raise ValueError("item is not in an uncertain state.")
        self.ledger.finish(
            item_id, "succeeded" if outcome == "succeeded" else "failed_before_start"
        )
        self._uncertain.discard(item_id)
        self._uncertain_reported.discard(item_id)
        if outcome == "succeeded" and self.seen is not None:
            self.seen.add(item_id)
        self.record({"event": "uncertain_resolved", "item_id": item_id, "outcome": outcome})

    def _mark_uncertain(self, item_id: str, error: str | None = None) -> None:
        self._uncertain.add(item_id)
        self._uncertain_reported.add(item_id)
        try:
            self.ledger.finish(item_id, "uncertain")
        except Exception as exc:
            self.record({"event": "ledger_error", "item_id": item_id, "error": type(exc).__name__})
        event = {"event": "uncertain_outcome", "item_id": item_id}
        if error is not None:
            event["error"] = error
        self.record(event)

    def _skip(self, reason: str, item_id: str | None = None, once: bool = False) -> dict:
        """Record a skip; with once=True only on the first tick in that gated state."""
        if not once or self._gate != reason:
            event = {"event": "skipped", "reason": reason}
            if item_id is not None:
                event["item_id"] = item_id
            self.record(event)
        if once:
            self._gate = reason
        return {"state": self.state, "started": None, "skipped": reason}

    def tick(self) -> dict:
        out = self._tick()
        out["uncertain"] = sorted(self._uncertain)
        return out

    def _tick(self) -> dict:
        now = self.clock()
        if _API_KEY_VAR in self.environ:
            return self._skip("api_key_present", once=True)
        if self.kill_switch.engaged():
            if not self._kill_recorded:
                self._kill_recorded = True
                self.record({"event": "kill_switch", "engaged": True})
            return self._skip("kill_switch", once=True)
        self._kill_recorded = False
        try:
            self._load_state()
            recent = len(self.ledger.starts_since(now - WINDOW_SECONDS))
            leftovers = self.ledger.started_items()
        except Exception as exc:  # fail closed: cannot trust limits
            self.record({"event": "ledger_error", "error": type(exc).__name__})
            return {"state": self.state, "started": None, "skipped": "ledger_error"}
        try:  # housekeeping only; the hourly window needs just the last WINDOW_SECONDS
            self.ledger.prune_starts(now - 2 * WINDOW_SECONDS)
        except Exception as exc:
            self.record({"event": "ledger_error", "error": type(exc).__name__})
        if self.state == "halted":
            return self._skip("halted", once=True)
        if self._backoff_until > now:
            self.state = "backing_off"
            return self._skip("backoff", once=True)
        if self.state == "backing_off":
            self.state = "idle"
        if leftovers:  # crash leftovers, observed or not: outcome unknown
            self._gate = None
            for item_id in leftovers:
                self._mark_uncertain(item_id)
            return {"state": self.state, "started": None, "skipped": "uncertain_outcome"}
        if recent >= self.policy.max_calls_per_hour:
            return self._skip("rate_cap", once=True)
        self._gate = None

        candidates = self.observe()
        for candidate in candidates:
            item_id = candidate.get("id")
            if not isinstance(item_id, str):
                continue
            st = self.ledger.state(item_id)
            if st == "uncertain":
                self._uncertain.add(item_id)
                if item_id not in self._uncertain_reported:
                    self._uncertain_reported.add(item_id)
                    self.record({"event": "uncertain_outcome", "item_id": item_id})

        mismatch = None
        item = None
        for candidate in candidates:
            item_id = candidate.get("id")
            if not isinstance(item_id, str) or self._finished(item_id):
                continue
            if item_id in self._uncertain:
                continue
            if (
                candidate.get("tier", self.policy.tier) != self.policy.tier
                or candidate.get("identity_ref", self.policy.identity_ref)
                != self.policy.identity_ref
            ):
                mismatch = mismatch or item_id
                self.record(
                    {"event": "skipped", "reason": "identity_or_tier_mismatch", "item_id": item_id}
                )
                continue
            item = candidate
            break
        if item is None:
            reason = "identity_or_tier_mismatch" if mismatch else "no_work"
            return {"state": self.state, "started": None, "skipped": reason}
        return self._run(item, now)

    def _run(self, item: dict, now: float) -> dict:
        p = self.policy
        item_id = item["id"]
        try:
            began = self.ledger.begin(item_id, now)
        except Exception as exc:  # fail closed: no durable record, no call
            self.record({"event": "ledger_error", "item_id": item_id, "error": type(exc).__name__})
            return {"state": self.state, "started": None, "skipped": "ledger_error"}
        if not began:
            return {"state": self.state, "started": None, "skipped": "already_begun"}
        self.record(
            {
                "event": "call_started",
                "item_id": item_id,
                "kind": item.get("kind"),
                "model": p.model,
                "tier": p.tier,
                "identity_ref": p.identity_ref,
                "policy_digest": p.policy_digest,
            }
        )
        try:
            self.start_call(
                item,
                model=p.model,
                tier=p.tier,
                identity_ref=p.identity_ref,
                policy_digest=p.policy_digest,
            )
        except QuotaExceeded as exc:
            self._set_ledger(item_id, "failed_before_start")
            self._quota_hits += 1
            delay = min(p.base_backoff_seconds * 2 ** (self._quota_hits - 1), p.max_backoff_seconds)
            if exc.retry_after is not None and exc.retry_after > delay:
                delay = float(exc.retry_after)
            self._backoff_until = self.clock() + delay
            self.state = "backing_off"
            self._save_state()
            self.record({"event": "call_finished", "item_id": item_id, "outcome": "quota"})
            self.record(
                {"event": "backoff", "until": self._backoff_until, "reason": "quota_exceeded"}
            )
            return {"state": "backing_off", "started": item_id, "skipped": None}
        except CallNotStarted as exc:
            self._set_ledger(item_id, "failed_before_start")
            self._failures += 1
            self.record(
                {
                    "event": "call_finished",
                    "item_id": item_id,
                    "outcome": "failed",
                    "error": type(exc).__name__,
                }
            )
            if self._failures >= p.max_consecutive_failures:
                self.state = "halted"
            self._save_state()
            return {"state": self.state, "started": item_id, "skipped": None}
        except Exception as exc:
            self._failures += 1
            self._mark_uncertain(item_id, type(exc).__name__)
            if self._failures >= p.max_consecutive_failures:
                self.state = "halted"
            self._save_state()
            return {"state": self.state, "started": item_id, "skipped": None}
        self._failures = 0
        self._quota_hits = 0
        self._backoff_until = 0.0
        self._save_state()
        self._set_ledger(item_id, "succeeded")
        if self.seen is not None:
            try:
                self.seen.add(item_id)
            except Exception as exc:
                self.record(
                    {"event": "ledger_error", "item_id": item_id, "error": type(exc).__name__}
                )
        self.record({"event": "call_finished", "item_id": item_id, "outcome": "ok"})
        return {"state": self.state, "started": item_id, "skipped": None}

    def _set_ledger(self, item_id: str, outcome: str) -> None:
        try:
            self.ledger.finish(item_id, outcome)
        except Exception as exc:  # row stays "started" -> treated as uncertain later
            self.record({"event": "ledger_error", "item_id": item_id, "error": type(exc).__name__})
