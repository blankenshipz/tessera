"""Observe-and-report stage watcher (ADR 0087 slice S2).

The watcher's only actions are observe (one probe sample per tick) and report (record the
report in the durable outbox, then deliver it). It has no stop, cancel, retry, resize or
re-route operation, holds no management credential, and reaches no network except through the
injected sink. Its classification is deterministic over the probe samples and the clock:

- ``failed``: the run exited non-zero or was OOM-killed, the watch deadline passed while the
  run was still running, or the probe read "unknown"/"absent" for 3 consecutive ticks. A
  single missed read is never a failure.
- ``progress``: output bytes or CPU time changed since the last mark (in either direction: a
  container restart can reset them), or the restart count rose; reported at most once per
  ``MIN_ROUTINE_INTERVAL_SECONDS``.
- ``stuck``: running with a progress signal and no advance for ``silent_seconds``; reported
  once per episode, re-armed only after progress resumes. After a watcher restart silence is
  measured from the first post-restart sample (the deadline still counts from the original
  start).
- ``heartbeat``: at most once per ``max(heartbeat_seconds, MIN_ROUTINE_INTERVAL_SECONDS)``
  without any other report, also while stuck (so control can tell "still stuck" from "the
  watcher died").

A clean exit (code 0, not OOM-killed) or a ``failed`` report ends the watch.

Budget (shared with control, ``tessera_control.stage_watch_limits``): routine reports
(``heartbeat``/``progress``) stop once ``routine_cap(max_reports)`` reports of ANY kind are
recorded (counted from the outbox, so across restarts), which is how control counts too, and
keeps a reserve for ``stuck`` and ``failed``. The last slot is held for ``failed`` alone. If a
closing report still cannot be recorded at the total cap it is counted as a
``budget_dropped`` gap, never silently lost.

Delivery is three-valued: delivered, refused (a permanent status for the kind, see
``refusal_statuses``; settled, counted, never retried, and later reports still flow), or
transient (retried under the same key; it stops the flush for this tick). A ``stuck`` or
``failed`` is never settled by a refusal that could clear on retry (401/403, 409, 425, ...);
it stays pending until delivered or abandoned after 24 h. Closing reports are flushed first.
Every report carries the outbox's refused and dropped counts once they are non-zero. Every
report is a claim, never a verdict.

``observed_at`` is monotonic: never earlier than the previous recorded report, even if the
clock steps backwards, so control's "not earlier than the last report" check cannot refuse it.

The watcher has no run loop. The caller must keep calling ``tick()`` (or ``flush()``) after
``done`` until ``finished`` is true, or a ``failed`` whose delivery failed transiently stays
in the outbox until the next start.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta

from tessera_control.stage_watch_limits import (
    MIN_ROUTINE_INTERVAL_SECONDS,
    ROUTINE_KINDS,
    routine_limit,
)

from .contract import StageContract
from .killswitch import KillSwitch
from .outbox import WatchOutbox
from .probe import ComputeProbe, ProbeSample
from .report import (
    TRANSIENT,
    DeliveryResult,
    ReportKind,
    WatchReport,
    WatchReportSink,
    refusal_statuses,
)

UNKNOWN_TICKS_BEFORE_FAILED = 3
MIN_ROUTINE_INTERVAL = timedelta(seconds=MIN_ROUTINE_INTERVAL_SECONDS)

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]


def _seconds(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds()))


def _result(value: object) -> DeliveryResult:
    """Normalize a sink's answer; anything but a DeliveryResult (or True) is transient."""
    if isinstance(value, DeliveryResult):
        return value
    if value is True:
        return DeliveryResult("delivered")
    return TRANSIENT


def routine_cap(max_reports: int) -> int:
    """Reports of any kind recorded before routine reports stop: the shared limit (which
    control applies to its count of all recorded reports), keeping the last slot free."""
    return min(routine_limit(max_reports), max_reports - 1)


class StageWatcher:
    def __init__(
        self,
        contract: StageContract,
        probe: ComputeProbe,
        sink: WatchReportSink,
        kill_switch: KillSwitch | None,
        clock: Clock,
        outbox: WatchOutbox,
        *,
        expected_digest: str | None = None,
        sleep: Sleep = time.sleep,
        max_delivery_attempts: int = 3,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 4.0,
    ):
        if not isinstance(contract, StageContract):
            raise TypeError("contract must be a StageContract.")
        if expected_digest is not None and not contract.verify(expected_digest):
            raise ValueError("stage contract does not verify against the expected digest.")
        if (
            isinstance(max_delivery_attempts, bool)
            or not isinstance(max_delivery_attempts, int)
            or not 1 <= max_delivery_attempts <= 10
        ):
            raise ValueError("max_delivery_attempts must be an integer in 1..10.")
        if not 0 <= base_backoff_seconds <= max_backoff_seconds <= 60:
            raise ValueError("backoff must satisfy 0 <= base <= max <= 60 seconds.")
        self._contract = contract
        self._digest = contract.digest()
        self._probe = probe
        self._sink = sink
        self._kill_switch = (
            kill_switch if kill_switch is not None else KillSwitch(contract.kill_switch_path)
        )
        self._clock = clock
        self._outbox = outbox
        self._sleep = sleep
        self._max_delivery_attempts = max_delivery_attempts
        self._base_backoff = float(base_backoff_seconds)
        self._max_backoff = float(max_backoff_seconds)

        created_at = self._now()
        self._started_at = outbox.bind(contract.operation_id, self._digest, created_at)
        self._seq = outbox.next_seq()
        self._routine_cap = routine_cap(contract.max_reports)
        self._done = False
        self._outcome: str | None = None
        self._unknown_ticks = 0
        self._mark: tuple[int | None, int | None, int] | None = None
        # A restarted watcher keeps the original start (for the deadline) but measures
        # silence from its first sample: it has no evidence about the time it was down.
        restarted = self._started_at != created_at
        self._last_advance_at: datetime | None = None if restarted else self._started_at
        self._last_report_at: datetime | None = None
        self._last_kind_at: dict[str, datetime] = {}
        self._failed_recorded = False
        for recorded in outbox.recorded():
            at = recorded.observed_at
            if self._last_report_at is None or at > self._last_report_at:
                self._last_report_at = at
            previous = self._last_kind_at.get(recorded.kind)
            if previous is None or at > previous:
                self._last_kind_at[recorded.kind] = at
            if recorded.kind == "failed":
                self._failed_recorded = True
        if self._failed_recorded:  # a recorded ``failed`` ended this watch before the restart
            self._done = True
            self._outcome = "failed"
        self._stuck_reported = False
        self._budget_marked = False

    # -- read-only state -------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self._done

    @property
    def pending_count(self) -> int:
        """Recorded reports not yet delivered, refused or abandoned."""
        return self._outbox.pending_count()

    @property
    def finished(self) -> bool:
        """Done AND nothing left to deliver: only then may the caller stop calling tick()."""
        return self._done and self.pending_count == 0

    @property
    def outcome(self) -> str | None:
        """None while watching, else "exited_ok" or "failed"."""
        return self._outcome

    @property
    def budget_exhausted(self) -> bool:
        return self._outbox.budget_exhausted()

    @property
    def contract_digest(self) -> str:
        return self._digest

    def may_request(self) -> bool:
        return self._kill_switch.may_request()

    def may_call_model(self) -> bool:
        return self._kill_switch.may_call_model()

    # -- the tick --------------------------------------------------------------------------

    def tick(self) -> list[WatchReport]:
        """Observe once, record any report before delivering it, then flush the outbox."""
        now = self._now()
        made: list[WatchReport] = []
        if not self._done:
            sample = self._observe(now)
            kind = self._classify(sample, now)
            if kind is not None:
                report = self._report(kind, sample, now)
                if report is not None:
                    made.append(report)
        self._flush(now)
        return made

    def flush(self) -> None:
        """Deliver pending reports without sampling (for a caller still flushing after done)."""
        self._flush(self._now())

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime.")
        return value

    def _observe(self, now: datetime) -> ProbeSample:
        try:
            sample = self._probe.sample(now)
        except Exception:
            return ProbeSample("unknown", now)
        if not isinstance(sample, ProbeSample):
            return ProbeSample("unknown", now)
        return sample

    def _classify(self, sample: ProbeSample, now: datetime) -> ReportKind | None:
        if self._last_advance_at is None:
            self._last_advance_at = now  # first sample after a restart
        if sample.state in ("unknown", "absent"):
            self._unknown_ticks += 1
            if self._unknown_ticks >= UNKNOWN_TICKS_BEFORE_FAILED:
                return self._finish("failed")
            return None
        self._unknown_ticks = 0
        if sample.state == "exited":
            if sample.exit_code == 0 and not sample.oom_killed:
                self._finish("exited_ok")
                return None
            return self._finish("failed")
        # running
        if now - self._started_at >= timedelta(seconds=self._contract.deadline_seconds):
            return self._finish("failed")
        # Any change counts: a container restart can lower the counters, and a working run
        # must not read as silent until it passes the old high-water mark.
        current = (sample.output_bytes, sample.cpu_ms, sample.restarts)
        if self._mark is None:
            self._mark = current
            advanced = False
        else:
            advanced = current[2] > self._mark[2] or any(
                value is not None and value != previous
                for value, previous in zip(current[:2], self._mark[:2], strict=True)
            )
        if advanced:
            self._mark = current
            self._last_advance_at = now
            self._stuck_reported = False
            return "progress" if self._routine_due("progress", now) else None
        silent = now - self._last_advance_at
        if (
            sample.has_progress_signal
            and not self._stuck_reported
            and silent >= timedelta(seconds=self._contract.silent_seconds)
        ):
            self._stuck_reported = True
            return "stuck"
        # Healthy or still stuck: keep the heartbeat cadence so silence means "watcher gone".
        if (
            self._last_report_at is None
            or now - self._last_report_at >= timedelta(seconds=self._contract.heartbeat_seconds)
        ) and self._routine_due("heartbeat", now):
            return "heartbeat"
        return None

    def _routine_due(self, kind: str, now: datetime) -> bool:
        last = self._last_kind_at.get(kind)
        return last is None or now - last >= MIN_ROUTINE_INTERVAL

    def _finish(self, outcome: str) -> ReportKind | None:
        self._done = True
        self._outcome = outcome
        return "failed" if outcome == "failed" else None

    def _within_budget(self, kind: ReportKind) -> bool:
        """Routine kinds stop once ``routine_cap`` reports of any kind are recorded (control
        counts every recorded report the same way); ``stuck`` leaves the last slot to
        ``failed``; ``failed`` may use every slot up to ``max_reports``."""
        total = self._contract.max_reports
        if kind in ROUTINE_KINDS:
            return self._seq < self._routine_cap
        if kind == "stuck":
            return self._seq < total - (0 if self._failed_recorded else 1)
        return self._seq < total

    def _report(self, kind: ReportKind, sample: ProbeSample, now: datetime) -> WatchReport | None:
        if not self._within_budget(kind):
            if kind in ROUTINE_KINDS:
                if not self._budget_marked:  # by design: the reserve is kept
                    self._outbox.mark_budget_exhausted()
                    self._budget_marked = True
            else:
                self._outbox.note_budget_drop()  # an explicit, counted gap
            return None
        # Monotonic observed_at: a clock stepping backwards must not date this report before
        # the previous one (control refuses that for good). Counters still use the clock.
        observed_at = now
        if self._last_report_at is not None and self._last_report_at > observed_at:
            observed_at = self._last_report_at
        last_advance = self._last_advance_at or now
        counters: dict[str, int] = {
            "elapsed_s": _seconds(now - self._started_at),
            "silent_s": _seconds(now - last_advance),
            "restarts": sample.restarts,
        }
        for name in ("mem_bytes", "cpu_ms", "output_bytes"):
            value = getattr(sample, name)
            if value is not None:
                counters[name] = value
        gaps = self._outbox.gaps()
        if gaps.refused:
            counters["refused_reports"] = gaps.refused
        if gaps.dropped:
            counters["dropped_reports"] = gaps.dropped
        try:
            engaged = self._kill_switch.engaged()
        except Exception:
            engaged = True
        report = WatchReport(
            operation_id=self._contract.operation_id,
            lease_id=self._contract.lease_id,
            epoch=self._contract.epoch,
            kind=kind,
            observed_at=observed_at,
            seq=self._seq,
            counters=counters,
            kill_switch_engaged=engaged,
            contract_digest=self._digest,
        )
        self._outbox.record(report)  # record BEFORE any delivery attempt
        self._seq += 1
        self._last_report_at = observed_at
        self._last_kind_at[kind] = observed_at
        if kind == "failed":
            self._failed_recorded = True
        return report

    def _flush(self, now: datetime) -> None:
        """Deliver pending reports, closing kinds first. A refusal is settled and the flush
        continues; a transient failure stops it for this tick (retried under the same key)."""
        self._outbox.abandon_expired(now)
        for report in self._outbox.pending():
            if self._deliver(report).outcome == "transient":
                return

    def _deliver(self, report: WatchReport) -> DeliveryResult:
        delay = self._base_backoff
        result = TRANSIENT
        for attempt in range(self._max_delivery_attempts):
            if attempt:
                self._sleep(delay)
                delay = min(delay * 2, self._max_backoff)
            self._outbox.note_attempt(report.seq)
            try:
                result = _result(self._sink.deliver(report))
            except Exception:
                result = TRANSIENT
            if result.outcome == "delivered":
                self._outbox.mark_delivered(report.seq, self._now())
                return result
            if result.outcome == "refused":
                if result.status not in refusal_statuses(report.kind):
                    # A sink may not settle a report on a status that could clear on retry
                    # (for example a ``failed`` answered 401 or 409): keep it pending.
                    result = DeliveryResult("transient", result.status)
                    continue
                self._outbox.mark_refused(report.seq, int(result.status or 0), self._now())
                return result
        return result
