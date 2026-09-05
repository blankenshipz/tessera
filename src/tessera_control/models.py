"""Small, strict request models for the control API."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


NonBlank200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NonBlank4000 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
StrictDuration = Annotated[int, Field(strict=True, ge=1, le=86_400)]
StrictRevision = Annotated[int, Field(strict=True, ge=1)]


class CreateProject(StrictRequest):
    name: NonBlank200


class CreateTask(StrictRequest):
    title: NonBlank200
    purpose: NonBlank4000
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration


class ApproveTask(StrictRequest):
    expected_revision: StrictRevision
