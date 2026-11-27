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
from datetime import UTC, datetime
from typing import Any

from .salad_custody import CustodyBinding
from .salad_events import closed_record, empty_record

NONZERO_EXIT_CAUSE = "container_exited_nonzero"
FAILURE_CLASS = "salad_container_exited_nonzero"
# 137 (SIGKILL, the out-of-memory / platform kill) and 143 (SIGTERM, a platform stop) keep today's
# handling when they come first: they are not an entry failure of the workload. After an earlier
# non-zero exit they are the stop that ended the crash loop.
PLATFORM_EXIT_CODES = frozenset({137, 143})
# Bounded and backing off: reads start 30 s apart and the gap doubles every 4 reads (30, 60,
# 120 s), at most 12 per frame (about 14 minutes), so a healthy long run costs a dozen log
# queries and a crash loop (Salad replaces the machine after minutes) is seen early. A failed
# read doubles the next gap again (capped at 240 s).
WATCH_INTERVAL_SECONDS = 30
WATCH_MAX_INTERVAL_SECONDS = 240
WATCH_MAX_READS = 12
# Every frame's watch reads together, per UTC day: Salad's log query limit is daily (a 429 lasts
# until 00:00 UTC), and the one post-stop read that tells a failed start from a pre-custody
# reallocation (ADR 0196) must still find quota. Past this budget the watch reads nothing more
# that day; a rate-limited answer ends every watch read until the next UTC midnight.
WATCH_DAILY_READ_BUDGET = 240
# Receipt token when no container-log tail is recorded (yet) for a recorded non-zero exit: the
# guardian's one log read has not finished or not been recorded when the receipt is built
# (q-2716 review F4; not ``no_trusted_log_source``, since the read path exists).
STDERR_UNAVAILABLE = {"outcome": "unavailable", "reason": "not_read"}


def _utc_day() -> str:
    """Salad's log limit resets at 00:00 UTC: the wall-clock UTC date, never a monotonic clock."""
    return datetime.now(UTC).date().isoformat()


def nonzero_exit_codes(record: Any) -> list[int]:
    """The non-zero, non-platform exit codes of a closed start-events record that names an exit,
    when the earliest non-zero exit is the workload's own.

    Codes are in first-seen order (``salad_events``). A crash loop the guardian stopped first
    reads ``[1, 137]``: the entry failed (1) before the stop's kill (137), so it is the
    workload's failure. A kill or platform stop that came first (``[137, ...]``, ``[143, ...]``)
    keeps today's handling: it is not an entry failure."""
    closed = closed_record(record)
    if closed is None or closed["outcome"] != "read" or "instance_exited" not in closed["events"]:
        return []
    nonzero = [code for code in closed["exit_codes"] if code != 0]
    if not nonzero or nonzero[0] in PLATFORM_EXIT_CODES:
        return []
    return [code for code in nonzero if code not in PLATFORM_EXIT_CODES]


def container_exit_receipt(record: Any, container_log: Any = None) -> dict[str, Any] | None:
    """Receipt evidence for a recorded non-zero exit, or None (every other receipt is unchanged).

    ``stderr_tail`` is the guardian's one bounded, sanitized container-log tail
    (``salad_container_log``: untrusted text, marked so) when it recorded one, else a closed
    ``unavailable`` token."""
    from .salad_container_log import closed_container_log

    codes = nonzero_exit_codes(record)
    if not codes:
        return None
    tail = closed_container_log(container_log)
    return {
        "exit_codes": codes,
        "stderr_tail": tail if tail is not None else dict(STDERR_UNAVAILABLE),
    }


def failure_detail(record: Any) -> str | None:
    codes = nonzero_exit_codes(record)
    if not codes:
        return None
    return (
        f"{FAILURE_CLASS}: the container process exited with code {codes[0]}; the group was "
        "stopped instead of letting the provider restart it (log tail: receipt "
        "guardian_container_exit)"
    )[:500]


class ContainerExitWatch:
    """Mixin for ``SaladSafetyService``: a throttled, off-thread events read while armed."""

    def _init_exit_watch(self):
        self._exit_watch = {}
        # Watch reads per UTC day, and the UTC day until which a rate limit stops every watch.
        self._exit_watch_day = dict(day=None, reads=0, quota_day=None)
        # Finished container-log reads, (operation, closed value), recorded on the loop thread.
        self._container_log_done = []

    def _read_container_log(self, row):
        """After a recorded non-zero exit: one durable-claimed ``read_logs`` of the exact group,
        off the loop thread; the bounded, sanitized tail is recorded by a later pass
        (``_drain_container_logs``). A failed or rate-limited read records a closed token and
        is never repeated. Nothing here raises."""
        from .salad_container_log import summarize_container_log, unavailable

        try:
            if not row["provider_id"] or not self.guardian.claim_container_log_read(
                row["operation"]
            ):
                return
            operation, provider_id = row["operation"], row["provider_id"]
            done = self._container_log_done
            if self._exit_watch_day.get("quota_day") == _utc_day():
                done.append((operation, unavailable("log_quota_exhausted")))
                return

            def read():
                try:
                    observation = self._bounded_send(row, "read_logs", provider_id).observation
                    value = observation["container_log"]
                    if type(value) is not dict:
                        value = summarize_container_log(None)
                except Exception as error:
                    limited = getattr(error, "detail", None) == "rate_limited"
                    value = unavailable("log_quota_exhausted" if limited else "read_failed")
                done.append((operation, value))

            threading.Thread(target=read, name="container-log-read", daemon=True).start()
        except Exception:
            self.diagnostic_write_failures += 1

    def _drain_container_logs(self):
        """Record finished container-log reads (loop thread only). Nothing here raises."""
        while self._container_log_done:
            operation, value = self._container_log_done.pop(0)
            try:
                self.guardian.record_container_log(operation, value)
            except Exception:
                self.diagnostic_write_failures += 1

    def _watch_budget_open(self):
        """True while today's watch budget has room and no rate limit was seen today."""
        today = _utc_day()
        budget = self._exit_watch_day
        if budget["day"] != today:
            budget.update(day=today, reads=0)
        if budget["quota_day"] == today:
            return False
        return budget["reads"] < WATCH_DAILY_READ_BUDGET

    def _watch_container_exit(self, reservation):
        """Nothing here raises. Loop thread only (the read itself runs on a daemon thread)."""
        self._drain_container_logs()
        try:
            state = self._exit_watch.setdefault(
                reservation,
                dict(reads=0, failures=0, next=0.0, thread=None, result=None, stopped=False),
            )
            if state["stopped"]:
                return
            if state["result"] is not None:
                result, state["result"] = state["result"], None
                outcome = result[1].get("outcome")
                if outcome == "log_quota_exhausted":
                    # Salad's daily log limit: no watch reads again until 00:00 UTC.
                    self._exit_watch_day["quota_day"] = _utc_day()
                if outcome in {"log_quota_exhausted", "unavailable"}:
                    state["failures"] += 1
                    state["next"] = max(
                        state["next"], self.clock() + self._watch_gap(state, failed=True)
                    )
                    return
                state["failures"] = 0
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
            if not self._watch_budget_open():
                return
            self._exit_watch_day["reads"] += 1
            state["reads"] += 1
            state["next"] = now + self._watch_gap(state)
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
                except Exception as error:
                    limited = getattr(error, "detail", None) == "rate_limited"
                    record = empty_record("log_quota_exhausted" if limited else "unavailable")
                state["result"] = (operation, record)

            thread = threading.Thread(target=read, name="exit-watch-read", daemon=True)
            state["thread"] = thread
            thread.start()
        except Exception:
            self.diagnostic_write_failures += 1

    @staticmethod
    def _watch_gap(state, *, failed=False):
        """Seconds to the next read: doubling every 4 reads, doubling again per failed read."""
        gap = WATCH_INTERVAL_SECONDS * 2 ** (max(state["reads"] - 1, 0) // 4)
        if failed:
            gap *= 2 ** min(state["failures"], 4)
        return min(gap, WATCH_MAX_INTERVAL_SECONDS)

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
            # Why it exited: one bounded container-log tail for the receipt (after the stop).
            self._read_container_log(row)
