"""Bounded plans and secret-free projections; no executable command fields."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .capability_models import (
    AdmitCapability,
    Digest,
    Identifier,
    InstalledPackage,
    Positive,
    Strict,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
State = Literal[
    "proposed",
    "ready",
    "running",
    "checkpointed",
    "suspended",
    "cancelled",
    "unknown",
    "completed",
    "failed",
    "expired",
]


class SandboxLimits(Strict):
    max_seconds: Annotated[int, Field(ge=1, le=900)] = 300
    cpu_millicores: Annotated[int, Field(ge=1, le=1000)] = 250
    memory_bytes: Annotated[int, Field(ge=1024 * 1024, le=1024**3)] = 128 * 1024**2
    pids: Annotated[int, Field(ge=1, le=64)] = 32
    storage_bytes: Annotated[int, Field(ge=1, le=16 * 1024**2)] = 1024**2
    output_bytes: Annotated[int, Field(ge=1, le=16 * 1024**2)] = 1024**2
    inodes: Annotated[int, Field(ge=1, le=1024)] = 128
    max_actions: Annotated[int, Field(ge=1, le=8)] = 8
    lease_seconds: Literal[5] = 5
    spend_cents: Literal[0] = 0


class CapabilityRequest(Strict):
    policy_id: Identifier
    declaration_digest: Digest
    escalation: Annotated[
        list[
            Literal[
                "new_domain",
                "credentials",
                "private_source",
                "paid_source",
                "persistent_write",
                "external_effect",
                "scientific_execution",
                "publication_decision",
            ]
        ],
        Field(max_length=8),
    ] = []


class AcquisitionPlan(Strict):
    action_id: Identifier
    kind: Literal["data", "model", "package"]
    purpose: Text
    provenance: Text
    access: Literal["public", "private", "paid", "unknown"] = "unknown"


class AutonomyPlan(Strict):
    task_plan_revision: Positive
    task_plan_digest: Digest
    capability: CapabilityRequest
    profile: Literal["observation-only", "public-prep", "experiment-ready"] = "observation-only"
    objective: Text
    environment_plan: Text
    acquisitions: Annotated[list[AcquisitionPlan], Field(max_length=8)] = []
    success_criteria: Text
    stop_conditions: Text
    uncertainty: Text
    # Configuration only takes effect after exact human review, not agent submission.
    public_auto: bool = False
    tools: Annotated[list[Literal["context_read", "capability_prepare"]], Field(max_length=2)] = [
        "context_read"
    ]
    workspace: Literal["disposable-capability-workspace"] = "disposable-capability-workspace"
    limits: SandboxLimits = SandboxLimits()
    control_surface: Literal["desktop_control_only"] = "desktop_control_only"
    # A proposal only: no target choice can activate preparation or experiment effects.
    proposed_execution_target: (
        Literal["kubernetes", "salad_container", "salad_inference"] | None
    ) = None

    @model_validator(mode="after")
    def unique_actions(self):
        if len({a.action_id for a in self.acquisitions}) != len(self.acquisitions):
            raise ValueError("duplicate acquisition")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("duplicate tool")
        return self


class Checkpoint(Strict):
    reason: Literal["review", "blocked", "progress", "consequential", "no_progress"]
    question: Text
    action_id: Identifier | None = None


class SessionCommand(Strict):
    expected_revision: Positive
    command: Literal[
        "approve",
        "reject",
        "revise",
        "checkpoint",
        "resume",
        "suspend",
        "cancel",
        "complete",
        "unknown",
    ]
    plan_digest: Digest | None = None
    plan: AutonomyPlan | None = None
    checkpoint: Checkpoint | None = None

    @model_validator(mode="after")
    def exact_command(self):
        if (self.command == "revise") != (self.plan is not None):
            raise ValueError("revise requires exactly one complete plan")
        if (self.command == "checkpoint") != (self.checkpoint is not None):
            raise ValueError("checkpoint requires a checkpoint")
        if (self.command == "approve") != (self.plan_digest is not None):
            raise ValueError("approval requires the displayed plan digest")
        return self


class StartAction(Strict):
    expected_revision: Positive
    operation_id: Identifier
    package_digest: Digest


class ActionUpdate(Strict):
    expected_revision: Positive
    operation_id: Identifier
    nonce: Digest


class AutonomyReceipt(Strict):
    bundle_digest: Digest
    artifact_sha256: Digest
    request_digest: Digest
    response_digest: Digest | None
    inventory_digest: Digest | None
    installed_packages: Annotated[list[InstalledPackage], Field(max_length=8)]
    bytes_retained: Annotated[int, Field(ge=0, le=16 * 1024**2)]
    elapsed_ms: Annotated[int, Field(ge=0)]
    exit_code: int | None
    stdout_sha256: Digest | None = None
    stderr_sha256: Digest | None = None
    streams: Literal["not_captured"] = "not_captured"
    state: str
    termination_confirmed: bool
    evidence: Literal["synthetic"] = "synthetic"
    custody: Literal["quarantined_unreleased"] = "quarantined_unreleased"
    scientific_validity: Literal["not_assessed"] = "not_assessed"
    cost: None = None


class AutonomyAction(Strict):
    autonomy_plan_digest: Digest
    task_plan_digest: Digest
    workspace_id: Digest
    operation_id: Identifier
    package_digest: Digest
    nonce: Digest
    worker_id: Identifier
    binding: AdmitCapability
    decision: Literal["auto_approved", "human_checkpoint"]
    receipt: AutonomyReceipt | None = None


class AutonomySession(Strict):
    id: Identifier
    org_id: Identifier
    project_id: Identifier
    task_id: Identifier
    revision: Positive
    plan: AutonomyPlan
    plan_digest: Digest
    workspace_id: Digest
    state: State
    approved_by: Identifier | None = None
    deadline: str | None = None
    lease_until: str | None = None
    checkpoint: Checkpoint | None = None
    permitted_action: Identifier | None = None
    cancel_requested: bool = False
    stop_reason: (
        Literal[
            "cancel_requested",
            "agent_unknown",
            "rejected",
            "criteria_reported",
            "receipt_failed",
            "effect_unknown",
        ]
        | None
    ) = None
    actions: Annotated[list[AutonomyAction], Field(max_length=8)] = []
