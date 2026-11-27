"""Stop a Salad group whose container process exited non-zero, instead of letting it crash-loop.

A one-shot container that exits with a failing code is restarted by Salad, then moved to a second
machine minutes later; each replacement is billed and the real cause is lost behind an
``allocation_changed`` stop. The system-events read (the one trusted, org-scoped read the service
already makes after a stop) names ``Instance Exited: <code>``. This module watches for that, in a
bounded way, while the frame is still armed, and the service then puts the frame into stop-only
recovery with the closed cause ``container_exited_nonzero`` (the ordinary exact-stop path: the
provider's own read still has to confirm the stop; nothing is inferred).

Only closed tokens are kept: exit codes (integers 0-255) and event names. The container's own
stderr is not in that read (system events carry no process output), so the receipt says so with a
closed token rather than a tail.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from .salad_custody import CustodyBinding
from .salad_events import closed_record, empty_record

NONZERO_EXIT_CAUSE = "container_exited_nonzero"
FAILURE_CLASS = "salad_container_exited_nonzero"
# 137 (SIGKILL, the out-of-memory / platform kill) and 143 (SIGTERM, a platform stop) keep today's
# handling: they are not an entry failure of the workload.
PLATFORM_EXIT_CODES = frozenset({137, 143})
# Bounded: one events read per interval, at most this many per frame (about ten minutes), so a
# healthy long run costs a handful of log queries and a crash loop is seen before the replacement.
WATCH_INTERVAL_SECONDS = 30
WATCH_MAX_READS = 20
# Receipt token: the events read carries no process output and no other log read is trusted.
STDERR_UNAVAILABLE = {"outcome": "unavailable", "reason": "no_trusted_log_source"}


def nonzero_exit_codes(record: Any) -> list[int]:
    """The non-zero, non-platform exit codes of a closed start-events record that names an exit."""
    closed = closed_record(record)
    if closed is None or closed["outcome"] != "read" or "instance_exited" not in closed["events"]:
        return []
    codes = closed["exit_codes"]
    if any(code in PLATFORM_EXIT_CODES for code in codes):
        return []  # a kill or platform stop is in the record: not an entry failure
    return [code for code in codes if code != 0]


def container_exit_receipt(record: Any) -> dict[str, Any] | None:
    """Receipt evidence for a recorded non-zero exit, or None (every other receipt is unchanged)."""
    codes = nonzero_exit_codes(record)
    if not codes:
        return None
    return {"exit_codes": codes, "stderr_tail": dict(STDERR_UNAVAILABLE)}


def failure_detail(record: Any) -> str | None:
    codes = nonzero_exit_codes(record)
    if not codes:
        return None
    return (
        f"{FAILURE_CLASS}: the container process exited with code {codes[0]}; the group was "
        "stopped instead of letting the provider restart it (stderr unavailable)"
    )[:500]


class ContainerExitWatch:
    """Mixin for ``SaladSafetyService``: a throttled, off-thread events read while armed."""

    def _init_exit_watch(self):
        self._exit_watch = {}

    def _watch_container_exit(self, reservation):
        """Nothing here raises. Loop thread only (the read itself runs on a daemon thread)."""
        try:
            state = self._exit_watch.setdefault(
                reservation, dict(reads=0, next=0.0, thread=None, result=None, stopped=False)
            )
            if state["stopped"]:
                return
            if state["result"] is not None:
                result, state["result"] = state["result"], None
                self._act_on_exit_read(reservation, state, result)
                return
            thread = state["thread"]
            if thread is not None and thread.is_alive():
                return
            now = self.clock()
            row = self._row(reservation)
            if (
                now < state["next"]
                or state["reads"] >= WATCH_MAX_READS
                or not row["provider_id"]
                or row["recovery"]
                or row["output"]
                or not row["seen_running"]
            ):
                return
            guardian = self.guardian.status(row["operation"])
            if guardian["status"] != "armed" or guardian["reason"] != "armed":
                return  # already stopping (any cause): the one post-stop read covers it
            state["reads"] += 1
            state["next"] = now + WATCH_INTERVAL_SECONDS
            operation, provider_id = row["operation"], row["provider_id"]

            def read():
                try:
                    observation = self._bounded_send(row, "read_events", provider_id).observation
                    record = dict(
                        outcome="empty" if observation.get("empty") else "read",
                        events=observation["events"],
                        exit_codes=observation["exit_codes"],
                        machines=observation["machines"],
                    )
                except Exception:
                    record = empty_record("unavailable")
                state["result"] = (operation, record)

            thread = threading.Thread(target=read, name="exit-watch-read", daemon=True)
            state["thread"] = thread
            thread.start()
        except Exception:
            self.diagnostic_write_failures += 1

    def _act_on_exit_read(self, reservation, state, result):
        operation, record = result
        if not nonzero_exit_codes(record):
            return
        row = self._row(reservation)
        if row["recovery"] or row["output"]:
            return
        # Keep the evidence first (the later post-stop read is then a no-op), then fence.
        self.guardian.record_start_events(operation, record)
        with self.db:
            self._first_recovery_cause(reservation, NONZERO_EXIT_CAUSE)
            self.db.execute(
                "UPDATE reservations SET recovery=1 WHERE reservation=?", (reservation,)
            )
            self._frame_note(
                reservation,
                "container_exited_nonzero",
                {"exit_codes": nonzero_exit_codes(record)},
                once=True,
            )
        state["stopped"] = True
        if self.inputs:
            self.inputs.drop(operation)
        try:
            self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
        finally:
            self.guardian.arm(self._assignment(self._row(reservation)), stop_only=True)
            self._cancel(operation)
