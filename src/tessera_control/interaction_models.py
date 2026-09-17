"""Strict request models for attributed interaction and human decisions."""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class InteractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


ProjectId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Intent = Literal["question", "direction", "status", "evidence"]
DecisionKind = Literal["direction", "spending", "access", "publication"]
Scope = Annotated[dict[str, Any], Field(max_length=32)]


class CreateThread(InteractionRequest):
    project_id: ProjectId
    task_id: str | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CreateMessage(InteractionRequest):
    thread_id: str
    intent: Intent
    body: Body
    reply_to: str | None = None


class EnqueueHarnessDelivery(InteractionRequest):
    """A human-directed, context-only message for an already admitted attempt."""

    thread_id: Identifier
    harness_session_binding_id: Identifier
    context_revision: int = Field(ge=1)
    intent: Intent
    body: Body
    reply_to: Identifier | None = None


class CreateOperationHarnessSessionBinding(InteractionRequest):
    """One worker-designated context-only session for an admitted operation."""

    operation_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    harness_instance_id: Identifier
    upstream_session_id: Identifier
    adapter_profile: Literal["opencode-v1.18.30-context-only"]
    provider_disabled: Literal[True]


class ClaimHarnessDelivery(InteractionRequest):
    expected_epoch: int = Field(ge=1)


class AuthorizeHarnessDelivery(InteractionRequest):
    """A body-free, live control recheck before the worker contacts OpenCode."""

    expected_epoch: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    context_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AcknowledgeHarnessDelivery(InteractionRequest):
    expected_revision: int = Field(ge=1)
    context_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    upstream_message_id: Identifier
    upstream_readback_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReportHarnessDelivery(InteractionRequest):
    expected_revision: int = Field(ge=1)
    context_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    outcome: Literal["uncertain", "failed"]
    error_code: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.-]{1,100}$")]


class CancelHarnessDelivery(InteractionRequest):
    expected_revision: int = Field(ge=1)


class CreateDecision(InteractionRequest):
    project_id: ProjectId
    task_id: str | None = None
    kind: DecisionKind
    scope: Scope
    expected_task_revision: int | None = Field(default=None, ge=1)
    reviewer_principal_id: str | None = None
    expires_at: str
    priority: int = Field(default=50, ge=0, le=100)

    @field_validator("scope")
    @classmethod
    def scope_is_bounded_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        def visit(item: Any, depth: int = 0) -> None:
            if depth > 4:
                raise ValueError("scope JSON is too deep")
            if item is None or type(item) in {bool, int, float, str}:
                return
            if isinstance(item, list):
                if len(item) > 100:
                    raise ValueError("scope JSON arrays are too large")
                for child in item:
                    visit(child, depth + 1)
                return
            if isinstance(item, dict):
                if len(item) > 32 or any(type(key) is not str or len(key) > 200 for key in item):
                    raise ValueError("scope JSON object keys are invalid")
                for child in item.values():
                    visit(child, depth + 1)
                return
            raise ValueError("scope must contain JSON values")

        visit(value)
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("scope must be strict JSON") from error
        if len(encoded) > 8000:
            raise ValueError("scope JSON is too large")

        return value


class ResolveDecision(InteractionRequest):
    decision_id: str
    expected_revision: int = Field(ge=1)
    decision: Literal["approve", "reject"]
    rationale: Body | None = None
