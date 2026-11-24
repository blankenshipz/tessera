"""Off-by-default, provider-neutral stage watcher (ADR 0087 slice S2: observe, report, and file
closed decision requests that the runner, never the watcher, admits).

This is a library. The default worker service and CLI do not construct it; an operator opts in
with the worker's `--stage-watcher` flag (see `tessera_worker.stage_watch_runner`), or a caller
constructs it explicitly per attempt. A stage with no contract runs exactly as before.
"""

from __future__ import annotations

from .contract import ENVELOPE_FIELDS, StageContract
from .decisions import (
    DECISION_BODY_KEYS,
    DECISION_KINDS,
    DECISION_REASONS,
    ControlDecisionSink,
    DecisionEnvelope,
    DecisionRequest,
    DecisionSink,
    decide,
    decision_idempotency_key,
    decision_settles,
    watch_decisions_path,
)
from .killswitch import KillSwitch
from .outbox import ABANDON_AFTER, OutboxGaps, WatchOutbox
from .probe import ComputeProbe, DockerInspectProbe, ProbeSample, RunCommand
from .report import (
    BODY_KEYS,
    COUNTER_KEYS,
    DELIVERED,
    REASON_REFUSAL_STATUSES,
    REPORT_KINDS,
    ROUTINE_REFUSAL_STATUSES,
    TERMINAL_REFUSAL_STATUSES,
    TRANSIENT,
    ControlSink,
    DeliveryResult,
    ReportSink,
    WatchExit,
    WatchReport,
    WatchReportSink,
    classify_status,
    idempotency_key,
    reason_settles,
    refusal_statuses,
    settles,
    watch_reports_path,
)
from .signals import (
    MAX_SNAPSHOT_BYTES,
    SIGNAL_KEYS,
    SIGNALS_SCHEMA,
    SignalProbe,
    SnapshotFileSignals,
    validate_signals,
    write_signal_snapshot,
)
from .watcher import UNKNOWN_TICKS_BEFORE_FAILED, StageWatcher, routine_cap

__all__ = [
    "ABANDON_AFTER",
    "BODY_KEYS",
    "COUNTER_KEYS",
    "DECISION_BODY_KEYS",
    "DECISION_KINDS",
    "DECISION_REASONS",
    "DELIVERED",
    "ENVELOPE_FIELDS",
    "MAX_SNAPSHOT_BYTES",
    "REASON_REFUSAL_STATUSES",
    "REPORT_KINDS",
    "ROUTINE_REFUSAL_STATUSES",
    "SIGNALS_SCHEMA",
    "SIGNAL_KEYS",
    "TERMINAL_REFUSAL_STATUSES",
    "TRANSIENT",
    "UNKNOWN_TICKS_BEFORE_FAILED",
    "ComputeProbe",
    "ControlDecisionSink",
    "ControlSink",
    "DecisionEnvelope",
    "DecisionRequest",
    "DecisionSink",
    "DeliveryResult",
    "DockerInspectProbe",
    "KillSwitch",
    "OutboxGaps",
    "ProbeSample",
    "ReportSink",
    "RunCommand",
    "SignalProbe",
    "SnapshotFileSignals",
    "StageContract",
    "StageWatcher",
    "WatchOutbox",
    "WatchExit",
    "WatchReport",
    "WatchReportSink",
    "classify_status",
    "decide",
    "decision_idempotency_key",
    "decision_settles",
    "idempotency_key",
    "reason_settles",
    "refusal_statuses",
    "routine_cap",
    "settles",
    "validate_signals",
    "watch_decisions_path",
    "watch_reports_path",
    "write_signal_snapshot",
]
