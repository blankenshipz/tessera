"""Observe-and-report stage watcher (ADR 0087 slice S2).

The watcher's only actions are observe (one probe sample per tick) and report (record the
report in the durable outbox, then deliver it). It has no stop, cancel, retry, resize or
re-route operation, holds no management credential, and reaches no network except through the
injected sink. Its classification is deterministic over the probe samples and the clock:

- ``failed``: the run exited non-zero or was OOM-killed, the watch deadline passed while the
  run was still running, or the probe read "unknown"/"absent" for 3 consecutive ticks. A
  single missed read is never a failure.
- ``progress``: output bytes, CPU time or the probe's internal ``progress_marker`` changed
  since the last mark (in either direction: a container restart can reset them), or the
  restart count rose; reported at most once per ``MIN_ROUTINE_INTERVAL_SECONDS``.
- ``stuck``: running with a progress signal (output bytes, CPU time or a progress marker) and
  no advance for ``silent_seconds``. For a remote provider whose only signal is the marker its
  driver advances once per successful poll, this means "the provider-side driver stopped
  observing", not "the workload stalled". Reported once per episode, re-armed only after
  progress resumes. After a watcher restart silence is measured from the first post-restart
  sample (the deadline still counts from the original start).
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

Delivery is three-valued: delivered, refused (a permanent status for the kind, or a
permanent reason code from control for that status, see ``settles``; settled, counted, never
retried, and later reports still flow), or transient (retried under the same key; it stops the
flush for this tick). A ``stuck`` or ``failed`` is never settled by a refusal that could clear
on retry (401, 403/409 without a permanent reason code, 425, ...);
it stays pending until delivered or abandoned after 24 h. Closing reports are flushed first.
Every report carries the outbox's refused and dropped counts once they are non-zero. Every
report is a claim, never a verdict.

``observed_at`` is monotonic: never earlier than the previous recorded report, even if the
clock steps backwards, so control's "not earlier than the last report" check cannot refuse it.

Decision requests (``decisions.py``, ADR 0087 section 4 "Requests"): only with a
``decision_sink`` and a contract envelope, and only while the kill switch is clear, an exit that
the envelope covers files at most one ``retry_request`` (transient exit code) or
``resize_request`` (OOM-killed) per kind per epoch. It is recorded in the outbox before it is
sent, delivered on its own queue under ``watch-decision-<operation_id>-<decision_seq>``, and is
only a request: the watcher never retries, resizes, re-routes or stops anything.

The watcher has no run loop. The caller must keep calling ``tick()`` (or ``flush()``) after
``done`` until ``finished`` is true, or a ``failed`` whose delivery failed transiently stays
in the outbox until the next start.

Bounded delivery: ``tick()`` and ``flush()`` take an optional ``max_posts`` (delivery attempts
this call may make; 0 means sample only) and ``deadline`` (a value of the watcher's
``monotonic`` clock after which no further attempt starts). Both default to unbounded. A flush
cut short by either leaves the rest pending (it is already recorded) for a later call;
``last_posts`` and ``last_flush_complete`` say what the last flush did. Decision requests are
flushed after the reports (closing reports first) and count against the same ``max_posts`` and
``deadline``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta

from tessera_control.stage_watch_limits import (
    MIN_ROUTINE_INTERVAL_SECONDS,
    ROUTINE_KINDS,
    TERMINAL_KINDS,
    routine_limit,
)

from .contract import StageContract
from .decisions import DecisionRequest, DecisionSink, decide, decision_settles
from .killswitch import KillSwitch
from .outbox import WatchOutbox
from .probe import ComputeProbe, ProbeSample
from .report import (
    MAX_EXIT_CODE,
    TRANSIENT,
    DeliveryResult,
    ReportKind,
    WatchExit,
    WatchReport,
    WatchReportSink,
    settles,
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


def _definite_exit(kind: ReportKind, sample: ProbeSample) -> WatchExit | None:
    """The ``exit`` a ``failed`` report carries (ADR 0158): only for a sample the probe read as
    definitively ``exited`` with an integer exit code in 0..255. A failure from an unknown or
    absent probe, a passed deadline, or an exit with no code carries none."""
    code = sample.exit_code
    if (
        kind != "failed"
        or sample.state != "exited"
        or isinstance(code, bool)
        or not isinstance(code, int)
        or not 0 <= code <= MAX_EXIT_CODE
    ):
        return None
    return WatchExit(exit_code=code, oom_killed=sample.oom_killed)


def _check_budget(max_posts: int | None) -> None:
    if max_posts is not None and (
        isinstance(max_posts, bool) or not isinstance(max_posts, int) or max_posts < 0
    ):
        raise ValueError("max_posts must be None or a non-negative integer.")


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
        decision_sink: DecisionSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
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
        # No decision sink, no decision requests (nothing is recorded that cannot be sent).
        self._decision_sink = (
            decision_sink if callable(getattr(decision_sink, "deliver_decision", None)) else None
        )
        # Requests recorded and requests settled as refused by THIS instance (local counts).
        self.decision_requests = 0
        self.decision_refused = 0
        self._monotonic = monotonic
        # Per-flush delivery budget (None: unbounded) and what the last flush did.
        self._posts_left: int | None = None
        self._deadline: float | None = None
        self._budget_hit = False
        self._last_posts = 0
        self._last_flush_complete = True
        # Whether a ``stuck``/``failed`` is pending; None until first read from the outbox.
        # Only this watcher changes its outbox, so the cached value stays exact.
        self._terminal_pending: bool | None = None

        created_at = self._now()
        self._started_at = outbox.bind(contract.operation_id, self._digest, created_at)
        self._seq = outbox.next_seq()
        self._decision_seq = outbox.next_decision_seq()
        self._routine_cap = routine_cap(contract.max_reports)
        self._done = False
        self._outcome: str | None = None
        self._unknown_ticks = 0
        self._mark: tuple[int | None, int | None, int | None, int] | None = None
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
        """Recorded reports and decision requests not yet delivered, refused or abandoned."""
        return self._outbox.pending_count()

    @property
    def finished(self) -> bool:
        """Done AND nothing left to deliver: only then may the caller stop calling tick()."""
        return self._done and self.pending_count == 0

    @property
    def has_pending_terminal(self) -> bool:
        """A ``stuck`` or ``failed`` report is recorded and not yet settled."""
        if self._terminal_pending is None:
            self._terminal_pending = any(
                report.kind in TERMINAL_KINDS for report in self._outbox.pending()
            )
        return self._terminal_pending

    @property
    def last_posts(self) -> int:
        """Delivery attempts (reports and decision requests) the last ``tick()``/``flush()``
        made."""
        return self._last_posts

    @property
    def last_flush_complete(self) -> bool:
        """False when the last flush stopped on its ``max_posts`` or ``deadline`` budget with a
        report or decision request still pending."""
        return self._last_flush_complete

    @property
    def outcome(self) -> str | None:
        """None while watching, else "exited_ok", "exited_unknown" (no exit code) or "failed"."""
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

    def tick(
        self, *, max_posts: int | None = None, deadline: float | None = None
    ) -> list[WatchReport]:
        """Observe once, record any report before delivering it, then flush the outbox
        (within ``max_posts`` and ``deadline`` when given; see the module docstring)."""
        _check_budget(max_posts)
        now = self._now()
        made: list[WatchReport] = []
        if not self._done:
            sample = self._observe(now)
            kind = self._classify(sample, now)
            if kind is not None:
                report = self._report(kind, sample, now)
                if report is not None:
                    made.append(report)
            if kind == "failed" and sample.state == "exited":
                self._file_requests(sample, now)
        self._bounded_flush(now, max_posts, deadline)
        return made

    def flush(self, *, max_posts: int | None = None, deadline: float | None = None) -> None:
        """Deliver pending reports without sampling (for a caller still flushing after done),
        within ``max_posts`` and ``deadline`` when given."""
        _check_budget(max_posts)
        self._bounded_flush(self._now(), max_posts, deadline)

    def _bounded_flush(self, now: datetime, max_posts: int | None, deadline: float | None) -> None:
        self._posts_left = max_posts
        self._deadline = deadline
        self._budget_hit = False
        self._last_posts = 0
        try:
            if self._budget_left():
                self._flush(now)
        finally:
            # Only a budget stop that left something (report or decision) behind is
            # incomplete; ``pending_count`` reads the outbox only in that case.
            if not self._budget_hit:
                self._last_flush_complete = True
            else:
                try:
                    self._last_flush_complete = self.pending_count == 0
                except Exception:
                    self._last_flush_complete = False
            self._posts_left = None
            self._deadline = None

    def _budget_left(self) -> bool:
        """May one more delivery attempt start? Records a budget stop when not."""
        if self._posts_left is not None and self._last_posts >= self._posts_left:
            self._budget_hit = True
            return False
        if self._deadline is not None and self._monotonic() >= self._deadline:
            self._budget_hit = True
            return False
        return True

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
        state = sample.state
        if state == "unknown" and sample.progress_marker is not None:
            # A fresh liveness marker: the provider-side driver is alive and polling (for
            # example while a remote group is allocating). Not a lost probe: classify it like
            # a running sample, so the marker still feeds the stuck rule and the deadline.
            state = "running"
        if state in ("unknown", "absent"):
            self._unknown_ticks += 1
            if self._unknown_ticks >= UNKNOWN_TICKS_BEFORE_FAILED:
                return self._finish("failed")
            return None
        self._unknown_ticks = 0
        if state == "exited":
            if sample.exit_code == 0 and not sample.oom_killed:
                self._finish("exited_ok")
                return None
            if sample.exit_code is None and not sample.oom_killed:
                # The provider gave no exit code (a remote route): the platform's own exit
                # observation records the outcome, so the watch ends without a verdict.
                self._finish("exited_unknown")
                return None
            return self._finish("failed")
        # running
        if now - self._started_at >= timedelta(seconds=self._contract.deadline_seconds):
            return self._finish("failed")
        # Any change counts: a container restart can lower the counters, and a working run
        # must not read as silent until it passes the old high-water mark.
        current = (sample.output_bytes, sample.cpu_ms, sample.progress_marker, sample.restarts)
        if self._mark is None:
            self._mark = current
            advanced = False
        else:
            advanced = current[3] > self._mark[3] or any(
                value is not None and value != previous
                for value, previous in zip(current[:3], self._mark[:3], strict=True)
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
            exit=_definite_exit(kind, sample),
        )
        self._outbox.record(report)  # record BEFORE any delivery attempt
        if kind in TERMINAL_KINDS:
            self._terminal_pending = True
        self._seq += 1
        self._last_report_at = observed_at
        self._last_kind_at[kind] = observed_at
        if kind == "failed":
            self._failed_recorded = True
        return report

    def _file_requests(self, sample: ProbeSample, now: datetime) -> None:
        """Record (never perform) the requests the envelope allows for this exit."""
        if self._decision_sink is None:
            return
        proposals = decide(self._contract, sample)
        if not proposals:
            return
        try:
            allowed = self._kill_switch.may_request()
        except Exception:
            allowed = False
        if not allowed:  # engaged (or unreadable): no request; the reports still go out
            return
        epoch = self._contract.epoch
        filed = self._outbox.decision_kinds(epoch)
        observed_at = now
        if self._last_report_at is not None and self._last_report_at > observed_at:
            observed_at = self._last_report_at
        for proposal in proposals:
            if proposal.kind in filed:
                continue  # at most one per kind per operation per epoch
            request = DecisionRequest(
                operation_id=self._contract.operation_id,
                lease_id=self._contract.lease_id,
                epoch=epoch,
                kind=proposal.kind,
                reason=proposal.reason,
                observed_at=observed_at,
                decision_seq=self._decision_seq,
                requested_memory_bytes=proposal.requested_memory_bytes,
                contract_digest=self._digest,
            )
            self._outbox.record_decision(request)  # record BEFORE any delivery attempt
            self._decision_seq += 1
            self.decision_requests += 1

    def _flush(self, now: datetime) -> None:
        """Deliver pending reports, closing kinds first, then pending decision requests (their
        own queue), all within the one per-flush budget. A refusal is settled and the flush
        continues; a transient failure (or a spent budget) stops that queue for this tick
        (retried under the same key)."""
        self._outbox.abandon_expired(now)
        pending = self._outbox.pending()
        self._terminal_pending = None  # unknown until this flush ends without raising
        for index, report in enumerate(pending):
            if self._deliver(report).outcome == "transient":
                # ``pending`` is terminal-first: what is left decides the cached flag.
                self._terminal_pending = any(r.kind in TERMINAL_KINDS for r in pending[index:])
                break
        else:
            self._terminal_pending = False
        if self._decision_sink is None:
            return
        # Decisions go after the reports and draw on the same per-flush post budget and
        # deadline: once it is spent, ``_deliver_decision`` starts no attempt and the request
        # stays pending (already recorded) for a later call, under the same key.
        self._outbox.abandon_expired_decisions(now)
        for request in self._outbox.pending_decisions():
            if self._deliver_decision(request).outcome == "transient":
                return

    def _deliver_decision(self, request: DecisionRequest) -> DeliveryResult:
        assert self._decision_sink is not None
        delay = self._base_backoff
        result = TRANSIENT
        for attempt in range(self._max_delivery_attempts):
            if not self._budget_left():
                return result  # still pending (transient); a later call resumes it
            if attempt:
                self._sleep(delay)
                delay = min(delay * 2, self._max_backoff)
            self._last_posts += 1
            self._outbox.note_decision_attempt(request.decision_seq)
            try:
                result = _result(self._decision_sink.deliver_decision(request))
            except Exception:
                result = TRANSIENT
            if result.outcome == "delivered":
                self._outbox.mark_decision_delivered(request.decision_seq, self._now())
                return result
            if result.outcome == "refused":
                if not decision_settles(result.status, result.reason):
                    result = DeliveryResult("transient", result.status, result.reason)
                    continue
                self._outbox.mark_decision_refused(
                    request.decision_seq, int(result.status or 0), self._now(), reason=result.reason
                )
                self.decision_refused += 1
                return result
        return result

    def _deliver(self, report: WatchReport) -> DeliveryResult:
        delay = self._base_backoff
        result = TRANSIENT
        for attempt in range(self._max_delivery_attempts):
            if not self._budget_left():
                return result  # still pending (transient); a later call resumes it
            if attempt:
                self._sleep(delay)
                delay = min(delay * 2, self._max_backoff)
            self._last_posts += 1
            self._outbox.note_attempt(report.seq)
            try:
                result = _result(self._sink.deliver(report))
            except Exception:
                result = TRANSIENT
            if result.outcome == "delivered":
                self._outbox.mark_delivered(report.seq, self._now())
                return result
            if result.outcome == "refused":
                if not settles(report.kind, result.status, result.reason):
                    # A sink may not settle a report on a status that could clear on retry
                    # (for example a ``failed`` answered 401, or 409 without a permanent
                    # reason code): keep it pending.
                    result = DeliveryResult("transient", result.status, result.reason)
                    continue
                self._outbox.mark_refused(
                    report.seq, int(result.status or 0), self._now(), reason=result.reason
                )
                return result
        return result
