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
    model_serializer,
    model_validator,
)

from .agent_sandbox_models import SandboxPlan
from .artifact_limits import ABSOLUTE_MAX_ARTIFACT_BYTES, operator_limits
from .egress_models import EgressPolicy
from .request_limits import MAX_TOKEN_CHARS
from .token_purpose import provider_key_shaped


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


class DockerResources(StrictRequest):
    cpu_millicores: int = Field(strict=True, ge=100, le=2000)
    memory_bytes: int = Field(strict=True, ge=67108864, le=536870912)
    pids_limit: int = Field(strict=True, ge=16, le=128)
    scratch_bytes: int = Field(strict=True, ge=1048576, le=16777216)


class DockerPlan(DockerResources):
    target_id: NonBlank200
    target_digest: Sha256Digest


class ExecutionSpec(StrictRequest):
    argv: Annotated[list[RawString4000], Field(min_length=1, max_length=128)]
    input_snapshot_digest: Sha256Digest
    pinned_skill_digests: Annotated[list[Sha256Digest], Field(max_length=128)]
    output_paths: Annotated[list[RawString4000], Field(max_length=128)]
    profile: Literal["local-cpu", "kubernetes-opencode", "ordinary-docker", "agent-sandbox"]
    docker: DockerPlan | None = None
    sandbox: SandboxPlan | None = None
    egress: EgressPolicy | None = None
    network_enabled: Literal[False] = False
    # Outputs above 16 MiB use chunked large-artifact custody; the operator's
    # ArtifactLimits (default 8 GiB) bound what a package may actually request.
    max_output_bytes: Annotated[int, Field(strict=True, ge=1, le=ABSOLUTE_MAX_ARTIFACT_BYTES)]
    # The scratch a local-cpu run asks for (ADR 0059): within the worker operator's ceiling
    # it is granted, above it the run is refused before any container starts. Part of the
    # approved plan; absent keeps the operator's default and every existing plan digest.
    max_scratch_bytes: (
        Annotated[int, Field(strict=True, ge=1024 * 1024, le=64 * 1024**3)] | None
    ) = None

    @model_serializer(mode="wrap")
    def preserve_legacy_profile_shape(self, handler):
        value = handler(self)
        if self.max_scratch_bytes is None:
            value.pop("max_scratch_bytes", None)
        if self.sandbox is None:
            value.pop("sandbox", None)
        if self.egress is None:
            value.pop("egress", None)
        return value

    @model_validator(mode="after")
    def docker_is_explicit(self):
        if self.max_scratch_bytes is not None and self.profile != "local-cpu":
            raise ValueError("max_scratch_bytes is supported only for the local-cpu profile")
        if self.egress is not None and self.profile not in {"local-cpu", "agent-sandbox"}:
            raise ValueError(
                "Egress is supported only for the local-cpu and agent-sandbox profiles (ADR 0034)"
            )
        if (self.profile == "ordinary-docker") != (self.docker is not None):
            raise ValueError("Docker profile requires its explicit approved target/resource pins")
        if (self.profile == "agent-sandbox") != (self.sandbox is not None):
            raise ValueError("Sandbox profile requires explicit approved target/resource pins")
        return self

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


def within_operator_limit(spec: ExecutionSpec) -> ExecutionSpec:
    """Refuse a NEW plan whose output bound exceeds the operator's artifact limit.

    Only plan intake applies the operator limit. A stored, approved spec is validated against
    the absolute ceiling alone, so lowering the limit later never makes a running lease's
    uploads fail as an "invalid plan".
    """
    if spec.max_output_bytes > operator_limits().max_artifact_bytes:
        raise ValueError("max_output_bytes exceeds the operator's artifact size limit")
    return spec


class ReviseTaskPlan(StrictRequest):
    expected_revision: StrictRevision
    title: NonBlank200
    purpose: NonBlank4000
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration
    execution_spec: ExecutionSpec

    @field_validator("execution_spec")
    @classmethod
    def _within_operator_limit(cls, spec: ExecutionSpec) -> ExecutionSpec:
        return within_operator_limit(spec)


class ApproveTask(StrictRequest):
    expected_revision: StrictRevision
    expected_plan_digest: Sha256Digest | None = None


class ExternalAgentScope(StrictRequest):
    actions: Annotated[
        list[
            Literal[
                "context:read",
                "proposal:create",
                "proposal:read",
                "authority:request",
                "authority:consume",
                "observation:create",
                "review:request",
                "notebook:read",
                "provenance:append",
                "results:read",
                "budget:read",
                "campaign:submit",
                "campaign:read",
                "campaign:cancel",
                "provider_trust:read",
                "snapshot:read",
                "artifact:read",
                "campaign:logs",
            ]
        ],
        Field(min_length=1, max_length=18),
    ]
    destination: NonBlank200

    @field_validator("actions")
    @classmethod
    def unique_actions(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("actions must be unique")
        return value


class RenewWorkerCredential(StrictRequest):
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def offset_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at requires an offset")
        return value


class CreateCredential(StrictRequest):
    token: SecretStr
    kind: Literal["human_session", "agent", "worker"]
    audience: Literal["control", "worker"]
    project_id: str | None = None
    expires_at: datetime
    external_agent: ExternalAgentScope | None = None

    @field_validator("external_agent")
    @classmethod
    def external_scope_is_agent(cls, value: ExternalAgentScope | None, info: ValidationInfo):
        if value is not None and info.data.get("kind") != "agent":
            raise ValueError("external scopes require an agent credential")
        return value

    @field_validator("token")
    @classmethod
    def token_is_urlsafe_and_long(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value()
        if provider_key_shaped(value):
            raise ValueError("Provider keys are not Tessera authentication credentials")
        if not 43 <= len(value) <= MAX_TOKEN_CHARS or any(
            character not in URLSAFE_TOKEN_ALPHABET for character in value
        ):
            raise ValueError(f"token must be 43–{MAX_TOKEN_CHARS} URL-safe characters")
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
