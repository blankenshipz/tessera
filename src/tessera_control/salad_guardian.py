"""Stop-only independent service core, with no create/start capability.

Deploy under a separate supervisor, private state and provider read/stop identity;
do not host in control/proxy/workload. Callbacks are server-owned authenticated
provider adapters, never request-supplied claims. A provider terminal observation
plus sealed external custody proves only this bounded provider/custody boundary:
node-local no-continued-writes and final billing remain explicitly unknown.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .salad_custody import CustodyBinding, SaladCustody, private_database

# Guardian event kind prefix recording why a tick first decided to stop.
STOP_CAUSE_PREFIX = "stop_cause:"
RECONCILE_OPERATOR = "operator_reconciled_exact_group_stopped"
RECONCILE_PROVIDER = "provider_confirmed_exact_group_stopped"
# reason -> guardian event kind
RECONCILE_REASONS = {
    RECONCILE_OPERATOR: "operator_reconciled",
    RECONCILE_PROVIDER: "provider_confirmed_stop",
}
# An allocation change seen before any instance ran and before any custody began (no input
# fetched, no output written) is benign: the assignment is re-bound and the guardian keeps
# waiting. The event kind carries the old and new allocation as JSON after the prefix.
PRESTART_REBIND_PREFIX = "prestart_reallocation:"
PRESTART_REBIND_REASON = "prestart_reallocation"
DEFAULT_MAX_PRESTART_REBINDS = 3
MAX_PRESTART_REBINDS_LIMIT = 10
_REBIND_ID_CAP = 96

# A provider read that fails while the route is pre-start and unallocated (nothing ran,
# no custody began, no billable instance placed: 0c) is retried across polls instead of
# stopping the run at the first failure. Each attempt is one event whose kind carries a
# closed JSON detail after this prefix; the window closes after the profile's deadline.
PRESTART_READ_RETRY_PREFIX = "prestart_read_retry:"
# Overall window, not a per-request timeout: each read has its own short transport deadline
# (salad_http.READ_DEADLINE_SECONDS) and idempotent reads are retried inside this window. Salad
# can answer slowly for minutes while a new group is placed; a $0 pre-start wait is cheap.
DEFAULT_PRESTART_READ_DEADLINE_SECONDS = 300.0
MAX_PRESTART_READ_DEADLINE_SECONDS = 600.0
# Seconds before the next poll after the n-th consecutive failed read (bounded backoff).
PRESTART_READ_BACKOFF_SECONDS = (5.0, 10.0, 15.0)
# After the run began (custody started, an allocation bound, or the instance seen running) a
# transient read failure is retried at the next poll too, but only briefly: at most
# POSTSTART_READ_RETRY_LIMIT consecutive failures (the last one stops) and only within
# POSTSTART_READ_WINDOW_SECONDS of the first. The window equals the safety service's
# UNSEEN_RUN_GAP_SECONDS (asserted there), the margin every admission already adds to billed
# time, so the spend caps are unchanged. The assignment's deadline, heartbeat and stop clocks
# are not extended: a retry is only taken while the guardian is not already stopping.
POSTSTART_READ_RETRY_PREFIX = "poststart_read_retry:"
POSTSTART_READ_RETRY_LIMIT = 3
POSTSTART_READ_WINDOW_SECONDS = 45.0
# Transport failure reasons that are deterministic or mean the group is gone: retrying
# cannot change them, so they stop at once instead of burning the deadline. Readers get
# the reason through an optional ``reason`` attribute on the raised error (absent: unknown,
# treated as transient), never by parsing text.
NON_RETRYABLE_READ_REASONS = frozenset(
    {"identity_mismatch", "group_not_found", "group_gone", "provider_identity_changed"}
)
# identity_mismatch on one of these field names is a known provider normalization and may
# be retried like any transient read. Empty: no normalization is accepted by the guardian;
# a mismatch on any other (or unnamed) field stops immediately. Fields come from the
# optional ``field`` attribute on the raised error.
RETRYABLE_MISMATCH_FIELDS: frozenset[str] = frozenset()
MAX_SHOWN_READ_RETRIES = 10
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _token(value):
    return value if type(value) is str and _TOKEN.fullmatch(value) else None


def read_failure_detail(error) -> tuple[str, str | None]:
    """Closed (reason, field) of a failed provider read, read defensively.

    ``reason`` and ``field`` are optional attributes of the raised error (the transport's
    closed failure reason and the NAME, never a value, of a mismatching field); without
    them the reason is the generic ``timeout`` or ``provider_unknown``.
    """
    if isinstance(error, TimeoutError):
        return "timeout", None
    return (
        _token(getattr(error, "reason", None)) or _token(str(error)) or "provider_unknown",
        _token(getattr(error, "field", None)),
    )


def is_deterministic_read_failure(error) -> bool:
    """True for a read failure that retrying cannot fix (see NON_RETRYABLE_READ_REASONS)."""
    reason, field = read_failure_detail(error)
    if reason == "identity_mismatch" and field in RETRYABLE_MISMATCH_FIELDS:
        return False
    return reason in NON_RETRYABLE_READ_REASONS


def is_transient_read_failure(error) -> bool:
    """A timeout or a provider_unknown read that is not deterministic."""
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, ValueError) and (
        str(error) == "provider_unknown" and not is_deterministic_read_failure(error)
    )


@dataclass(frozen=True)
class GuardianAssignment:
    binding: CustodyBinding
    organization: str
    provider_project: str
    group_id: str
    group_name: str
    allocation_id: str
    image_digest: str
    deadline: float
    heartbeat_seconds: float = 30
    poll_seconds: float = 5
    stop_retry_seconds: float = 30
    # Billed-time bound: Salad bills only while an instance runs. When set, the
    # run is also stopped at first-seen-running + running_seconds; ``deadline``
    # stays the outer (provisioning/route) bound and is the only bound while
    # the instance has never been seen running.
    running_seconds: float | None = None

    def effective_deadline(self, first_running):
        if self.running_seconds is None or first_running is None:
            return self.deadline
        return min(self.deadline, first_running + self.running_seconds)

    def serialized(self):
        self.binding.key()
        if any(
            not isinstance(v, str) or not v or len(v) > 256
            for v in (
                self.organization,
                self.provider_project,
                self.group_id,
                self.group_name,
                self.allocation_id,
                self.image_digest,
            )
        ):
            raise ValueError("invalid_identity")
        if any(
            not math.isfinite(v) or v <= 0
            for v in (
                self.deadline,
                self.heartbeat_seconds,
                self.poll_seconds,
                self.stop_retry_seconds,
            )
        ):
            raise ValueError("invalid_deadline")
        if self.running_seconds is not None and (
            type(self.running_seconds) not in {int, float}
            or not math.isfinite(self.running_seconds)
            or self.running_seconds <= 0
        ):
            raise ValueError("invalid_deadline")
        data = asdict(self)
        if self.running_seconds is None:
            del data["running_seconds"]  # keep pre-existing stored assignments stable
        return json.dumps(data, sort_keys=True)


@dataclass(frozen=True)
class ProviderObservation:
    organization: str
    project: str
    group_id: str
    group_name: str
    allocation_id: str
    image_digest: str
    state: str  # running, stopped, pending, unknown; absence is unknown
    observed_at: float
    prior_allocation_id: str | None = None  # independently retained native read, never caller input


class SaladGuardian:
    def __init__(
        self,
        path: Path,
        *,
        custody: SaladCustody,
        observe: Callable[[GuardianAssignment], ProviderObservation],
        stop: Callable[[GuardianAssignment], str],
        clock=time.time,
        custody_started: Callable[[str], bool] | None = None,
        max_prestart_rebinds: int = DEFAULT_MAX_PRESTART_REBINDS,
        prestart_read_deadline_seconds: float = DEFAULT_PRESTART_READ_DEADLINE_SECONDS,
    ):
        if (
            type(max_prestart_rebinds) is not int
            or not 0 <= max_prestart_rebinds <= MAX_PRESTART_REBINDS_LIMIT
        ):
            raise ValueError("invalid_max_prestart_rebinds")
        if (
            type(prestart_read_deadline_seconds) not in {int, float}
            or not math.isfinite(prestart_read_deadline_seconds)
            or not 0 <= prestart_read_deadline_seconds <= MAX_PRESTART_READ_DEADLINE_SECONDS
        ):
            raise ValueError("invalid_prestart_read_deadline")
        # 0 turns the cross-poll retry off: every failed read stops, as before.
        self.prestart_read_deadline_seconds = float(prestart_read_deadline_seconds)
        # ``custody_started(operation)`` is the provider adapter's answer to "has any custody
        # begun for this operation" (an input fetched, an output written, a run observed). With
        # no callable the guardian cannot know, so it never re-binds: every change stops.
        self.custody_started = custody_started
        self.max_prestart_rebinds = max_prestart_rebinds
        self.db = private_database(path)
        self._lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self.db.close()
            raise ValueError("guardian_already_owned") from None
        self.custody, self.observe, self.stop, self.clock = custody, observe, stop, clock
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS assignments (
                operation TEXT PRIMARY KEY, assignment TEXT, heartbeat REAL,
                last_poll REAL, next_stop REAL DEFAULT 0,
                stopping INTEGER DEFAULT 0, status TEXT DEFAULT 'armed',
                reason TEXT DEFAULT 'armed', orphan INTEGER DEFAULT 0,
                allocation_uncertain INTEGER DEFAULT 0, manifest TEXT);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, operation TEXT, at REAL, kind TEXT);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(assignments)")}
        if "assigned_allocation" not in columns:
            self.db.execute("ALTER TABLE assignments ADD COLUMN assigned_allocation TEXT")
        if "first_running" not in columns:
            self.db.execute("ALTER TABLE assignments ADD COLUMN first_running REAL")
        if "read_fail_since" not in columns:
            self.db.execute("ALTER TABLE assignments ADD COLUMN read_fail_since REAL")
        # Every process recovery is stop-only, even if a prior owner died before
        # recording an effect. Never renew running authority from saved state.
        with self.db:
            self.db.execute(
                "UPDATE assignments SET stopping=1,reason='guardian_restart' "
                "WHERE status NOT IN ('stopped','reconciled')"
            )

    def close(self):
        if self._lock_fd is not None:
            self.db.close()
            os.close(self._lock_fd)
            self._lock_fd = None

    def arm(self, assignment: GuardianAssignment, *, stop_only=False):
        encoded = assignment.serialized()
        now = self.clock()
        if now >= assignment.deadline and not stop_only:
            raise ValueError("expired_assignment")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO assignments(operation,assignment,heartbeat,last_poll) "
                "VALUES(?,?,?,?)",
                (assignment.binding.operation, encoded, now, now),
            )
            if (
                self.db.execute(
                    "SELECT assignment FROM assignments WHERE operation=?",
                    (assignment.binding.operation,),
                ).fetchone()[0]
                != encoded
            ):
                raise ValueError("assignment_mismatch")
            if stop_only:
                self.db.execute(
                    "UPDATE assignments SET stopping=1,reason='stop_only_recovery' "
                    "WHERE operation=? AND stopping=0 AND status!='stopped'",
                    (assignment.binding.operation,),
                )

    def heartbeat(self, operation: str):
        """Authenticated control composition only; cannot undo a stop decision."""
        with self.db:
            self.db.execute(
                "UPDATE assignments SET heartbeat=? "
                "WHERE operation=? AND stopping=0 AND status!='stopped'",
                (self.clock(), operation),
            )

    def cancel(self, operation: str):
        with self.db:
            self.db.execute(
                "UPDATE assignments SET stopping=1,reason='cancelled' "
                "WHERE operation=? AND stopping=0 AND status!='stopped'",
                (operation,),
            )

    def complete(self, operation: str):
        """Stop as soon as the single approved output is retained.

        A one-shot workload exits after upload; Salad then keeps the replica
        count by scheduling a replacement instance,
        which would be a second, unapproved allocation. Stopping on retention
        closes that window. Like cancel, this can only start a stop.
        """
        with self.db:
            self.db.execute(
                "UPDATE assignments SET stopping=1,reason='output_retained' "
                "WHERE operation=? AND stopping=0 AND status!='stopped'",
                (operation,),
            )

    def reconcile(self, operation: str, *, reason: str = RECONCILE_OPERATOR):
        """Close the orphan exposure of an uncertain operation the provider reports stopped.

        The guardian never self-confirms termination after an allocation change
        (a provider readback cannot prove node-local writer death), so it never
        seals custody here. The provider's own read IS authoritative for what it
        runs and bills: once the service has freshly observed the exact group
        stopped with zero instances, the spend exposure is closed. The service does
        that automatically (RECONCILE_PROVIDER) or an operator does it with an
        attestation (RECONCILE_OPERATOR). The result is "reconciled", never
        "stopped": allocation uncertainty stays recorded, custody is not sealed or
        released, and no acceptance follows from it.
        """
        if reason not in RECONCILE_REASONS:
            raise ValueError("reconcile_reason")
        with self.db:
            changed = self.db.execute(
                "UPDATE assignments SET status='reconciled',orphan=0,reason=? "
                "WHERE operation=? AND stopping=1 AND status NOT IN ('stopped','reconciled')",
                (reason, operation),
            ).rowcount
            if changed != 1:
                raise ValueError("reconcile_not_applicable")
            self.db.execute(
                "INSERT INTO events(operation,at,kind) VALUES(?,?,?)",
                (operation, self.clock(), RECONCILE_REASONS[reason]),
            )

    def status(self, operation: str) -> dict:
        row = self.db.execute(
            "SELECT status,reason,orphan,allocation_uncertain,manifest "
            "FROM assignments WHERE operation=?",
            (operation,),
        ).fetchone()
        if not row:
            raise ValueError("assignment_missing")
        cause = self.db.execute(
            "SELECT kind FROM events WHERE operation=? AND substr(kind,1,?)=? "
            "ORDER BY sequence LIMIT 1",
            (operation, len(STOP_CAUSE_PREFIX), STOP_CAUSE_PREFIX),
        ).fetchone()
        result = dict(
            status=row[0],
            reason=row[1],
            stop_cause=cause[0][len(STOP_CAUSE_PREFIX) :] if cause else None,
            orphan=bool(row[2]),
            allocation_uncertain=bool(row[3]),
            manifest=json.loads(row[4]) if row[4] else None,
            cost="unknown",
            pending_exposure=True,
            node_writers="unknown",
            quarantine=True,
        )
        rebinds = self._prestart_rebinds(operation)
        if rebinds:
            # Additive: absent unless an allocation was re-bound before anything started.
            result["prestart_rebinds"] = {
                "count": len(rebinds),
                "events": rebinds[-MAX_PRESTART_REBINDS_LIMIT:],
            }
        retries = self._prestart_read_retries(operation)
        if retries:
            # Additive: absent unless a pre-start read failure was retried.
            result["prestart_read_retries"] = retries
        post = self._read_retries(operation, POSTSTART_READ_RETRY_PREFIX)
        if post:
            # Additive: absent unless a read failure after the run began was retried.
            post["window_seconds"] = POSTSTART_READ_WINDOW_SECONDS
            post["limit"] = POSTSTART_READ_RETRY_LIMIT
            result["poststart_read_retries"] = post
        return result

    def read_retry_backoff(self, operation: str) -> float | None:
        """Seconds to wait before the next poll while a pre-start read failure is being
        retried (bounded, growing with each consecutive failure); None when none is."""
        row = self.db.execute(
            "SELECT read_fail_since,stopping FROM assignments WHERE operation=?", (operation,)
        ).fetchone()
        if not row or row[0] is None or row[1]:
            return None
        count = sum(
            self._retries_since(operation, prefix, row[0])
            for prefix in (PRESTART_READ_RETRY_PREFIX, POSTSTART_READ_RETRY_PREFIX)
        )
        if count <= 0:
            return None
        return PRESTART_READ_BACKOFF_SECONDS[min(count, len(PRESTART_READ_BACKOFF_SECONDS)) - 1]

    def _retries_since(self, operation: str, prefix: str, since: float | None) -> int:
        """Retry events with this prefix at or after ``since`` (None: all of them)."""
        return self.db.execute(
            "SELECT count(*) FROM events WHERE operation=? AND substr(kind,1,?)=? AND at>=?",
            (operation, len(prefix), prefix, -1 if since is None else since),
        ).fetchone()[0]

    def _prestart_read_retries(self, operation: str) -> dict | None:
        return self._read_retries(operation, PRESTART_READ_RETRY_PREFIX)

    def _read_retries(self, operation: str, prefix: str) -> dict | None:
        rows = self.db.execute(
            "SELECT at,kind FROM events WHERE operation=? AND substr(kind,1,?)=? ORDER BY sequence",
            (operation, len(prefix), prefix),
        ).fetchall()
        if not rows:
            return None
        attempts = []
        for at, kind in rows:
            try:
                detail = json.loads(kind[len(prefix) :])
            except ValueError:
                continue
            attempts.append(
                {
                    "at": at,
                    "reason": _token(detail.get("reason")) or "provider_unknown",
                    "field": _token(detail.get("field")),
                }
            )
        if not attempts:
            return None
        return {
            "count": len(attempts),
            "first_at": attempts[0]["at"],
            "last_at": attempts[-1]["at"],
            "last_reason": attempts[-1]["reason"],
            "last_field": attempts[-1]["field"],
            "deadline_seconds": self.prestart_read_deadline_seconds,
            "attempts": attempts[-MAX_SHOWN_READ_RETRIES:],
        }

    def _prestart_rebinds(self, operation: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT at,kind FROM events WHERE operation=? AND substr(kind,1,?)=? ORDER BY sequence",
            (operation, len(PRESTART_REBIND_PREFIX), PRESTART_REBIND_PREFIX),
        ).fetchall()
        found = []
        for at, kind in rows:
            try:
                detail = json.loads(kind[len(PRESTART_REBIND_PREFIX) :])
            except ValueError:
                continue
            found.append(
                {
                    "at": at,
                    "from": detail.get("from"),
                    "to": detail.get("to"),
                    "reason": PRESTART_REBIND_REASON,
                }
            )
        return found

    def _may_rebind(self, operation: str, first_running, stopping: bool) -> bool:
        """True when an allocation change is a benign pre-start re-allocation: nothing ran,
        no custody began, the guardian is not already stopping, and the bound is not spent."""
        if stopping or first_running is not None or self.custody_started is None:
            return False
        if self.max_prestart_rebinds <= 0:
            return False
        try:
            if self.custody_started(operation):
                return False
        except Exception:
            return False  # cannot tell: fail closed, stop as before
        return self.rebinds_left(operation) > 0

    def _may_retry_read(
        self, operation, error, first_running, allocation, stopping, fail_since, now
    ) -> str | None:
        """The event prefix to record when a failed provider read is retried at the next poll
        instead of stopping, or None to stop as before.

        Pre-start (unallocated, never seen running, adapter reports no custody begun): bounded
        by the profile's deadline from the first consecutive failure (no adapter or no answer:
        fail closed). After the run began (custody started, an allocation bound or the instance
        seen running): bounded by POSTSTART_READ_RETRY_LIMIT consecutive failures within
        POSTSTART_READ_WINDOW_SECONDS of the first. Only a transient read failure qualifies, and
        never while the guardian is already stopping, so the assignment's deadline, heartbeat and
        stop clocks are not extended and a deterministic failure (identity mismatch, group gone)
        stops at once. Charging is the safety service's own reservation (running window plus the
        unseen-run gap, which this window equals); a retry only delays the stop within it."""
        if stopping or not is_transient_read_failure(error):
            return None
        started = first_running is not None or allocation != "unallocated"
        if not started and self.custody_started is not None:
            try:
                started = bool(self.custody_started(operation))
            except Exception:
                return None  # cannot tell: fail closed, stop as before
        if started:
            if fail_since is not None and now - fail_since >= POSTSTART_READ_WINDOW_SECONDS:
                return None
            # Consecutive failures including this one; a first failure has no earlier retries.
            earlier = (
                0
                if fail_since is None
                else self._retries_since(operation, POSTSTART_READ_RETRY_PREFIX, fail_since)
            )
            failures = 1 + earlier
            return POSTSTART_READ_RETRY_PREFIX if failures < POSTSTART_READ_RETRY_LIMIT else None
        if self.custody_started is None or self.prestart_read_deadline_seconds <= 0:
            return None
        if fail_since is None or now - fail_since < self.prestart_read_deadline_seconds:
            return PRESTART_READ_RETRY_PREFIX
        return None

    def rebinds_left(self, operation: str) -> int:
        """How many more pre-start re-binds the bound allows for this operation."""
        used = self.db.execute(
            "SELECT count(*) FROM events WHERE operation=? AND substr(kind,1,?)=?",
            (operation, len(PRESTART_REBIND_PREFIX), PRESTART_REBIND_PREFIX),
        ).fetchone()[0]
        return max(0, self.max_prestart_rebinds - used)

    def _rebind(self, operation: str, old: str, new: str, now: float) -> None:
        """Re-bind to ``new`` without touching any clock, deadline or stop decision."""
        detail = json.dumps(
            {"from": old[:_REBIND_ID_CAP], "to": new[:_REBIND_ID_CAP]}, separators=(",", ":")
        )
        with self.db:
            self.db.execute(
                "UPDATE assignments SET assigned_allocation=? WHERE operation=?",
                (new, operation),
            )
            self.db.execute(
                "INSERT INTO events(operation,at,kind) VALUES(?,?,?)",
                (operation, now, PRESTART_REBIND_PREFIX + detail),
            )

    def tick(self, operation: str) -> dict:
        row = self.db.execute(
            "SELECT assignment,heartbeat,last_poll,next_stop,stopping,status,allocation_uncertain,"
            "assigned_allocation,first_running,read_fail_since "
            "FROM assignments WHERE operation=?",
            (operation,),
        ).fetchone()
        if not row:
            raise ValueError("assignment_missing")
        data = json.loads(row[0])
        data["binding"] = CustodyBinding(**data["binding"])
        a = GuardianAssignment(**data)
        now = self.clock()
        first_running = row[8]
        if row[5] in {"stopped", "reconciled"}:
            return self.status(operation)
        reason = None
        if now < row[2] or now < row[1]:
            reason = "clock_uncertain"
        elif now >= a.effective_deadline(first_running):
            reason = "deadline"
        elif now - row[1] >= a.heartbeat_seconds:
            reason = "control_disconnect"
        elif now - row[2] > a.poll_seconds * 2:
            reason = "missed_poll"
        stopping = bool(row[4]) or reason is not None
        exact = False
        terminal = False
        uncertain = bool(row[6])
        allocation = row[7] or a.allocation_id
        stop_safe = True
        fail_since = row[9]
        retry_detail = None
        retry_prefix = None
        try:
            o = self.observe(a)
            fail_since = None  # a read answered: the failure window is over
            # Real callbacks finish after poll entry. Compare their receipt to
            # the completion clock, not the earlier frozen poll-start time.
            now = self.clock()
            if (
                first_running is None
                and isinstance(o, ProviderObservation)
                and o.state == "running"
            ):
                first_running = now
                with self.db:
                    self.db.execute(
                        "UPDATE assignments SET first_running=? "
                        "WHERE operation=? AND first_running IS NULL",
                        (now, operation),
                    )
            if now >= a.effective_deadline(first_running):
                reason, stopping = "deadline", True
            elif now - row[1] >= a.heartbeat_seconds:
                reason, stopping = "control_disconnect", True
            exact = isinstance(o, ProviderObservation) and (
                o.organization,
                o.project,
                o.group_id,
                o.group_name,
                o.image_digest,
            ) == (a.organization, a.provider_project, a.group_id, a.group_name, a.image_digest)
            fresh = (
                isinstance(o, ProviderObservation) and 0 <= now - o.observed_at <= a.poll_seconds
            )
            if isinstance(o, ProviderObservation) and not exact:
                stop_safe = False
            if (
                exact
                and fresh
                and o.prior_allocation_id is not None
                and o.allocation_id != o.prior_allocation_id
            ):
                if self._may_rebind(operation, first_running, stopping):
                    self._rebind(operation, allocation, o.allocation_id, now)
                    terminal = o.state == "stopped" and not uncertain
                else:
                    reason, stopping, uncertain = "allocation_changed", True, True
            elif not exact or not fresh or o.state not in {"running", "stopped", "pending"}:
                reason, stopping = "provider_unknown", True
            elif allocation == "unallocated" and o.allocation_id != "unallocated":
                with self.db:
                    self.db.execute(
                        "UPDATE assignments SET assigned_allocation=? WHERE operation=?",
                        (o.allocation_id, operation),
                    )
                terminal = o.state == "stopped" and not uncertain
            elif o.allocation_id != allocation:
                if self._may_rebind(operation, first_running, stopping):
                    self._rebind(operation, allocation, o.allocation_id, now)
                    terminal = o.state == "stopped" and not uncertain
                else:
                    reason, stopping, uncertain = "allocation_changed", True, True
            else:
                terminal = o.state == "stopped" and not uncertain
        except Exception as error:
            failed_at = self.clock()
            retry_prefix = self._may_retry_read(
                operation, error, first_running, allocation, stopping, fail_since, failed_at
            )
            if retry_prefix is not None:
                # The window opens at this poll's start, the same instant its retry event is
                # stamped with, so every retry of the window counts from fail_since.
                fail_since = now if fail_since is None else fail_since
                retry_detail = read_failure_detail(error)
            else:
                reason, stopping = "provider_unavailable", True
        with self.db:
            self.db.execute(
                "UPDATE assignments SET last_poll=?,stopping=?,allocation_uncertain=?,"
                "read_fail_since=? WHERE operation=?",
                (now, int(stopping), int(uncertain), fail_since, operation),
            )
            if retry_detail is not None:
                self.db.execute(
                    "INSERT INTO events(operation,at,kind) VALUES(?,?,?)",
                    (
                        operation,
                        now,
                        retry_prefix
                        + json.dumps(
                            {"reason": retry_detail[0], "field": retry_detail[1]},
                            separators=(",", ":"),
                        ),
                    ),
                )
            if stopping and not row[4] and reason:
                # Durable cause of this guardian's own stop decision: the reason column
                # is overwritten later (e.g. exact_terminal_and_sealed), this is not.
                self.db.execute(
                    "INSERT INTO events(operation,at,kind) VALUES(?,?,?)",
                    (operation, now, STOP_CAUSE_PREFIX + reason),
                )
            if reason:
                self.db.execute(
                    "UPDATE assignments SET reason=? WHERE operation=?", (reason, operation)
                )
        if terminal:
            manifest = self.custody.seal_operation(a.binding)
            with self.db:
                self.db.execute(
                    "INSERT INTO events(operation,at,kind) VALUES(?,?,'exact_terminal')",
                    (operation, now),
                )
                self.db.execute(
                    "UPDATE assignments SET status='stopped',reason='exact_terminal_and_sealed',"
                    "stopping=1,orphan=0,manifest=? WHERE operation=?",
                    (json.dumps(manifest, sort_keys=True), operation),
                )
            return self.status(operation)
        if stopping:
            # Close external writers immediately, including outage/identity loss.
            # Custody failure must not suppress independently authorized stop.
            try:
                self.custody.close_writers(a.binding)
            except Exception:
                with self.db:
                    self.db.execute(
                        "UPDATE assignments SET reason='custody_unknown' WHERE operation=?",
                        (operation,),
                    )
            if now >= row[3] and stop_safe:
                # Persist stop intent and next allowed retry before network. Only
                # this exact immutable assignment may be retried; never create/start.
                with self.db:
                    self.db.execute(
                        "UPDATE assignments SET next_stop=?,status='unknown',orphan=1 "
                        "WHERE operation=?",
                        (now + a.stop_retry_seconds, operation),
                    )
                    self.db.execute(
                        "INSERT INTO events(operation,at,kind) VALUES(?,?,'stop_intent')",
                        (operation, now),
                    )
                try:
                    result = self.stop(a)
                    kind = "stop_pending" if result == "pending" else "stop_unknown"
                except Exception:
                    kind = "stop_unknown"
                with self.db:
                    self.db.execute(
                        "INSERT INTO events(operation,at,kind) VALUES(?,?,?)",
                        (operation, now, kind),
                    )
            with self.db:
                self.db.execute(
                    "UPDATE assignments SET status='unknown',orphan=1 WHERE operation=?",
                    (operation,),
                )
        return self.status(operation)

    def run(self, *, shutdown: Callable[[], bool], sleep=time.sleep):
        """Separate-service entry seam; shutdown never claims workloads stopped."""
        while not shutdown():
            for (operation,) in self.db.execute(
                "SELECT operation FROM assignments WHERE status!='stopped'"
            ).fetchall():
                self.tick(operation)
            sleep(1)
