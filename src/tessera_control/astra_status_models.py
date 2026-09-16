# ruff: noqa: I001
"""Strict bounded commands for Astra's deterministic Explain status turn."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RequestExplainStatusTurn(_Request):
    expected_conversation_revision: int = Field(ge=0)
    prompt: Body
    experiment_id: Identifier
    expected_design_revision: int = Field(ge=1)


class ClaimExplainStatusTurn(_Request):
    expected_revision: int = Field(ge=1)


class BeginStatusToolCall(_Request):
    expected_revision: int = Field(ge=1)
    tool: Literal["read_experiment_status"]
    arguments: dict[Literal["identifier"], Identifier]


class StatusOperation(_Request):
    operation_id: Identifier
    project_id: Identifier
    task_id: Identifier
    desired_state: Literal["admitted", "cancel_requested"]
    observed_state: Literal["unknown", "running", "exited"]
    provider_id: str | None


class StatusToolResult(_Request):
    experiment_id: Identifier
    project_id: Identifier
    current_design_revision: int = Field(ge=1)
    operation_count: int = Field(ge=0, le=1_000)
    operations: list[StatusOperation] = Field(max_length=100)
    operations_truncated: bool
    scientific_validity: Literal["not_assessed"] = "not_assessed"


class RecordStatusToolResult(_Request):
    expected_revision: int = Field(ge=1)
    result: StatusToolResult


class SettleExplainStatusTurn(_Request):
    expected_revision: int = Field(ge=1)
