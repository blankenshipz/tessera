"""Strict, metadata-only commands for a confirmed local workspace handoff."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Revision = Annotated[int, Field(strict=True, ge=1)]


class RequestWorkspaceHandoff(HandoffRequest):
    task_id: Identifier
    grant_id: Identifier
    expected_task_revision: Revision
    expected_grant_revision: Revision
    baseline_snapshot_id: Identifier
    expected_baseline_digest: Digest
    workspace_label: Label


class ConfirmWorkspaceStop(HandoffRequest):
    expected_revision: Revision


class HandBackWorkspace(HandoffRequest):
    expected_revision: Revision
    expected_baseline_digest: Digest
    result_snapshot_id: Identifier
    expected_result_digest: Digest
