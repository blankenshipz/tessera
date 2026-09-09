"""Durable, fail-closed driver for the local CPU execution adapter.

This journal establishes local recovery facts only.  It never grants authority,
and it never treats a container exit as a scientific result.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from tessera_worker.cpu import CpuError, CpuObservation, CpuOperation

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_PHASES = frozenset({"prepared", "start_intent", "running", "terminal", "uncertain"})

Phase = Literal["prepared", "start_intent", "running", "terminal", "uncertain", "denied"]
ValidateLease = Callable[["Lease"], bool]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
Sleep = Callable[[float], None]


class SupervisorError(Exception):
    """The local supervisor cannot safely advance an operation."""


class JournalLockedError(SupervisorError):
    """Another supervisor process owns this private journal."""


@dataclass(frozen=True)
class Lease:
    """Authority facts supplied by the control plane, not authority by itself."""

    id: str
    operation_id: str
    plan_digest: str
    epoch: int
    expires_at: datetime
    task_revision: int
    project_id: str

    def __post_init__(self) -> None:
        if not self.id or not self.operation_id or not self.project_id:
            raise ValueError("Lease identity, operation, and project are required.")
        if not _DIGEST.fullmatch(self.plan_digest):
            raise ValueError("Lease plan digest is required.")
        if self.epoch < 1 or self.task_revision < 1:
            raise ValueError("Lease epoch and task revision must be positive.")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("Lease expiry must be UTC.")


@dataclass(frozen=True)
class DriverResult:
    """A durable driver observation; ``terminal`` is operational only."""

    phase: Phase
    observation: CpuObservation
    reason: str | None = None


class CpuSupervisor:
    """Own one private journal and drive a single stable CPU operation at a time.

    ``validate_lease`` must query trusted control-plane state.  Its failure is a
    denial, never a reason to retry a create or start operation.
    """

    def __init__(
        self,
        journal_dir: Path,
        adapter: object,
        validate_lease: ValidateLease,
        *,
        before_start: Callable[[CpuOperation, Lease], None] | None = None,
        before_reconcile: Callable[[CpuOperation], None] | None = None,
        before_cancel: Callable[[CpuOperation], None] | None = None,
        now: Clock | None = None,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise SupervisorError("The supervisor requires POSIX no-follow file locking.")
        self._journal_dir = Path(journal_dir)
        self._journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_fd = self._acquire_lock(self._journal_dir / "supervisor.lock")
        self._database = self._journal_dir / "supervisor.sqlite3"
        self._adapter = adapter
        self._validate_lease = validate_lease
        self._before_start = before_start
        self._before_reconcile = before_reconcile
        self._before_cancel = before_cancel
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        # This process-local anchor prevents a backwards wall clock from extending
        # a started operation.  The persisted wall-clock intent remains the
        # recovery bound after a supervisor restart.
        self._duration_monotonic_started: dict[str, float] = {}
        self._recovered_operations: set[str] = set()
        self._guard = threading.RLock()
        self._closed = False
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Release the process lock.  The journal remains for recovery."""
        with self._guard:
            if self._closed:
                return
            self._closed = True
            if self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self) -> CpuSupervisor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def drive_once(self, operation: CpuOperation, lease: Lease) -> DriverResult:
        """Perform at most one create and one start, after fresh authorization."""
        with self._guard:
            self._require_open()
            row = self._bind(operation, lease)
            if row["phase"] == "terminal":
                return self._recover_terminal(operation, row)
            stopped = self._enforce_bounds_and_authority(operation, lease, row)
            if stopped is not None:
                return stopped
            if row["phase"] in {"start_intent", "uncertain", "running"}:
                return self._reconcile_observation(operation)

            try:
                observation = self._adapter.prepare(operation)
            except CpuError:
                return self._uncertain(operation, "prepare_unconfirmed")
            if observation.state == "exited":
                return self._terminal(operation, observation, "observed_exited")
            if observation.state != "created":
                return self._uncertain(operation, f"prepare_{observation.state}", observation)

            # Recheck immediately before the side effect, after preparation.
            row = self._lookup_required(operation)
            stopped = self._enforce_bounds_and_authority(operation, lease, row)
            if stopped is not None:
                return stopped
            if not self._start_intent(operation):
                return self._reconcile_observation(operation)
            try:
                if self._before_start is not None:
                    self._before_start(operation, lease)
                observation = self._adapter.start(operation)
            except (CpuError, SupervisorError):
                return self._uncertain(operation, "start_unconfirmed")
            return self._after_start(operation, observation)

    def reconcile(self, operation: CpuOperation, lease: Lease) -> DriverResult:
        """Observe a prior intent without ever issuing another start."""
        with self._guard:
            self._require_open()
            row = self._bind(operation, lease)
            if row["phase"] == "terminal":
                return self._recover_terminal(operation, row)
            stopped = self._enforce_bounds_and_authority(operation, lease, row)
            if stopped is not None:
                return stopped
            return self._reconcile_observation(operation)

    def _reconcile_observation(self, operation: CpuOperation) -> DriverResult:
        try:
            if self._before_reconcile is not None:
                self._before_reconcile(operation)
            observation = self._adapter.observe(operation)
        except (CpuError, SupervisorError):
            return self._uncertain(operation, "observe_unconfirmed")
        if observation.state == "exited":
            return self._terminal(operation, observation, "observed_exited")
        if observation.state == "running":
            self._set_phase(operation.operation_id, "running", "observed_running")
            return DriverResult("running", observation)
        # Created after an intent may have started and stopped between observations.
        # It is deliberately a hold, rather than an invitation to retry start.
        return self._uncertain(operation, f"reconcile_{observation.state}", observation)

    def _recover_terminal(self, operation: CpuOperation, row: sqlite3.Row) -> DriverResult:
        """Read terminal provider state after restart; never issue another start."""
        if operation.operation_id not in self._recovered_operations:
            return self._result(row)
        try:
            observation = self._adapter.observe(operation)
        except CpuError:
            return DriverResult(
                "uncertain", CpuObservation("unknown"), "terminal_observation_unconfirmed"
            )
        if observation.state == "exited" and observation.termination_confirmed:
            return DriverResult("terminal", observation, row["terminal_reason"])
        # Preserve the durable terminal journal but expose an absent or
        # contradictory fresh provider read as unresolved.
        return DriverResult("uncertain", observation, "terminal_observation_unresolved")

    def tick(self, operation: CpuOperation, lease: Lease) -> DriverResult:
        """Recheck live authority and bounded duration for an existing operation."""
        with self._guard:
            self._require_open()
            row = self._bind(operation, lease)
            if row["phase"] == "terminal":
                return self._recover_terminal(operation, row)
            stopped = self._enforce_bounds_and_authority(operation, lease, row)
            if stopped is not None:
                return stopped
            return self._reconcile_observation(operation)

    def cancel(self, operation: CpuOperation, *, reason: str = "requested") -> DriverResult:
        """Request termination; call it terminal only after provider confirmation."""
        with self._guard:
            self._require_open()
            return self._cancel(operation, reason)

    def run_until_terminal(
        self,
        operation: CpuOperation,
        lease: Lease,
        *,
        poll_interval_seconds: float,
        cancellation_grace_seconds: float,
        max_ticks: int,
        sleep: Sleep = time.sleep,
    ) -> DriverResult:
        """Bounded foreground watchdog; dispatch remains an explicit caller choice.

        ``max_ticks`` bounds this helper even if a faulty clock or sleep callback
        does not advance.  Each tick independently checks control authority,
        lease expiry, and the durable operation deadline.
        """
        if poll_interval_seconds <= 0 or cancellation_grace_seconds < 0 or max_ticks < 1:
            raise ValueError("Watchdog intervals and tick bound must be positive.")
        with self._guard:
            self._require_open()
        result = self.drive_once(operation, lease)
        cancelling_since: float | None = None
        for _ in range(max_ticks):
            if result.phase in {"terminal", "denied"}:
                return result
            if result.reason is not None and result.reason.startswith("cancel_"):
                cancelling_since = (
                    cancelling_since if cancelling_since is not None else self._monotonic()
                )
                if self._monotonic() >= cancelling_since + cancellation_grace_seconds:
                    return DriverResult(
                        "uncertain", result.observation, "cancellation_grace_elapsed"
                    )
            sleep(poll_interval_seconds)
            result = self.tick(operation, lease)
        if result.phase == "terminal":
            return result
        # A bounded watchdog may not abandon a still-running operation merely
        # because its own poll budget is exhausted.  Cancellation is a request;
        # only a confirmed provider stop is terminal.
        return self.cancel(operation, reason="watchdog_tick_limit")

    def _cancel(self, operation: CpuOperation, reason: str) -> DriverResult:
        row = self._lookup(operation.operation_id)
        if row is None:
            raise SupervisorError("Operation is not present in this journal.")
        self._verify_operation(row, operation)
        try:
            if self._before_cancel is not None:
                self._before_cancel(operation)
            observation = self._adapter.cancel(operation)
        except (CpuError, SupervisorError):
            return self._uncertain(operation, f"cancel_{reason}_unconfirmed")
        if observation.termination_confirmed:
            return self._terminal(operation, observation, f"cancelled_{reason}")
        return self._uncertain(operation, f"cancel_{reason}_unconfirmed", observation)

    def _after_start(self, operation: CpuOperation, observation: CpuObservation) -> DriverResult:
        if observation.state == "running":
            self._set_phase(operation.operation_id, "running", "started")
            return DriverResult("running", observation)
        if observation.state == "exited":
            return self._terminal(operation, observation, "observed_exited")
        return self._uncertain(operation, f"start_{observation.state}", observation)

    def _bind(self, operation: CpuOperation, lease: Lease) -> sqlite3.Row:
        if (
            operation.operation_id != lease.operation_id
            or operation.plan_digest != lease.plan_digest
        ):
            raise SupervisorError("Operation does not match the supplied lease.")
        payload = self._payload(operation)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        current = self._lookup(operation.operation_id)
        if current is None:
            self._write(
                "INSERT INTO operations "
                "(operation_id, payload_digest, plan_digest, lease_id, epoch, expires_at, "
                "task_revision, project_id, phase, started_at, terminal_reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, ?)",
                (
                    operation.operation_id,
                    digest,
                    operation.plan_digest,
                    lease.id,
                    lease.epoch,
                    self._stamp(lease.expires_at),
                    lease.task_revision,
                    lease.project_id,
                    self._stamp(self._now()),
                ),
                operation.operation_id,
                "journal_prepared",
                {"payload_digest": digest, "epoch": lease.epoch},
            )
            current = self._lookup(operation.operation_id)
            assert current is not None
            return current
        self._verify_operation(current, operation)
        expected = (
            digest,
            operation.plan_digest,
            lease.id,
            lease.epoch,
            lease.task_revision,
            lease.project_id,
        )
        actual = (
            current["payload_digest"],
            current["plan_digest"],
            current["lease_id"],
            current["epoch"],
            current["task_revision"],
            current["project_id"],
        )
        if actual != expected:
            raise SupervisorError("Operation journal identity or authority epoch does not match.")
        return current

    def _enforce_bounds_and_authority(
        self, operation: CpuOperation, lease: Lease, row: sqlite3.Row
    ) -> DriverResult | None:
        possible_execution = row["phase"] in {"start_intent", "running", "uncertain"}
        if possible_execution and self._recovered_clock_rollback(row):
            return self._cancel(operation, "clock_rollback")
        if self._expired(lease):
            if possible_execution:
                return self._cancel(operation, "lease_expired")
            return DriverResult("denied", CpuObservation("unknown"), "lease_expired")
        if self._duration_elapsed(row, operation):
            if possible_execution:
                return self._cancel(operation, "duration_elapsed")
            return DriverResult("denied", CpuObservation("unknown"), "duration_elapsed")
        if not self._authorized(lease):
            if possible_execution:
                return self._cancel(operation, "lease_denied")
            return DriverResult("denied", CpuObservation("unknown"), "lease_denied")
        return None

    def _lookup_required(self, operation: CpuOperation) -> sqlite3.Row:
        row = self._lookup(operation.operation_id)
        if row is None:
            raise SupervisorError("Operation disappeared from this journal.")
        self._verify_operation(row, operation)
        return row

    def _verify_operation(self, row: sqlite3.Row, operation: CpuOperation) -> None:
        digest = hashlib.sha256(self._payload(operation).encode()).hexdigest()
        if (
            row["operation_id"] != operation.operation_id
            or row["payload_digest"] != digest
            or row["plan_digest"] != operation.plan_digest
        ):
            raise SupervisorError("Operation does not match its journal payload digest.")

    def _require_open(self) -> None:
        if self._closed or self._lock_fd is None:
            raise SupervisorError("Supervisor is closed.")

    def _start_intent(self, operation: CpuOperation) -> bool:
        # This conservative persisted timestamp starts the duration clock before
        # Docker receives start, including recovery after a lost reply.
        now = self._stamp(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE operations SET phase = 'start_intent', started_at = ?, updated_at = ? "
                "WHERE operation_id = ? AND phase = 'prepared' AND started_at IS NULL",
                (now, now, operation.operation_id),
            ).rowcount
            if updated:
                connection.execute(
                    "INSERT INTO events (operation_id, recorded_at, event, detail) "
                    "VALUES (?, ?, ?, ?)",
                    (operation.operation_id, now, "start_intent", "{}"),
                )
            connection.commit()
        if updated:
            self._duration_monotonic_started[operation.operation_id] = self._monotonic()
        return updated == 1

    def _terminal(
        self, operation: CpuOperation, observation: CpuObservation, reason: str
    ) -> DriverResult:
        self._set_phase(operation.operation_id, "terminal", reason)
        return DriverResult("terminal", observation, reason)

    def _uncertain(
        self, operation: CpuOperation, reason: str, observation: CpuObservation | None = None
    ) -> DriverResult:
        observation = observation or CpuObservation("unknown")
        self._set_phase(operation.operation_id, "uncertain", reason)
        return DriverResult("uncertain", observation, reason)

    def _set_phase(self, operation_id: str, phase: str, event: str) -> None:
        if phase not in _PHASES:
            raise AssertionError("invalid journal phase")
        now = self._high_water_updated_at(operation_id)
        terminal_reason = event if phase == "terminal" else None
        self._write(
            "UPDATE operations SET phase = ?, terminal_reason = ?, updated_at = ? "
            "WHERE operation_id = ?",
            (phase, terminal_reason, now, operation_id),
            operation_id,
            event,
            {"phase": phase},
        )

    def _high_water_updated_at(self, operation_id: str) -> str:
        """Do not let a backwards recovered wall clock erase a durable fence."""
        current = self._now()
        row = self._lookup(operation_id)
        if row is None:
            return self._stamp(current)
        try:
            durable = datetime.fromisoformat(row["updated_at"])
        except ValueError:
            # Keep malformed durable data visible to the recovery rollback fence.
            return row["updated_at"]
        if durable.tzinfo is None or durable > current:
            return row["updated_at"]
        return self._stamp(current)

    def _authorized(self, lease: Lease) -> bool:
        try:
            return self._validate_lease(lease) is True
        except Exception:
            return False

    def _expired(self, lease: Lease) -> bool:
        return self._now() >= lease.expires_at

    def _duration_elapsed(self, row: sqlite3.Row, operation: CpuOperation) -> bool:
        if row["started_at"] is None:
            return False
        started = datetime.fromisoformat(row["started_at"])
        if self._now() >= started + timedelta(seconds=operation.max_duration_seconds):
            return True
        monotonic_started = self._duration_monotonic_started.get(operation.operation_id)
        if monotonic_started is None:
            # A recovered supervisor cannot reconstruct monotonic time.  Retain
            # the durable start intent and account for elapsed wall time seen at
            # attachment; never rewrite the persisted deadline from a renewal.
            elapsed_wall = max(0.0, (self._now() - started).total_seconds())
            monotonic_started = self._monotonic() - elapsed_wall
            self._duration_monotonic_started[operation.operation_id] = monotonic_started
        return self._monotonic() - monotonic_started >= operation.max_duration_seconds

    def _recovered_clock_rollback(self, row: sqlite3.Row) -> bool:
        """Fence executable recovered rows when durable time is in the future."""
        if row["operation_id"] not in self._recovered_operations:
            return False
        now = self._now()
        for column in ("started_at", "updated_at"):
            value = row[column]
            if value is None:
                continue
            try:
                timestamp = datetime.fromisoformat(value)
            except ValueError:
                return True
            if timestamp.tzinfo is None or now < timestamp:
                return True
        return False

    @staticmethod
    def _payload(operation: CpuOperation) -> str:
        payload = {
            "argv": list(operation.argv),
            "image": operation.image,
            "max_duration_seconds": operation.max_duration_seconds,
            "memory_bytes": operation.memory_bytes,
            "operation_id": operation.operation_id,
            "plan_digest": operation.plan_digest,
            "scratch_bytes": operation.scratch_bytes,
        }
        if operation.input_directory is not None:
            payload["input_directory"] = operation.input_directory
            payload["input_manifest_json"] = operation.input_manifest_json
        if operation.development_output is not None:
            # The mount identity is part of the durable operation fence.  A
            # recovered operation cannot be redirected to another workspace.
            payload["development_output"] = {
                "directory": operation.development_output.directory,
                "profile": operation.development_output.profile,
                "fixture_marker": operation.development_output.fixture_marker,
            }
        return json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _stamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise SupervisorError("Supervisor clock must return UTC timestamps.")
        return value.isoformat()

    @staticmethod
    def _acquire_lock(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SupervisorError("Journal lock is not a regular file.")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except SupervisorError:
            if fd is not None:
                os.close(fd)
            raise
        except BlockingIOError as error:
            if fd is not None:
                os.close(fd)
            raise JournalLockedError("Another process owns this supervisor journal.") from error
        except OSError as error:
            if fd is not None:
                os.close(fd)
            raise SupervisorError("Cannot acquire a no-follow supervisor journal lock.") from error

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    task_revision INTEGER NOT NULL,
                    project_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    started_at TEXT,
                    terminal_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                );
                """
            )
            self._recovered_operations = {
                row[0] for row in connection.execute("SELECT operation_id FROM operations")
            }
        os.chmod(self._database, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database)
        connection.row_factory = sqlite3.Row
        return connection

    def _lookup(self, operation_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()

    def _write(
        self,
        statement: str,
        parameters: tuple[object, ...],
        operation_id: str,
        event: str,
        detail: dict[str, object],
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(statement, parameters)
            connection.execute(
                "INSERT INTO events (operation_id, recorded_at, event, detail) VALUES (?, ?, ?, ?)",
                (operation_id, self._stamp(self._now()), event, json.dumps(detail, sort_keys=True)),
            )
            connection.commit()

    @staticmethod
    def _result(row: sqlite3.Row) -> DriverResult:
        # The journal phase is durable; it is not a substitute for a fresh
        # provider observation.  Do not manufacture an exited container here.
        return DriverResult("terminal", CpuObservation("unknown"), row["terminal_reason"])
