"""Closed-schema body of a worker's stage watch report (ADR 0087 sections 3 and 7).

A stage watch report is an operational, unverified worker assertion about a running
stage: a heartbeat, progress, a stuck or a failed signal with a few bounded counters.
It never carries free text, so nothing a stage prints can reach the control plane.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .egress_models import Identifier, Sha256
from .execution_models import ExecutionRequest, ReportExecutionObservation

WATCH_KINDS = ("heartbeat", "progress", "stuck", "failed")
MAX_WATCH_SEQ = 1_000_000
Counter = Annotated[int, Field(strict=True, ge=0, le=2**53)]


class WatchCounters(ExecutionRequest):
    """Optional bounded integer counters; no other keys and no strings are accepted."""

    elapsed_s: Counter | None = None
    silent_s: Counter | None = None
    mem_bytes: Counter | None = None
    cpu_ms: Counter | None = None
    output_bytes: Counter | None = None
    restarts: Counter | None = None
    # The worker's own count of its reports control refused with a definite 4xx, and of
    # reports it dropped (pacing, budget or abandonment), so its gaps reach the receipt.
    refused_reports: Counter | None = None
    dropped_reports: Counter | None = None


class ReportStageWatch(ExecutionRequest):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=0)]
    kind: Literal["heartbeat", "progress", "stuck", "failed"]
    observed_at: datetime
    seq: Annotated[int, Field(strict=True, ge=0, le=MAX_WATCH_SEQ)]
    counters: WatchCounters
    kill_switch_engaged: bool
    contract_digest: Sha256

    @field_validator("observed_at", mode="before")
    @classmethod
    def observed_at_has_offset(cls, value: object) -> datetime:
        return ReportExecutionObservation.observed_at_has_offset(value)
