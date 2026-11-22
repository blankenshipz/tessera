"""Off-by-default, provider-neutral stage watcher (ADR 0087 slice S2, observe and report only).

This is a library. Nothing in the default worker service or CLI constructs it; a caller must
opt in explicitly per attempt. A stage with no contract runs exactly as before.
"""

from __future__ import annotations

from .contract import StageContract
from .killswitch import KillSwitch
from .outbox import ABANDON_AFTER, OutboxGaps, WatchOutbox
from .probe import ComputeProbe, DockerInspectProbe, ProbeSample, RunCommand
from .report import (
    BODY_KEYS,
    COUNTER_KEYS,
    DELIVERED,
    REPORT_KINDS,
    ROUTINE_REFUSAL_STATUSES,
    TERMINAL_REFUSAL_STATUSES,
    TRANSIENT,
    ControlSink,
    DeliveryResult,
    ReportSink,
    WatchReport,
    WatchReportSink,
    classify_status,
    idempotency_key,
    refusal_statuses,
    watch_reports_path,
)
from .watcher import UNKNOWN_TICKS_BEFORE_FAILED, StageWatcher, routine_cap

__all__ = [
    "ABANDON_AFTER",
    "BODY_KEYS",
    "COUNTER_KEYS",
    "DELIVERED",
    "REPORT_KINDS",
    "ROUTINE_REFUSAL_STATUSES",
    "TERMINAL_REFUSAL_STATUSES",
    "TRANSIENT",
    "UNKNOWN_TICKS_BEFORE_FAILED",
    "ComputeProbe",
    "ControlSink",
    "DeliveryResult",
    "DockerInspectProbe",
    "KillSwitch",
    "OutboxGaps",
    "ProbeSample",
    "ReportSink",
    "RunCommand",
    "StageContract",
    "StageWatcher",
    "WatchOutbox",
    "WatchReport",
    "WatchReportSink",
    "classify_status",
    "idempotency_key",
    "refusal_statuses",
    "routine_cap",
    "watch_reports_path",
]
