"""Durable record-before-act outbox for watch reports (ADR 0087 section 8, ADR 0054).

Every report is committed here BEFORE any delivery attempt. Delivery is redelivery of the
recorded body under its stable idempotency key. A report still undelivered 24 h after it was
recorded is abandoned and counted, so the gap is explicit, never silence; a ``stuck``/``failed``
is never abandoned while a later report is still pending. A report control permanently
refused (a status in ``refusal_statuses(kind)``) is settled as ``refused``: counted, never
retried, and it never blocks later reports. The database and its rollback journal are
owner-only (0600; the journal is pre-created and kept in TRUNCATE mode so SQLite never
recreates it under the umask) and store ids, closed report bodies and times only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tessera_control.stage_watch_limits import TERMINAL_KINDS

from .report import WatchReport, refusal_statuses

ABANDON_AFTER = timedelta(hours=24)
JOURNAL_SUFFIX = "-journal"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS reports ("
    "seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, body TEXT NOT NULL, "
    "recorded_at REAL NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
    "settled_at REAL, refused_status INTEGER)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


@dataclass(frozen=True)
class OutboxGaps:
    undelivered: int
    abandoned: int
    last_delivered_seq: int | None
    refused: int = 0
    budget_dropped: int = 0

    @property
    def dropped(self) -> int:
        """Reports control never got: abandoned after 24 h or dropped at the total cap."""
        return self.abandoned + self.budget_dropped


def _owner_only_file(path: str) -> None:
    """Create or open ``path`` without following a symlink and force it to 0600."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        raise ValueError("outbox files must be regular files, not symlinks.") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("outbox files must be regular files.")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("time must be a timezone-aware datetime.")
    return value


class WatchOutbox:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        try:
            parent_mode = os.stat(parent).st_mode
        except OSError as error:
            raise ValueError("outbox parent directory must exist.") from error
        if not stat.S_ISDIR(parent_mode):
            raise ValueError("outbox parent must be a directory.")
        # The journal is pre-created owner-only and, in TRUNCATE mode, SQLite truncates it
        # after each transaction instead of deleting it, so it is never recreated under the
        # process umask. TRUNCATE is a rollback journal: crash-safe, and no -wal/-shm files.
        _owner_only_file(self.path)
        _owner_only_file(self.path + JOURNAL_SUFFIX)
        with self._conn() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(reports)")}
            if "refused_status" not in columns:
                conn.execute("ALTER TABLE reports ADD COLUMN refused_status INTEGER")

    def _conn(self) -> closing[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            mode = conn.execute("PRAGMA journal_mode=TRUNCATE").fetchone()[0]
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        if str(mode).lower() != "truncate":
            conn.close()
            raise ValueError("outbox journal mode could not be set to TRUNCATE.")
        return closing(conn)

    # -- binding ---------------------------------------------------------------------------

    def bind(self, operation_id: str, contract_digest: str, started_at: datetime) -> datetime:
        """Bind this outbox to one contract; return the first recorded watch start.

        A restarted watcher keeps the original start (so a restart never extends the watch
        deadline). An outbox bound to another operation or contract is refused.
        """
        _aware(started_at)
        wanted = {"operation_id": operation_id, "contract_digest": contract_digest}
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key, value in wanted.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, value)
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('started_at', ?)",
                    (started_at.isoformat(),),
                )
                rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        for key, value in wanted.items():
            if rows.get(key) != value:
                raise ValueError("outbox is bound to another operation or contract.")
        return datetime.fromisoformat(rows["started_at"])

    # -- record before act -----------------------------------------------------------------

    def record(self, report: WatchReport) -> None:
        """Durably persist one report before any delivery attempt. A seq is recorded once."""
        body = json.dumps(report.to_body(), sort_keys=True, separators=(",", ":"))
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO reports (seq, operation_id, body, recorded_at, state) "
                    "VALUES (?, ?, ?, ?, 'pending')",
                    (report.seq, report.operation_id, body, report.observed_at.timestamp()),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("report seq is already recorded.") from error

    def pending(self) -> list[WatchReport]:
        """Pending reports in flush order: ``stuck``/``failed`` first, each group by seq.

        A routine report that keeps failing transiently therefore never delays a closing one.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT operation_id, body FROM reports WHERE state='pending' ORDER BY seq"
            ).fetchall()
        reports = [
            WatchReport.from_body(operation_id, json.loads(body)) for operation_id, body in rows
        ]
        return sorted(reports, key=lambda r: (r.kind not in TERMINAL_KINDS, r.seq))

    def recorded(self) -> list[WatchReport]:
        """Every recorded report, in any state, by seq (bounded by the contract's cap)."""
        with self._conn() as conn:
            rows = conn.execute("SELECT operation_id, body FROM reports ORDER BY seq").fetchall()
        return [
            WatchReport.from_body(operation_id, json.loads(body)) for operation_id, body in rows
        ]

    def note_attempt(self, seq: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE reports SET attempts=attempts+1 WHERE seq=? AND state='pending'", (seq,)
            )

    def mark_delivered(self, seq: int, now: datetime | None = None) -> None:
        settled = (now or datetime.now(UTC)).timestamp()
        with self._conn() as conn:
            conn.execute(
                "UPDATE reports SET state='delivered', settled_at=? "
                "WHERE seq=? AND state='pending'",
                (settled, seq),
            )

    def mark_refused(self, seq: int, status: int, now: datetime | None = None) -> None:
        """Settle a report control permanently refused: never retried, counted in gaps().

        Only a status in ``refusal_statuses(kind)`` settles; anything else (for example a 401
        or a 409 for ``failed``) is refused here, because the report must stay pending.
        """
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status < 500:
            raise ValueError("a refusal status must be a 4xx integer.")
        settled = (now or datetime.now(UTC)).timestamp()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT body FROM reports WHERE seq=? AND state='pending'", (seq,)
                ).fetchone()
                if row is not None and status not in refusal_statuses(json.loads(row[0])["kind"]):
                    raise ValueError("this status does not settle a report of this kind.")
                conn.execute(
                    "UPDATE reports SET state='refused', settled_at=?, refused_status=? "
                    "WHERE seq=? AND state='pending'",
                    (settled, status, seq),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def refused_status(self, seq: int) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT refused_status FROM reports WHERE seq=? AND state='refused'", (seq,)
            ).fetchone()
        return None if row is None else row[0]

    def abandon_expired(self, now: datetime) -> int:
        """Abandon reports pending for 24 h or more; they stay counted in `gaps()`.

        A ``stuck``/``failed`` is abandoned only once no later report is still pending, so a
        closing report is never given up while the outbox still holds newer work for control.
        """
        cutoff = (_aware(now) - ABANDON_AFTER).timestamp()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT seq, body, recorded_at FROM reports WHERE state='pending' "
                    "ORDER BY seq DESC"
                ).fetchall()
                abandoned: list[int] = []
                later_pending = False
                for seq, body, recorded_at in rows:  # newest first
                    terminal = json.loads(body)["kind"] in TERMINAL_KINDS
                    if recorded_at <= cutoff and not (terminal and later_pending):
                        abandoned.append(seq)
                    else:
                        later_pending = True
                conn.executemany(
                    "UPDATE reports SET state='abandoned', settled_at=? WHERE seq=?",
                    [(now.timestamp(), seq) for seq in abandoned],
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return len(abandoned)

    def gaps(self) -> OutboxGaps:
        with self._conn() as conn:
            counts = dict(
                conn.execute("SELECT state, COUNT(*) FROM reports GROUP BY state").fetchall()
            )
            last = conn.execute("SELECT MAX(seq) FROM reports WHERE state='delivered'").fetchone()[
                0
            ]
            row = conn.execute("SELECT value FROM meta WHERE key='budget_dropped'").fetchone()
        return OutboxGaps(
            undelivered=counts.get("pending", 0),
            abandoned=counts.get("abandoned", 0),
            last_delivered_seq=last,
            refused=counts.get("refused", 0),
            budget_dropped=0 if row is None else int(row[0]),
        )

    def pending_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM reports WHERE state='pending'").fetchone()[0]

    def next_seq(self) -> int:
        with self._conn() as conn:
            value = conn.execute("SELECT MAX(seq) FROM reports").fetchone()[0]
        return 0 if value is None else value + 1

    # -- local markers ---------------------------------------------------------------------

    def mark_budget_exhausted(self) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('budget_exhausted', '1')")

    def note_budget_drop(self) -> None:
        """Count a closing report that could not be recorded because the total cap was hit."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('budget_dropped', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('budget_exhausted', '1')"
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def budget_exhausted(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM meta WHERE key='budget_exhausted'").fetchone()
        return row is not None
