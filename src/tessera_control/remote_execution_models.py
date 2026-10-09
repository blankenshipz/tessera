"""Remote routing declarations are additional restrictions, never execution grants."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from .capability_models import Digest, Identifier, Positive, Strict

Target = Literal["kubernetes", "salad_container", "salad_inference"]
Action = Literal["create", "start", "stop", "delete", "observe"]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
Image = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}$", max_length=512),
]


class Frozen(Strict):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Manifests(Frozen):
    image: Image
    code: Digest
    data: Digest
    model: Digest
    environment: Digest


class RemoteResources(Frozen):
    cpu_millicores: int = Field(ge=1, le=64000)
    memory_mb: int = Field(ge=128, le=262144)
    gpu_class: Name | None = None
    gpu_count: int = Field(ge=0, le=1)
    disk_mb: int = Field(ge=128, le=1048576)
    max_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def gpu(self):
        if bool(self.gpu_count) != (self.gpu_class is not None):
            raise ValueError("GPU count and exact class must agree")
        return self


class SaladExecution(Frozen):
    """Exact human-reviewed executable profile, not evidence of provider enforcement."""

    platform: Literal["linux/amd64"]
    command: Literal["/tessera/bin/run"]
    argv: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]],
        Field(max_length=16),
    ]
    env: dict[str, str]
    max_output_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    max_output_files: int = Field(ge=1, le=128)
    capability_digest: Digest
    network_policy: Literal["provider_broad_egress_explicitly_accepted"]
    data_policy: Literal["public_released_only"]
    artifact_destination: Annotated[
        str, StringConstraints(pattern=r"^artifact-ref:[a-z0-9-]{1,63}$")
    ]
    checkpoint_destination: Annotated[
        str, StringConstraints(pattern=r"^checkpoint-ref:[a-z0-9-]{1,63}$")
    ]
    monitor_profile: Digest
    watchdog_profile: Digest
    max_cost_cents: int = Field(ge=0, le=10000)
    credit_exposure: Literal["unknown_blocks_new_work_credit_not_hard_cap"]
    gateway: Literal[False] = False
    ssh: Literal[False] = False
    exec: Literal[False] = False

    @field_validator("env")
    @classmethod
    def closed_environment(cls, value):
        allowed = {"PYTHONUNBUFFERED": {"1"}, "OMP_NUM_THREADS": {"1", "2", "4"}}
        if any(k not in allowed or v not in allowed[k] for k, v in value.items()):
            raise ValueError("only explicit non-secret execution environment is supported")
        return value


class SaladProfile(Frozen):
    organization: Name
    project: Name
    group: Name
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    replicas: Literal[1] = 1
    artifact_custody: Literal["external_quarantine"] = "external_quarantine"
    checkpoints: Literal["external"] = "external"
    autoscaling: Literal[False] = False
    queues: Literal[False] = False
    interactive_shell: Literal[False] = False
    runtime_install: Literal[False] = False
    runtime_download: Literal[False] = False
    provider_trust: Literal["broad_egress_ephemeral_reallocation_credit_not_hard_cap"]
    # Legacy offline routes remain readable but cannot pass proxy admission.
    execution: SaladExecution | None = None


class ExecutionSelection(Frozen):
    target: Target
    target_digest: Digest
    resources: RemoteResources
    manifests: Manifests
    salad: SaladProfile | None = None
    desktop_role: Literal["control_only"] = "control_only"
    placement: Literal["remote_execution_plane"] = "remote_execution_plane"

    @field_validator("target", mode="before")
    @classmethod
    def remote_only(cls, value):
        if isinstance(value, str) and value in {
            "desktop",
            "local_process",
            "local_cpu",
            "local-cpu",
        }:
            raise ValueError("desktop/local process agent experiment execution is forbidden")
        return value

    @model_validator(mode="after")
    def profile(self):
        if (self.target == "salad_container") != (self.salad is not None):
            raise ValueError("only Salad containers require the narrow Salad profile")
        return self


class RemoteProposal(Frozen):
    operation_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    package_digest: Digest
    attempt_id: Identifier
    plan_digest: Digest
    lease_id: Identifier
    epoch: Positive
    selection: ExecutionSelection
    study_id: Identifier
    program_review_id: Identifier
    source_review_id: Identifier


class RemoteCommand(Frozen):
    expected_revision: Positive
    command: Literal["approve", "start", "pause", "cancel"]
    proposal_digest: Digest


class RemoteExecutionPackage(Frozen):
    version: Literal[1] = 1
    org_id: Identifier
    project_id: Identifier
    task_id: Identifier
    proposal: RemoteProposal
    mode: Literal["offline"] = "offline"
    capability_expansion: Literal[False] = False


class EvidenceLink(Frozen):
    kind: Literal["log", "event", "checkpoint", "artifact", "termination", "cost"]
    sha256: Digest
    # References are claims until independently matched to retained control custody.
    manifest_id: Identifier | None = None
    custody: Literal["unverified", "quarantined"] = "unverified"


class ProviderReadback(Frozen):
    provider_id: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9:/._-]{0,511}$")] | None
    ) = None
    allocation_id: Identifier | None = None
    state: Literal["stopped", "running", "succeeded", "failed", "deleted", "unknown"]
    observed_at: Annotated[str, StringConstraints(max_length=40)]
    request_id: Identifier | None = None
    response_status: int | None = Field(default=None, ge=100, le=599)
    termination_confirmed: bool = False
    cost_cents: int | None = Field(default=None, ge=0, le=2**53 - 1)
    evidence: Annotated[list[EvidenceLink], Field(max_length=32)] = []
    provenance: Literal["synthetic", "provider"] = "synthetic"
    proxy_reason: (
        Literal[
            "admission_denied",
            "credential_unavailable",
            "provider_unavailable",
            "malformed_response",
            "redirect_denied",
            "identity_mismatch",
            "reallocation_uncertain",
            "accepted_pending",
            "observed",
        ]
        | None
    ) = None
    usage_status: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def honest_termination(self):
        from .store import _parse_utc

        _parse_utc(self.observed_at)
        if self.state != "unknown" and self.provider_id is None:
            raise ValueError("non-unknown provider observations require exact provider identity")
        if self.termination_confirmed and (
            self.provider_id is None
            or self.state not in {"stopped", "succeeded", "failed", "deleted"}
            or self.response_status == 202
        ):
            raise ValueError("acknowledgement is not termination evidence")
        return self


class TargetPolicy(Frozen):
    """Operator-owned exact allowlist entry; never accepted from API request bodies."""

    selection: ExecutionSelection
    mode: Literal["offline"] = "offline"
    # No live target can be activated by changing a boolean in a review request.
    live_enabled: Literal[False] = False
