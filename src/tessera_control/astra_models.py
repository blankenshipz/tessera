"""Strict bounded commands for Astra's provider-disabled coordinator slice."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .experiments_models import CreateExperiment, Digest


class AstraRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]
ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.-]{1,100}$")]


class SelectedContextReference(AstraRequest):
    """A revision-fenced record the deterministic coordinator may read."""

    kind: Literal["task", "experiment"]
    record_id: Identifier
    expected_revision: int = Field(ge=1)


class RequestCoordinatorTurn(AstraRequest):
    expected_conversation_revision: int = Field(ge=0)
    prompt: Body
    selected_context: Annotated[list[SelectedContextReference], Field(max_length=16)] = []

    @field_validator("selected_context")
    @classmethod
    def unique_context_records(
        cls, value: list[SelectedContextReference]
    ) -> list[SelectedContextReference]:
        keys = [(item.kind, item.record_id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("selected context records must be unique")
        return value


class ClaimCoordinatorTurn(AstraRequest):
    expected_revision: int = Field(ge=1)


class ConsumerHeartbeat(AstraRequest):
    """An ordinary coordinator's bounded, provider-disabled availability beat."""

    pass


class CompleteCoordinatorTurn(AstraRequest):
    expected_revision: int = Field(ge=1)
    status: Annotated[list[Body], Field(min_length=1, max_length=20)]
    recap: Body
    proposal: Body
    proposal_type: Literal["human_review_needed"]
    # A coordinator may attach one complete, still non-authoritative design
    # candidate.  It is registered only by the separate human command below.
    design: CreateExperiment | None = None
    provider_disabled: Literal[True]
    unsupported_actions: Annotated[
        list[Literal["approval", "grant", "dispatch", "tool", "shell", "skill_loading"]],
        Field(min_length=6, max_length=6),
    ]

    @field_validator("unsupported_actions")
    @classmethod
    def complete_unsupported_set(cls, value: list[str]) -> list[str]:
        expected = {"approval", "grant", "dispatch", "tool", "shell", "skill_loading"}
        if set(value) != expected or len(value) != len(expected):
            raise ValueError("unsupported_actions must disclose every unavailable authority")
        return value

    @model_validator(mode="after")
    def completion_message_is_bounded(self) -> CompleteCoordinatorTurn:
        completion = (
            f"Status: {' | '.join(self.status)}\nRecap: {self.recap}\n"
            f"Proposal: {self.proposal}\n"
            "Unsupported: approval, grant, dispatch, tool, shell, skill_loading."
        )
        if len(completion) > 4_000:
            raise ValueError("coordinator completion exceeds the conversation message bound")
        return self


class FailCoordinatorTurn(AstraRequest):
    expected_revision: int = Field(ge=1)
    error_code: ErrorCode


class CancelCoordinatorTurn(AstraRequest):
    expected_revision: int = Field(ge=1)


class RegisterProposalDesign(AstraRequest):
    """Fence an explicit human registration to the reviewed context snapshot."""

    expected_context_digest: Digest
