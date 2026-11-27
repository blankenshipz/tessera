"""Remote routing declarations are additional restrictions, never execution grants."""

import re
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from .capability_models import Digest, Identifier, Positive, Strict
from .execution_targets import Target
from .salad_workload_contract import WorkloadContractV1

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
    max_seconds: int = Field(ge=1, le=7200)

    @model_validator(mode="after")
    def gpu(self):
        if bool(self.gpu_count) != (self.gpu_class is not None):
            raise ValueError("GPU count and exact class must agree")
        return self


# Salad routes bill only running time, but the guardian's route deadline is wall-clock from
# arming: allocation, image pull and input fetch all spend it before the workload's own
# clock starts. A stage's route window must therefore cover the declared run PLUS this
# provisioning allowance; the guardian never shortens the run to fit.
#
# Allocation: the guardian's default allocation deadline (a group with no machine after this
# long is cancelled, ``salad_guardian.DEFAULT_ALLOCATION_SECONDS``).
SALAD_ALLOCATION_ALLOWANCE_SECONDS = 600
# Image pull + input fetch after a machine is assigned: Salad documents that bringing
# containers up "can occasionally take 20 minutes or more" (container-group deployment
# lifecycle); the allowance is 15 minutes of that after the allocation share above.
SALAD_IMAGE_PULL_INPUT_ALLOWANCE_SECONDS = 900
SALAD_PROVISIONING_ALLOWANCE_SECONDS = (
    SALAD_ALLOCATION_ALLOWANCE_SECONDS + SALAD_IMAGE_PULL_INPUT_ALLOWANCE_SECONDS
)
# Schema headroom: the largest route window a plan may carry beyond its own duration. The
# profile's ``route_max_seconds`` (itself at most 7200) is the operative ceiling.
SALAD_ROUTE_WINDOW_HEADROOM_SECONDS = 3600


def route_window_limit(target, plan_max_duration_seconds):
    """Largest route window a plan admits for this target."""
    if target == "salad_container":
        return plan_max_duration_seconds + SALAD_ROUTE_WINDOW_HEADROOM_SECONDS
    return plan_max_duration_seconds


def salad_route_need_seconds(declared_run_seconds):
    """Route window one Salad stage needs: declared run plus the provisioning allowance."""
    return declared_run_seconds + SALAD_PROVISIONING_ALLOWANCE_SECONDS


def salad_route_window_refusal(declared_run_seconds, ceiling_seconds, label="route_max_seconds"):
    """Reason text when the declared run cannot fit its route window ceiling, else None."""
    need = salad_route_need_seconds(declared_run_seconds)
    if need <= ceiling_seconds:
        return None
    return (
        f"declared run {declared_run_seconds}s plus the {SALAD_PROVISIONING_ALLOWANCE_SECONDS}s "
        f"provisioning allowance (allocation {SALAD_ALLOCATION_ALLOWANCE_SECONDS}s + image pull "
        f"and input fetch {SALAD_IMAGE_PULL_INPUT_ALLOWANCE_SECONDS}s) needs a {need}s route "
        f"window, above the {ceiling_seconds}s ceiling ({label}); shorten the run or have the "
        "operator raise the profile's route_max_seconds (at most 7200)"
    )


def salad_route_window_description(window_seconds, declared_run_seconds, profile_ceiling):
    """Plan/review text: effective window, its source and the allowance."""
    source = (
        f"profile route_max_seconds {profile_ceiling}"
        if window_seconds == profile_ceiling
        else f"plan bound (declared run {declared_run_seconds}s "
        f"+ {SALAD_ROUTE_WINDOW_HEADROOM_SECONDS}s)"
    )
    return (
        f"route window {window_seconds}s (source: {source}); provisioning allowance "
        f"{SALAD_PROVISIONING_ALLOWANCE_SECONDS}s; declared run {declared_run_seconds}s"
    )


_Rate = Annotated[str, StringConstraints(pattern=r"^(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,6})?$")]


class CpuRate(Frozen):
    """Operator-configured CPU-only Salad rates (USD per hour), reviewed with the route.

    A CPU-only container has no published GPU class rate to read, so the estimate comes from
    these two operator rates. They are an estimate input, never invoice or cost proof.
    """

    vcpu_hour_usd: _Rate
    memory_gb_hour_usd: _Rate

    @model_validator(mode="after")
    def positive(self):
        if not (float(self.vcpu_hour_usd) > 0 and float(self.memory_gb_hour_usd) >= 0):
            raise ValueError("CPU rates must be positive (memory may be zero)")
        return self

    def hourly_usd(self, cpu_millicores: int, memory_mb: int):
        """Exact Decimal hourly rate of one container of this shape."""
        from decimal import Decimal

        return (
            Decimal(self.vcpu_hour_usd) * Decimal(cpu_millicores) / 1000
            + Decimal(self.memory_gb_hour_usd) * Decimal(memory_mb) / 1024
        )


class SaladExecution(Frozen):
    """Exact human-reviewed executable profile, not evidence of provider enforcement."""

    platform: Literal["linux/amd64"]
    command: Literal["/tessera/bin/run"]
    # exact/smoke_operation_v1 keep the original strict token shape (enforced below);
    # workload_v1 carries the resolved printable argv of the reviewed contract.
    argv: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=256)]],
        Field(max_length=32),
    ]
    argv_binding: Literal["exact", "smoke_operation_v1", "workload_v1"] = "exact"
    workload: WorkloadContractV1 | None = None
    env: dict[str, str]
    max_output_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    max_output_files: int = Field(ge=1, le=128)
    capability_digest: Digest
    network_policy: Literal["provider_broad_egress_explicitly_accepted"]
    data_policy: Literal["public_released_only", "provider_trust_policy_v1"]
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
    # Present only on a CPU-only route (no GPU class); absent keeps every GPU route's bytes and
    # digests unchanged.
    cpu_rate: CpuRate | None = None
    gateway: Literal[False] = False
    ssh: Literal[False] = False
    exec: Literal[False] = False

    @model_serializer(mode="wrap")
    def legacy_exact_shape(self, handler):
        data = handler(self)
        # Exact is the pre-existing contract, not new authority. Keep historical
        # proposal/package bytes and digests stable without migrating any ledger.
        # The new smoke binding must remain explicit in every serialized review.
        if self.argv_binding == "exact":
            data.pop("argv_binding", None)
        if self.workload is None:
            data.pop("workload", None)
        if self.cpu_rate is None:
            data.pop("cpu_rate", None)
        return data

    @model_validator(mode="after")
    def binding_shape(self):
        if (self.argv_binding == "workload_v1") != (self.workload is not None):
            raise ValueError("workload contract is required for, and only for, workload_v1")
        if self.workload is None:
            if len(self.argv) > 16 or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", token) for token in self.argv
            ):
                raise ValueError("argv tokens must match the reviewed strict shape")
            allowed = {"PYTHONUNBUFFERED": {"1"}, "OMP_NUM_THREADS": {"1", "2", "4"}}
            if any(k not in allowed or v not in allowed[k] for k, v in self.env.items()):
                raise ValueError("only explicit non-secret execution environment is supported")
            return self
        if (
            self.env != self.workload.env
            or self.max_output_files != len(self.workload.outputs)
            or self.max_output_bytes < self.workload.output_bytes
        ):
            raise ValueError("workload profile env/output limits differ from the contract")
        return self


def derived_salad_argv(execution, approved_argv, operation_id, epoch, max_seconds):
    """Resolve only the two reviewed smoke operation slots; never a template engine.

    The retained approved spec/package bytes are unchanged. Human route review
    still covers the concrete resolved selection, and every use re-derives it.
    """
    profile = SaladExecution.model_validate(execution)
    if profile.argv_binding == "workload_v1":
        work = profile.workload
        if (
            type(approved_argv) is not list
            or approved_argv != [profile.command, *work.argv_template]
            or type(operation_id) is not str
            or len(operation_id) != 32
            or any(c not in "0123456789abcdef" for c in operation_id)
            or type(epoch) is not int
            or epoch < 1
            or work.workload_seconds > max_seconds
        ):
            raise ValueError("unsupported Salad workload binding")
        argv = work.derived_argv(operation_id, epoch)[1:]
        if argv != profile.argv:
            raise ValueError("Salad workload binding differs from approved spec")
        return argv
    if profile.argv_binding == "exact":
        if approved_argv != [profile.command, *profile.argv]:
            raise ValueError("Salad argv differs from approved execution spec")
        return list(profile.argv)
    if (
        type(approved_argv) is not list
        or len(approved_argv) != 11
        or approved_argv[:3] != ["/tessera/bin/run", "v1", "public-integers-v1"]
        or approved_argv[3:5] != ["operation-id", "operation-epoch"]
        or type(operation_id) is not str
        or len(operation_id) != 32
        or any(c not in "0123456789abcdef" for c in operation_id)
        or type(epoch) is not int
        or epoch < 1
    ):
        raise ValueError("unsupported Salad smoke binding")
    argv = list(approved_argv[1:])
    argv[2:4] = [operation_id, str(epoch)]
    if (
        not argv[4]
        or len(argv[4]) > 64
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in argv[4])
        or argv[4].startswith((".", "-"))
        or argv[4].endswith((".", "-"))
        or argv[5] != profile.watchdog_profile
    ):
        raise ValueError("unsupported Salad smoke binding")
    try:
        seconds, byte_cap, file_cap, dimension = map(int, argv[6:])
    except (TypeError, ValueError):
        raise ValueError("unsupported Salad smoke binding") from None
    if (
        argv[6:] != list(map(str, (seconds, byte_cap, file_cap, dimension)))
        or not 1 <= seconds <= min(300, max_seconds)
        or not 256 <= byte_cap <= min(65536, profile.max_output_bytes)
        or file_cap != 1
        or file_cap > profile.max_output_files
        or not 2 <= dimension <= 32
        or profile.argv != argv
    ):
        raise ValueError("Salad smoke runtime binding differs from approved spec")
    return argv


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
        execution = self.salad.execution if self.salad else None
        if execution is not None and execution.cpu_rate is not None and self.resources.gpu_count:
            raise ValueError("CPU rates belong only to a CPU-only route")
        # (A CPU-only route without rates still parses, so a stored proposal keeps working for
        # stop and reconcile; admission and the transport require the rates.)
        work = execution.workload if execution else None
        if work is not None:
            # A declared input is the reviewed data snapshot of this route, never another.
            if work.input is not None and work.input.snapshot_digest != self.manifests.data:
                raise ValueError("workload input must be the route's manifests.data snapshot")
            # Bounds that may tighten later (disk_mb, staged input checks) are admission
            # checks (salad_workload_admission), never parse checks: stored proposals
            # must always parse so stop and reconcile keep working.
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


class ProviderRejection(Frozen):
    """Bounded, non-secret summary of a provider's clean 400/422 create rejection, or of a
    create that never left the process (status 0: no HTTP exchange)."""

    status: Literal[0, 400, 422]
    title: Annotated[str, StringConstraints(max_length=200)] | None = None
    detail: Annotated[str, StringConstraints(max_length=200)] | None = None
    error_fields: Annotated[
        list[Annotated[str, StringConstraints(max_length=128)]], Field(max_length=16)
    ] = []


class ProviderReadback(Frozen):
    provider_id: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9:/._-]{0,511}$")] | None
    ) = None
    allocation_id: Identifier | None = None
    # ``pending`` (image pull) and ``deploying`` (started, no instance running) are observations
    # only, never authority; a failed read stays ``unknown``.
    state: Literal[
        "stopped", "running", "succeeded", "failed", "deleted", "pending", "deploying", "unknown"
    ]
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
            "provider_rejected_request",
            "request_not_sent",
            "group_gone",
        ]
        | None
    ) = None
    # Closed diagnostics of a failed read (never a value, body, URL or path): the transport's
    # detail token (e.g. ``timeout``, ``non_2xx_5xx``), the NAME of an offending field, and
    # which provider request failed. Only ever set on an unknown readback.
    proxy_detail: Annotated[str | None, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = None
    proxy_field: Annotated[str | None, StringConstraints(pattern=r"^[a-z][a-z_.\-]{0,47}$")] = None
    proxy_request_kind: (
        Literal[
            "list",
            "quotas",
            "gpu_classes",
            "availability",
            "read_group",
            "read_instance",
            "read_events",
            "read_logs",
            "create",
            "other",
        ]
        | None
    ) = None
    usage_status: Literal["unknown"] = "unknown"
    # Why a create was refused (operator diagnostics only, never absence evidence).
    provider_rejection: ProviderRejection | None = None

    @model_validator(mode="after")
    def honest_termination(self):
        from .store import _parse_utc

        _parse_utc(self.observed_at)
        if self.state != "unknown" and (
            self.proxy_detail is not None
            or self.proxy_field is not None
            or self.proxy_request_kind is not None
        ):
            raise ValueError("read-failure diagnostics are only set on an unknown readback")
        if self.state != "unknown" and self.provider_id is None:
            raise ValueError("non-unknown provider observations require exact provider identity")
        if self.provider_rejection is not None and (
            self.proxy_reason not in {"provider_rejected_request", "request_not_sent"}
            or (self.provider_rejection.status == 0) != (self.proxy_reason == "request_not_sent")
            or self.state != "unknown"
            or self.provider_id is not None
        ):
            raise ValueError("a provider rejection is only an unknown, unbound create receipt")
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
