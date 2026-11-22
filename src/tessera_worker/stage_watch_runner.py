"""Run the observe-and-report stage watcher beside a stage (ADR 0087, S2 wiring).

``StageWatchRunner`` owns one ``StageWatcher`` per running operation. It has no thread and no
run loop: the CPU supervisor calls it from its own tick, and every call does bounded work.

- ``on_start(operation, lease)``: the stage is running. Idempotent; the first call builds the
  watcher (contract from the real operation and lease, Docker probe over the adapter's own
  ``run`` and container name and labels, control sink over the worker's existing control
  channel, durable outbox under ``<state>/stage-watch/<operation_id>.sqlite3``).
- ``on_start_remote(info)``: the same for a stage that runs at a remote provider. The remote
  driver supplies the attempt's identity (``RemoteOperationInfo``, from its own lease and package
  rows); the probe comes from the factory registered for ``info.provider``
  (``register_probe_factory``). An unknown provider is refused and counted, never raised. The
  ready-made ``snapshot_file_probe_factory`` reads ``signals_path(operation_id)``, a snapshot a
  provider-side component writes; the runner itself makes no provider call.
- ``tick()``: one sample and one flush per active watcher (flush only once it is done), within
  the tick budget below.
- ``on_end(operation_id)``: the stage ended. The watcher is kept, sampled until it is done and
  flushed until ``finished`` (nothing pending), then dropped. The outbox's 24 h abandon bounds
  this.
- ``close()``: one last best-effort flush (within the same budget), then forget every
  watcher. Pending reports stay in the outbox; the next runner over the same state directory
  resumes them (flush only, without sampling) even when no operation is running, until nothing
  is pending.

Isolation: every call into a watcher is caught. An exception is counted (``errors``) and never
reaches the supervisor, so it cannot change an execution result. Delivery never sleeps (one
attempt per report per tick, a no-op sleep, a short per-report HTTP timeout); a failed delivery
is simply retried on a later tick. The runner has no stop, cancel, retry, resize or re-route
path, holds no credential of its own and receives no management credential: its only network
path is the worker's existing control channel, and only for the watch-reports and
watch-decisions routes. The kill switch is read by the watcher and recorded on each report, and
observe and report continue whatever its state. With no kill switch path the runner starts no
watcher (fail closed).

Decision requests: ``on_start``/``on_start_remote`` take an optional ``DecisionEnvelope``. With
none (the default) the contract has no envelope and no request is ever filed. With one, the
watcher may file a closed ``retry_request``/``resize_request`` (see
``stage_watcher.decisions``) through the same channel; the runner forwards it and never acts
on it. ``status()`` counts ``decision_requests`` (recorded) and ``decision_refused`` (settled
as refused by control) for this process, without ids.

``StageWatchConfig.stats`` makes the local Docker probe also read ``docker stats``, so reports
carry ``mem_bytes``.

Tick budget: the supervisor calls ``tick()`` on the thread that also renews the worker's lease,
so a backlog after a control outage, or a control plane that times out, must not hold that
thread. Each ``tick()`` (and ``close()``) spends at most ``tick_budget_seconds`` of wall time
(the runner's injectable ``monotonic`` clock) and makes at most ``tick_max_posts`` delivery
attempts across ALL watchers, reports and decision requests together (a watcher flushes its
decision requests after its reports); each post's HTTP timeout is also capped by the time left.
Once the time is gone no further watcher is sampled or flushed; once the posts are gone the
running watchers are still sampled (and record their reports and requests) but nothing more is
posted. Whatever does not fit stays recorded in the outbox for a later tick (record before post:
nothing is lost). Watchers holding a pending ``stuck``/``failed`` go first; the others are
served round-robin from an in-memory cursor that moves past the last watcher served, so none is
starved.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .stage_watch_config import StageWatchConfig
from .stage_watcher import (
    ComputeProbe,
    ControlDecisionSink,
    ControlSink,
    DecisionEnvelope,
    DockerInspectProbe,
    KillSwitch,
    ProbeSample,
    RunCommand,
    StageContract,
    StageWatcher,
    WatchOutbox,
)
from .stage_watcher.contract import (
    ENVELOPE_FIELDS,
    MAX_DEADLINE_SECONDS,
    MAX_EPOCH,
    MAX_REPORTS_LIMIT,
)

PROVIDER = "local-docker"
STAGE_KEY = "local-cpu"
WATCH_DIRECTORY = "stage-watch"
SIGNALS_DIRECTORY = "signals"
SIGNALS_SUFFIX = ".signals.json"
CONTRACT_SUFFIX = ".contract.json"
OUTBOX_SUFFIX = ".sqlite3"
# Default freshness bound for a provider-side signal snapshot: older reads as "unknown".
DEFAULT_SIGNAL_MAX_AGE_SECONDS = 120
# A watcher that raises this many ticks in a row is dropped for this process; its outbox stays
# on disk and the next runner resumes any pending reports.
MAX_CONSECUTIVE_ERRORS = 10
# Bounds on what a restart scans and reads.
MAX_RESUMED_OUTBOXES = 256
MAX_CONTRACT_BYTES = 8192

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CONTRACT_FIELDS = frozenset(
    {
        "operation_id",
        "lease_id",
        "epoch",
        "stage_key",
        "provider",
        "deadline_seconds",
        "silent_seconds",
        "heartbeat_seconds",
        "max_reports",
        "kill_switch_path",
    }
)
_OPTIONAL_CONTRACT_FIELDS = frozenset(ENVELOPE_FIELDS)

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]
# ``factory(contract, watch_directory) -> ComputeProbe``: builds the read-only probe for one
# remote attempt. ``watch_directory`` is the runner's owner-only ``<state>/stage-watch``.
ProbeFactory = Callable[[StageContract, Path], ComputeProbe]

_PROBE_FACTORIES: dict[str, ProbeFactory] = {}


def register_probe_factory(provider_id: str, factory: ProbeFactory) -> None:
    """Register (or replace) the probe factory for a remote provider id.

    ``local-docker`` is reserved: its probe is bound per runner to the adapter's own Docker
    ``run`` and the operation's ownership labels, which a ``(contract, directory)`` factory
    cannot carry, so the runner resolves it itself and it cannot be overridden here.
    """
    if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("provider id must be a bounded lowercase identifier.")
    if provider_id == PROVIDER:
        raise ValueError("local-docker is the runner's own probe and cannot be registered.")
    if not callable(factory):
        raise ValueError("a probe factory must be callable.")
    _PROBE_FACTORIES[provider_id] = factory


def unregister_probe_factory(provider_id: str) -> None:
    """Forget a provider's factory (a no-op when none is registered)."""
    _PROBE_FACTORIES.pop(provider_id, None)


def probe_factory(provider_id: str) -> ProbeFactory | None:
    """The registered factory for a remote provider id, else None."""
    if not isinstance(provider_id, str):
        return None
    return _PROBE_FACTORIES.get(provider_id)


def registered_providers() -> tuple[str, ...]:
    """Every provider id with a probe: ``local-docker`` (built in) and the registered ones."""
    return tuple(sorted({PROVIDER, *_PROBE_FACTORIES}))


def snapshot_file_probe_factory(
    signals_dir: Path | str | None = None,
    max_age_seconds: int = DEFAULT_SIGNAL_MAX_AGE_SECONDS,
    *,
    clock: Clock | None = None,
) -> ProbeFactory:
    """A generic factory: read ``<signals_dir>/<operation_id>.signals.json``.

    A provider-side component only has to write that snapshot (``write_signal_snapshot``);
    stale, missing or malformed snapshots read as "unknown". With ``signals_dir`` None the
    runner's own ``<state>/stage-watch/signals`` is used (``StageWatchRunner.signals_path``).
    The snapshot's age is measured against ``clock`` when given, else against the watcher's
    own ``now`` for each sample (so it follows the runner's clock).
    """
    fixed = None if signals_dir is None else Path(signals_dir)

    def factory(contract: StageContract, watch_directory: Path) -> ComputeProbe:
        from .stage_watcher.signals import SignalProbe

        directory = fixed if fixed is not None else Path(watch_directory) / SIGNALS_DIRECTORY
        path = directory / f"{contract.operation_id}{SIGNALS_SUFFIX}"
        if clock is not None:
            return SignalProbe.from_file(path, max_age_seconds, clock)
        sample_clock = _SampleClock()
        return _SampleDatedProbe(
            SignalProbe.from_file(path, max_age_seconds, sample_clock), sample_clock
        )

    return factory


class _SampleClock:
    """The ``now`` of the sample being taken (wall clock before the first one)."""

    def __init__(self) -> None:
        self.now: datetime | None = None

    def __call__(self) -> datetime:
        return self.now if self.now is not None else datetime.now(UTC)


class _SampleDatedProbe:
    """Hand the watcher's ``now`` to the snapshot reader, so staleness uses the same clock."""

    def __init__(self, probe: ComputeProbe, clock: _SampleClock) -> None:
        self._probe = probe
        self._clock = clock

    def sample(self, now: datetime) -> Any:
        self._clock.now = now
        return self._probe.sample(now)


@dataclass(frozen=True)
class RemoteOperationInfo:
    """What a remote driver knows about one attempt from its own lease and package rows.

    Identity and bounds only: no credential, URL or provider handle.
    """

    operation_id: str
    lease_id: str
    epoch: int
    stage_key: str
    provider: str
    deadline_seconds: int
    plan_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID.fullmatch(self.operation_id):
            raise ValueError("operation_id must be 32 lowercase hex characters.")
        for name in ("lease_id", "stage_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                raise ValueError(f"{name} must be a bounded identifier token.")
        if not isinstance(self.provider, str) or not _PROVIDER_ID.fullmatch(self.provider):
            raise ValueError("provider must be a bounded lowercase identifier.")
        if (
            isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or not 0 <= self.epoch <= MAX_EPOCH
        ):
            raise ValueError("epoch must be a non-negative integer.")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, int)
            or self.deadline_seconds < 1
        ):
            raise ValueError("deadline_seconds must be a positive integer.")
        if self.plan_digest is not None and (
            not isinstance(self.plan_digest, str) or not _DIGEST.fullmatch(self.plan_digest)
        ):
            raise ValueError("plan_digest must be None or 64 lowercase hex characters.")


class _UnsampledProbe:
    """Stands in for a resumed remote watcher, which is only flushed, never sampled."""

    @staticmethod
    def sample(now: datetime) -> ProbeSample:
        return ProbeSample("unknown", now)


def _no_sleep(_seconds: float) -> None:
    """The watcher never blocks the supervisor tick: a failed delivery waits for a later tick."""


@dataclass
class _Entry:
    watcher: StageWatcher
    ended: bool = False
    # Resumed after a restart with no running operation: flush only, never sample (a gone
    # container must not be reported as a failure after the fact).
    resumed: bool = False
    consecutive_errors: int = 0
    remote: bool = False
    # The watcher's decision counts already added to the runner's totals.
    seen_requests: int = 0
    seen_refused: int = 0


class StageWatchRunner:
    """One stage watcher per running operation (local Docker or remote); see the module
    docstring."""

    def __init__(
        self,
        config: StageWatchConfig,
        *,
        state_dir: Path | str,
        channel: Any,
        docker_run: RunCommand,
        clock: Clock | None = None,
        monotonic: Monotonic | None = None,
    ) -> None:
        if not isinstance(config, StageWatchConfig) or not config.enabled:
            raise ValueError("StageWatchRunner needs an enabled StageWatchConfig.")
        if not callable(docker_run):
            raise ValueError("StageWatchRunner needs the Docker adapter's run callable.")
        self._config = config
        self._channel = channel
        self._docker_run = docker_run
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._monotonic: Monotonic = monotonic or time.monotonic
        # Round-robin start for the next tick (an operation id; memory only).
        self._cursor: str | None = None
        # The running tick's deadline (``monotonic`` time), None between ticks.
        self._tick_deadline: float | None = None
        self._entries: dict[str, _Entry] = {}
        self._refused: set[str] = set()
        self.errors = 0
        self.refused_no_kill_switch = 0
        self.refused_unknown_provider = 0
        self.decision_requests = 0
        self.decision_refused = 0
        self._directory: Path | None = None
        self._signals_directory: Path | None = None
        try:
            self._directory = self._prepare_directory(Path(state_dir) / WATCH_DIRECTORY)
        except Exception:
            self.errors += 1
            return
        self._resume()

    # -- read-only status -------------------------------------------------------------------

    @property
    def active_operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    @property
    def directory(self) -> Path | None:
        return self._directory

    def status(self) -> dict[str, Any]:
        """Counts only: no paths, no ids, no report content."""
        remote = sum(1 for entry in self._entries.values() if entry.remote)
        return {
            "enabled": True,
            "watching": len(self._entries),
            "watching_local": len(self._entries) - remote,
            "watching_remote": remote,
            "errors": self.errors,
            "refused_no_kill_switch": self.refused_no_kill_switch,
            "refused_unknown_provider": self.refused_unknown_provider,
            "decision_requests": self.decision_requests,
            "decision_refused": self.decision_refused,
            "kill_switch_engaged": KillSwitch(self._config.kill_switch_path).engaged(),
        }

    # -- supervisor hooks -------------------------------------------------------------------

    def on_start(
        self, operation: Any, lease: Any, envelope: DecisionEnvelope | None = None
    ) -> None:
        """The operation is running: make sure it has a watcher. Never raises."""
        try:
            self._on_start(operation, lease, envelope)
        except Exception:
            self.errors += 1
            operation_id = getattr(operation, "operation_id", None)
            if isinstance(operation_id, str):
                self._refused.add(operation_id)

    def on_start_remote(
        self, info: RemoteOperationInfo, envelope: DecisionEnvelope | None = None
    ) -> None:
        """A remote stage is running: make sure it has a watcher. Idempotent. Never raises."""
        try:
            self._on_start_remote(info, envelope)
        except Exception:
            self.errors += 1
            operation_id = getattr(info, "operation_id", None)
            if isinstance(operation_id, str):
                self._refused.add(operation_id)

    def signals_path(self, operation_id: str) -> Path | None:
        """Where a provider-side component writes this operation's signal snapshot.

        Creates or checks the owner-only (0700) ``<state>/stage-watch/signals`` directory.
        None for a malformed id or an unusable directory. Never raises.
        """
        try:
            if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
                return None
            directory = self._signals_dir()
            if directory is None:
                return None
            return directory / f"{operation_id}{SIGNALS_SUFFIX}"
        except Exception:
            self.errors += 1
            return None

    def on_end(self, operation_id: str) -> None:
        """The stage ended: keep its watcher until ``finished``, then drop it. Never raises."""
        entry = self._entries.get(operation_id)
        if entry is not None:
            entry.ended = True

    def tick(self) -> None:
        """Sample and flush the watchers within the tick budget; drop the finished ones.
        Never raises."""
        try:
            self._serve(sample=True)
        except Exception:
            self.errors += 1
        finally:
            self._tick_deadline = None

    def close(self) -> None:
        """One last flush within the tick budget, then forget every watcher. Never raises."""
        try:
            self._serve(sample=False)
        except Exception:
            self.errors += 1
        finally:
            self._tick_deadline = None
            self._entries.clear()

    def _remaining(self) -> float | None:
        """Seconds left in the running tick (None outside one): caps each post's timeout."""
        deadline = self._tick_deadline
        return None if deadline is None else deadline - self._monotonic()

    def _rotation(self) -> list[str]:
        """Every watched operation id, starting at the round-robin cursor."""
        ids = sorted(self._entries)
        if self._cursor is None or not ids:
            return ids
        start = bisect_left(ids, self._cursor) % len(ids)
        return ids[start:] + ids[:start]

    def _terminal_pending(self, entry: _Entry) -> bool:
        try:
            return entry.watcher.has_pending_terminal
        except Exception:
            self.errors += 1
            return False

    def _serve(self, *, sample: bool) -> None:
        """One bounded pass: at most ``tick_max_posts`` posts and ``tick_budget_seconds``."""
        rotation = self._rotation()
        if not rotation:
            return
        deadline = self._monotonic() + float(self._config.tick_budget_seconds)
        self._tick_deadline = deadline
        posts_left = self._config.tick_max_posts
        urgent = [i for i in rotation if self._terminal_pending(self._entries[i])]
        urgent_set = set(urgent)
        last_served: str | None = None
        for operation_id in urgent + [i for i in rotation if i not in urgent_set]:
            if self._monotonic() >= deadline:
                break  # out of time: the rest go first next tick (the cursor stays before them)
            entry = self._entries.get(operation_id)
            if entry is None:
                continue
            watcher = entry.watcher
            flush_only = not sample or entry.resumed or watcher.done
            if flush_only and posts_left <= 0:
                continue  # nothing left to do for it this tick
            served = False
            try:
                if flush_only:
                    watcher.flush(max_posts=posts_left, deadline=deadline)
                else:
                    watcher.tick(max_posts=posts_left, deadline=deadline)
                posts = watcher.last_posts
                served = posts_left > 0 and (posts > 0 or watcher.last_flush_complete)
                posts_left = max(0, posts_left - posts)
                entry.consecutive_errors = 0
                if not sample:
                    drop = False
                elif entry.resumed:
                    drop = watcher.pending_count == 0
                else:
                    drop = entry.ended and watcher.finished
            except Exception:
                self.errors += 1
                entry.consecutive_errors += 1
                drop = entry.consecutive_errors >= MAX_CONSECUTIVE_ERRORS
            self._count_decisions(entry)
            if served and operation_id not in urgent_set:
                last_served = operation_id
            if drop:
                self._entries.pop(operation_id, None)
        if last_served is not None:
            self._cursor = rotation[(rotation.index(last_served) + 1) % len(rotation)]

    # -- internals --------------------------------------------------------------------------

    def _count_decisions(self, entry: _Entry) -> None:
        """Add the watcher's new decision counts to the runner totals (counts only)."""
        requests = getattr(entry.watcher, "decision_requests", 0)
        refused = getattr(entry.watcher, "decision_refused", 0)
        if isinstance(requests, int) and requests > entry.seen_requests:
            self.decision_requests += requests - entry.seen_requests
            entry.seen_requests = requests
        if isinstance(refused, int) and refused > entry.seen_refused:
            self.decision_refused += refused - entry.seen_refused
            entry.seen_refused = refused

    @staticmethod
    def _envelope_fields(envelope: DecisionEnvelope | None) -> dict[str, Any]:
        if envelope is None:
            return {}
        if not isinstance(envelope, DecisionEnvelope):
            raise TypeError("envelope must be a DecisionEnvelope or None.")
        return envelope.contract_fields()

    def _on_start(self, operation: Any, lease: Any, envelope: DecisionEnvelope | None) -> None:
        operation_id = operation.operation_id
        entry = self._entries.get(operation_id)
        if entry is not None:
            if entry.remote:  # a remote stage is observed through ``on_start_remote`` only
                return
            if entry.resumed:  # recovered and running again: observe it from here on
                entry.resumed = False
                entry.ended = False
            return
        if self._directory is None or operation_id in self._refused:
            return
        if self._config.kill_switch_path is None:
            # Fail closed: without a kill switch reference the watcher does not start.
            self.refused_no_kill_switch += 1
            self._refused.add(operation_id)
            return
        deadline = min(int(operation.max_duration_seconds), MAX_DEADLINE_SECONDS)
        contract = StageContract(
            operation_id=operation_id,
            lease_id=lease.id,
            epoch=lease.epoch,
            stage_key=STAGE_KEY,
            provider=PROVIDER,
            deadline_seconds=deadline,
            silent_seconds=min(self._config.silent_seconds, deadline),
            heartbeat_seconds=min(self._config.heartbeat_seconds, deadline),
            max_reports=self._config.max_reports,
            kill_switch_path=self._config.kill_switch_path,
            **self._envelope_fields(envelope),
        )
        stored = self._load_contract(operation_id)
        if stored is not None:
            # A restarted worker keeps the contract it started with (the outbox is bound to
            # its digest); it must be the same attempt.
            contract, plan_digest = stored
            if (contract.lease_id, contract.epoch) != (lease.id, lease.epoch):
                raise ValueError("stored stage contract belongs to another attempt.")
            if plan_digest != operation.plan_digest:
                raise ValueError("stored stage contract belongs to another plan.")
            if contract.provider != PROVIDER:
                raise ValueError("stored stage contract belongs to a remote stage.")
        else:
            self._store_contract(contract, operation.plan_digest)
        self._entries[operation_id] = _Entry(self._watcher(contract, operation.plan_digest))

    def _on_start_remote(
        self, info: RemoteOperationInfo, envelope: DecisionEnvelope | None
    ) -> None:
        if not isinstance(info, RemoteOperationInfo):
            raise TypeError("on_start_remote needs a RemoteOperationInfo.")
        operation_id = info.operation_id
        entry = self._entries.get(operation_id)
        if entry is not None and not entry.resumed:
            return
        if self._directory is None or operation_id in self._refused:
            return
        if self._config.kill_switch_path is None:
            # Fail closed, exactly like the local path.
            self.refused_no_kill_switch += 1
            self._refused.add(operation_id)
            return
        if info.provider == PROVIDER or probe_factory(info.provider) is None:
            # No probe for this provider (local Docker is served by ``on_start``).
            self.refused_unknown_provider += 1
            self._refused.add(operation_id)
            return
        deadline = min(info.deadline_seconds, MAX_DEADLINE_SECONDS)
        contract = StageContract(
            operation_id=operation_id,
            lease_id=info.lease_id,
            epoch=info.epoch,
            stage_key=info.stage_key,
            provider=info.provider,
            deadline_seconds=deadline,
            silent_seconds=min(self._config.silent_seconds, deadline),
            heartbeat_seconds=min(self._config.heartbeat_seconds, deadline),
            max_reports=min(self._config.max_reports, MAX_REPORTS_LIMIT),
            kill_switch_path=self._config.kill_switch_path,
            **self._envelope_fields(envelope),
        )
        stored = self._load_contract(operation_id)
        if stored is not None:
            # A restarted worker keeps the contract it started with; same attempt only.
            contract, plan_digest = stored
            if (contract.lease_id, contract.epoch) != (info.lease_id, info.epoch):
                raise ValueError("stored stage contract belongs to another attempt.")
            if plan_digest != info.plan_digest:
                raise ValueError("stored stage contract belongs to another plan.")
            if (contract.provider, contract.stage_key) != (info.provider, info.stage_key):
                raise ValueError("stored stage contract belongs to another stage.")
        else:
            self._store_contract(contract, info.plan_digest)
        # A resumed (flush-only) entry is rebuilt with the provider's probe from here on.
        self._entries[operation_id] = _Entry(
            self._watcher(contract, info.plan_digest, sample=True), remote=True
        )

    def _probe(self, contract: StageContract, plan_digest: str | None, sample: bool) -> Any:
        operation_id = contract.operation_id
        if contract.provider == PROVIDER:
            if plan_digest is None:
                raise ValueError("a local stage contract needs its plan digest.")
            return DockerInspectProbe(
                self._docker_run,
                f"tessera-op-{operation_id}",  # CpuOperation.container_name
                # The adapter's own ownership check (DockerCpuAdapter.observe).
                expected_labels={"tessera.operation": operation_id, "tessera.plan": plan_digest},
                stats=self._config.stats,
            )
        if not sample:
            return _UnsampledProbe()
        factory = probe_factory(contract.provider)
        if factory is None:
            raise ValueError("no probe factory for this provider.")
        assert self._directory is not None
        return factory(contract, self._directory)

    def _watcher(
        self, contract: StageContract, plan_digest: str | None, *, sample: bool = True
    ) -> StageWatcher:
        operation_id = contract.operation_id
        assert self._directory is not None
        probe = self._probe(contract, plan_digest, sample)
        timeout = self._config.delivery_timeout_seconds
        sink = ControlSink(self._channel, timeout_seconds=timeout, remaining=self._remaining)
        outbox = WatchOutbox(self._directory / f"{operation_id}{OUTBOX_SUFFIX}")
        return StageWatcher(
            contract,
            probe,
            sink,
            KillSwitch(contract.kill_switch_path),
            self._clock,
            outbox,
            sleep=_no_sleep,
            max_delivery_attempts=1,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
            decision_sink=ControlDecisionSink(
                self._channel, timeout_seconds=timeout, remaining=self._remaining
            ),
            monotonic=self._monotonic,
        )

    def _resume(self) -> None:
        """Rebuild flush-only watchers for outboxes that still hold pending reports."""
        assert self._directory is not None
        try:
            names = sorted(os.listdir(self._directory))
        except OSError:
            self.errors += 1
            return
        resumed = 0
        for name in names:
            if resumed >= MAX_RESUMED_OUTBOXES:
                break
            if not name.endswith(CONTRACT_SUFFIX):
                continue
            operation_id = name[: -len(CONTRACT_SUFFIX)]
            if not _OPERATION_ID.fullmatch(operation_id):
                continue
            try:
                outbox_path = self._directory / f"{operation_id}{OUTBOX_SUFFIX}"
                if not outbox_path.is_file() or outbox_path.is_symlink():
                    continue
                if WatchOutbox(outbox_path).pending_count() == 0:
                    continue
                stored = self._load_contract(operation_id)
                if stored is None:
                    continue
                contract, plan_digest = stored
                self._entries[operation_id] = _Entry(
                    self._watcher(contract, plan_digest, sample=False),
                    resumed=True,
                    remote=contract.provider != PROVIDER,
                )
                resumed += 1
            except Exception:
                self.errors += 1

    def _contract_path(self, operation_id: str) -> Path:
        assert self._directory is not None
        return self._directory / f"{operation_id}{CONTRACT_SUFFIX}"

    def _store_contract(self, contract: StageContract, plan_digest: str | None) -> None:
        """Write the contract (no credential, no URL) owner-only, atomically, without links."""
        document = {"contract": json.loads(contract.canonical_json()), "plan_digest": plan_digest}
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
        path = self._contract_path(contract.operation_id)
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def _load_contract(self, operation_id: str) -> tuple[StageContract, str | None] | None:
        path = self._contract_path(operation_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("stage contract must be a regular file.")
            encoded = handle.read(MAX_CONTRACT_BYTES + 1)
        if len(encoded) > MAX_CONTRACT_BYTES:
            raise ValueError("stage contract exceeds its bound.")
        document = json.loads(encoded)
        if not isinstance(document, dict) or set(document) != {"contract", "plan_digest"}:
            raise ValueError("stage contract file has an unexpected shape.")
        fields, plan_digest = document["contract"], document["plan_digest"]
        if not isinstance(fields, dict) or not (
            _CONTRACT_FIELDS <= set(fields) <= _CONTRACT_FIELDS | _OPTIONAL_CONTRACT_FIELDS
        ):
            raise ValueError("stage contract has an unexpected field set.")
        contract = StageContract(**fields)
        if contract.operation_id != operation_id:
            raise ValueError("stage contract belongs to another operation.")
        # A remote driver may not know the plan digest; a local stage always has one.
        if (plan_digest is not None or contract.provider == PROVIDER) and (
            not isinstance(plan_digest, str) or not _DIGEST.fullmatch(plan_digest)
        ):
            raise ValueError("stage contract plan digest is malformed.")
        return contract, plan_digest

    def _signals_dir(self) -> Path | None:
        if self._signals_directory is None and self._directory is not None:
            self._signals_directory = self._prepare_directory(self._directory / SIGNALS_DIRECTORY)
        return self._signals_directory

    @staticmethod
    def _prepare_directory(path: Path) -> Path:
        """Create or check the owner-only (0700), non-symlink outbox directory."""
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("stage watch directory must be a real directory.")
        if info.st_uid != os.geteuid():
            raise ValueError("stage watch directory must be owned by this user.")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
        return path


INSPECT_TIMEOUT_SECONDS = 2.0


def _default_docker() -> Any:
    from . import cpu

    return cpu._docker


def bounded_docker(arguments: Any) -> Any:
    """``cpu._docker`` with a short timeout, for the watcher's read-only inspect/stats calls.

    A slow or hung Docker daemon makes the sample ``unknown`` (the probe never raises) instead
    of holding the supervisor thread, which also renews the worker's lease."""
    import subprocess

    from . import cpu

    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise cpu.CpuError("Docker reply unavailable for the watcher.") from error
    if len(result.stdout.encode()) + len(result.stderr.encode()) > cpu.MAX_CAPTURE_BYTES:
        raise cpu.CpuError("Docker response exceeded its bound.")
    return result


def build_stage_watch_runner(
    config: StageWatchConfig | None,
    *,
    state_dir: Path | str,
    channel: Any,
    adapter: Any,
    clock: Clock | None = None,
    monotonic: Monotonic | None = None,
) -> StageWatchRunner | None:
    """The runner for an enabled config and a local Docker adapter, else None. Never raises.

    The probe uses the adapter's own ``run`` (the same Docker command runner the adapter
    executes with); an adapter without one is not local Docker and gets no watcher.
    """
    if config is None or not config.enabled:
        return None
    docker_run = getattr(adapter, "run", None)
    if docker_run is not None and docker_run is _default_docker():
        # The adapter's own runner waits up to 30 s for Docker. The watcher shares the
        # supervisor thread (and so the lease renewal), so its inspect gets a short bound.
        docker_run = bounded_docker
    if not callable(docker_run):
        return None
    try:
        return StageWatchRunner(
            config,
            state_dir=state_dir,
            channel=channel,
            docker_run=docker_run,
            clock=clock,
            monotonic=monotonic,
        )
    except Exception:
        return None
