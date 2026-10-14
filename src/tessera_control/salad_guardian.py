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
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .salad_custody import CustodyBinding, SaladCustody, private_database


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
        return json.dumps(asdict(self), sort_keys=True)


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
    ):
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
        # Every process recovery is stop-only, even if a prior owner died before
        # recording an effect. Never renew running authority from saved state.
        with self.db:
            self.db.execute(
                "UPDATE assignments SET stopping=1,reason='guardian_restart' "
                "WHERE status!='stopped'"
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

    def status(self, operation: str) -> dict:
        row = self.db.execute(
            "SELECT status,reason,orphan,allocation_uncertain,manifest "
            "FROM assignments WHERE operation=?",
            (operation,),
        ).fetchone()
        if not row:
            raise ValueError("assignment_missing")
        return dict(
            status=row[0],
            reason=row[1],
            orphan=bool(row[2]),
            allocation_uncertain=bool(row[3]),
            manifest=json.loads(row[4]) if row[4] else None,
            cost="unknown",
            pending_exposure=True,
            node_writers="unknown",
            quarantine=True,
        )

    def tick(self, operation: str) -> dict:
        row = self.db.execute(
            "SELECT assignment,heartbeat,last_poll,next_stop,stopping,status,allocation_uncertain,"
            "assigned_allocation "
            "FROM assignments WHERE operation=?",
            (operation,),
        ).fetchone()
        if not row:
            raise ValueError("assignment_missing")
        data = json.loads(row[0])
        data["binding"] = CustodyBinding(**data["binding"])
        a = GuardianAssignment(**data)
        now = self.clock()
        if row[5] == "stopped":
            return self.status(operation)
        reason = None
        if now < row[2] or now < row[1]:
            reason = "clock_uncertain"
        elif now >= a.deadline:
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
        try:
            o = self.observe(a)
            # Real callbacks finish after poll entry. Compare their receipt to
            # the completion clock, not the earlier frozen poll-start time.
            now = self.clock()
            if now >= a.deadline:
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
                reason, stopping, uncertain = "allocation_changed", True, True
            else:
                terminal = o.state == "stopped" and not uncertain
        except Exception:
            reason, stopping = "provider_unavailable", True
        with self.db:
            self.db.execute(
                "UPDATE assignments SET last_poll=?,stopping=?,allocation_uncertain=? "
                "WHERE operation=?",
                (now, int(stopping), int(uncertain), operation),
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
