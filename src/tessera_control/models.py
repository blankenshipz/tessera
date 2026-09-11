"""Small, strict request models for the control API."""

from datetime import datetime
from string import ascii_letters, digits
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


NonBlank200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NonBlank4000 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
StrictDuration = Annotated[int, Field(strict=True, ge=1, le=86_400)]
StrictRevision = Annotated[int, Field(strict=True, ge=1)]
StrictBytes = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RawString4000 = Annotated[str, StringConstraints(max_length=4000)]
URLSAFE_TOKEN_ALPHABET = frozenset(ascii_letters + digits + "-_")


class CreateProject(StrictRequest):
    name: NonBlank200


class CreateTask(StrictRequest):
    title: NonBlank200
    purpose: NonBlank4000
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration


class ExecutionSpec(StrictRequest):
    argv: Annotated[list[RawString4000], Field(min_length=1, max_length=128)]
    input_snapshot_digest: Sha256Digest
    pinned_skill_digests: Annotated[list[Sha256Digest], Field(max_length=128)]
    output_paths: Annotated[list[RawString4000], Field(max_length=128)]
    profile: Literal["local-cpu", "kubernetes-opencode"]
    network_enabled: Literal[False] = False
    max_output_bytes: StrictBytes

    @field_validator("argv")
    @classmethod
    def argv_is_bounded_and_preserved(cls, argv: list[str]) -> list[str]:
        if not argv[0].strip():
            raise ValueError("argv must start with a nonblank command")
        if any("\x00" in argument for argument in argv):
            raise ValueError("argv must not contain NUL")
        return argv

    @field_validator("output_paths")
    @classmethod
    def output_paths_are_portable_and_relative(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if "\x00" in path or "\\" in path or (len(path) >= 2 and path[1] == ":"):
                raise ValueError("output paths must be portable relative paths")
            if any(part in {"", ".", ".."} for part in path.split("/")):
                raise ValueError("output paths must be nonempty relative paths")
        if len(set(paths)) != len(paths):
            raise ValueError("output paths must be unique")
        return paths

    @field_validator("pinned_skill_digests")
    @classmethod
    def pinned_skills_are_unique(cls, digests: list[str]) -> list[str]:
        if len(digests) != len(set(digests)):
            raise ValueError("pinned skill digests must be unique")
        return digests


class ReviseTaskPlan(StrictRequest):
    expected_revision: StrictRevision
    title: NonBlank200
    purpose: NonBlank4000
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration
    execution_spec: ExecutionSpec


class ApproveTask(StrictRequest):
    expected_revision: StrictRevision
    expected_plan_digest: Sha256Digest | None = None


class CreateCredential(StrictRequest):
    token: SecretStr
    kind: Literal["human_session", "agent", "worker"]
    audience: Literal["control", "worker"]
    project_id: str | None = None
    expires_at: datetime

    @field_validator("token")
    @classmethod
    def token_is_urlsafe_and_long(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value()
        if len(value) < 43 or any(character not in URLSAFE_TOKEN_ALPHABET for character in value):
            raise ValueError("token must be at least 43 URL-safe characters")
        return token

    @field_validator("expires_at")
    @classmethod
    def expiry_has_offset(cls, expires_at: datetime) -> datetime:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at requires an offset")
        return expires_at

    @field_validator("project_id")
    @classmethod
    def project_scope_is_present_for_nonhuman(
        cls, project_id: str | None, info: ValidationInfo
    ) -> str | None:
        kind = info.data.get("kind")
        if kind in {"agent", "worker"} and not project_id:
            raise ValueError("agent and worker credentials require project scope")
        return project_id

    @field_validator("audience")
    @classmethod
    def audience_matches_kind(cls, audience: str, info: ValidationInfo) -> str:
        kind = info.data.get("kind")
        if kind == "worker" and audience != "worker":
            raise ValueError("worker credentials require worker audience")
        if kind in {"human_session", "agent"} and audience != "control":
            raise ValueError("human and agent credentials require control audience")
        return audience
