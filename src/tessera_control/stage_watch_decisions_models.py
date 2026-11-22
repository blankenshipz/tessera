"""Closed-schema body of a worker's watch decision request (ADR 0087 section 4, ADR 0111).

A decision request is what the stage watcher would ask the runner to consider: a retry or a
resize. It is an advisory record only; nothing here or on the route performs or admits it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .egress_models import Identifier, Sha256
from .execution_models import ExecutionRequest, ReportExecutionObservation
from .stage_watch_limits import MAX_DECISION_SEQ

DECISION_KINDS = ("retry_request", "resize_request")
DECISION_REASONS = ("transient_exit", "oom_killed", "resource_limit", "probe_lost")
MAX_REQUESTED_MEMORY_BYTES = 2**40


class RecordWatchDecision(ExecutionRequest):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=0)]
    kind: Literal["retry_request", "resize_request"]
    reason: Literal["transient_exit", "oom_killed", "resource_limit", "probe_lost"]
    observed_at: datetime
    decision_seq: Annotated[int, Field(strict=True, ge=0, le=MAX_DECISION_SEQ)]
    # Required for, and only for, a resize request.
    requested_memory_bytes: (
        Annotated[int, Field(strict=True, ge=1, le=MAX_REQUESTED_MEMORY_BYTES)] | None
    ) = None
    contract_digest: Sha256

    @field_validator("observed_at", mode="before")
    @classmethod
    def observed_at_has_offset(cls, value: object) -> datetime:
        return ReportExecutionObservation.observed_at_has_offset(value)

    @model_validator(mode="after")
    def memory_only_for_resize(self) -> RecordWatchDecision:
        if (self.kind == "resize_request") != (self.requested_memory_bytes is not None):
            raise ValueError("requested_memory_bytes is required for, and only for, a resize")
        return self
