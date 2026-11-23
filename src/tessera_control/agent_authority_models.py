"""Closed, resource-pinned human authority and external-agent command contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Discriminator, Field, StringConstraints, Tag, field_validator, model_validator

from .models import (
    NonBlank200,
    NonBlank4000,
    Sha256Digest,
    StrictDuration,
    StrictNonNegativeInt,
    StrictRequest,
    StrictRevision,
)

AuthorityAction = Literal[
    "attempt:admit", "attempt:dispatch", "attempt:status", "execution:cancel", "authority:delegate"
]


class AgentAuthorityBinding(StrictRequest):
    variant_id: NonBlank200
    expected_variant_revision: StrictRevision
    expected_design_revision: StrictRevision
    execution_grant_id: NonBlank200
    expected_execution_grant_revision: StrictRevision
    expected_execution_epoch: StrictRevision
    task_id: NonBlank200
    expected_task_revision: StrictRevision
    plan_digest: Sha256Digest
    target_digest: Sha256Digest


class AgentAuthorityBounds(StrictRequest):
    """Execution authority bounds. ``kind`` is implicit: a stored row or client body without a
    kind is execution, and it is never serialized, so existing digests and rows are unchanged."""

    kind: Annotated[Literal["execution"], Field(exclude=True)] = "execution"
    subject_credential_id: NonBlank200
    destination: NonBlank200
    model: NonBlank200
    purpose: NonBlank4000
    actions: Annotated[list[AuthorityAction], Field(min_length=1, max_length=5)]
    bindings: Annotated[list[AgentAuthorityBinding], Field(min_length=1, max_length=64)]
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration
    expires_at: datetime
    delegation_depth: Annotated[int, Field(strict=True, ge=0, le=8)] = 0

    @field_validator("expires_at")
    @classmethod
    def offset_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("expiry requires an offset")
        return value

    @model_validator(mode="after")
    def unique_resources(self):
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("actions must be unique")
        if len({b.variant_id for b in self.bindings}) != len(self.bindings):
            raise ValueError("variants must be unique")
        if len({b.execution_grant_id for b in self.bindings}) != len(self.bindings):
            raise ValueError("execution grants must be unique")
        return self


REASONING_KIND = "reasoning"
MODEL_CALL = "model:call"
ProviderId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class ReasoningAuthorityBounds(StrictRequest):
    """Reasoning-only authority: pays for model-runtime calls to one provider and model and
    nothing else. It carries no execution binding, cannot be consumed for any execution action,
    delegated, or parent a runtime binding, and spends only within the project's model-spend
    limits and its own budget."""

    kind: Literal["reasoning"]
    subject_credential_id: NonBlank200
    destination: NonBlank200
    model: NonBlank200
    purpose: NonBlank4000
    provider_id: ProviderId
    actions: Annotated[list[Literal["model:call"]], Field(min_length=1, max_length=1)]
    bindings: Annotated[list[AgentAuthorityBinding], Field(max_length=0)] = []
    budget_cents: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    max_duration_seconds: StrictDuration
    expires_at: datetime
    delegation_depth: Literal[0] = 0

    @field_validator("expires_at")
    @classmethod
    def offset_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("expiry requires an offset")
        return value


def authority_kind(bounds) -> str:
    """The grant kind of stored or submitted bounds; a missing kind is execution."""
    if isinstance(bounds, dict):
        return bounds.get("kind", "execution")
    return getattr(bounds, "kind", "execution")


AnyAuthorityBounds = Annotated[
    Annotated[AgentAuthorityBounds, Tag("execution")]
    | Annotated[ReasoningAuthorityBounds, Tag("reasoning")],
    Discriminator(authority_kind),
]


class RequestAgentAuthority(StrictRequest):
    bounds: AnyAuthorityBounds


class IssueAgentAuthority(RequestAgentAuthority):
    request_id: NonBlank200 | None = None


class AuthorityFence(StrictRequest):
    expected_revision: StrictRevision
    expected_epoch: StrictRevision


class RevokeAgentAuthority(AuthorityFence):
    pass


class DelegateAgentAuthority(AuthorityFence):
    bounds: AnyAuthorityBounds


class AdmitAgentAttempt(StrictRequest):
    action: Literal["attempt:admit"]
    variant_id: NonBlank200
    expected_variant_revision: StrictRevision
    expected_design_revision: StrictRevision
    execution_grant_id: NonBlank200


class DispatchAgentAttempt(StrictRequest):
    action: Literal["attempt:dispatch"]
    attempt_id: NonBlank200
    expected_revision: StrictRevision
    target_digest: Sha256Digest
    profile_version: Literal["development-minikube-opencode-v1", "ordinary-docker-v1"]


class StatusAgentAttempt(StrictRequest):
    action: Literal["attempt:status"]
    attempt_id: NonBlank200


class CancelAgentExecution(StrictRequest):
    action: Literal["execution:cancel"]
    execution_grant_id: NonBlank200
    expected_revision: StrictRevision


class ConsumeAgentAuthority(AuthorityFence):
    model: NonBlank200
    command: Annotated[
        AdmitAgentAttempt | DispatchAgentAttempt | StatusAgentAttempt | CancelAgentExecution,
        Field(discriminator="action"),
    ]
