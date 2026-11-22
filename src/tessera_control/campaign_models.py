"""Campaign specs v1 (ADR 0038) and v2 (ADR 0040): typed, digest-pinned DAGs of Tessera runs.

The runner and store read only the normal form (``normalize``); a spec is validated with the
registered compute provider of each stage and approval binds the spec as submitted.
"""

from __future__ import annotations

import dataclasses
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_serializer,
    model_validator,
)

from .artifact_limits import ABSOLUTE_MAX_ARTIFACT_BYTES
from .campaign_providers import TrustClass
from .data_class import OUTPUT_DATA_CLASS, PRIVATE_DERIVED, PUBLIC, DataClass
from .egress_models import EgressPolicy
from .evaluation_models import MetricCategory
from .gate_evidence_models import OutcomeId
from .models import RawString4000, Sha256Digest, StrictDuration
from .research_models import NonBlank200, NonBlank4000
from .salad_workload_contract import WorkloadContractV1
from .store import canonical_digest

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
MetricName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
PinnedImage = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}$", max_length=330
    ),
]
Backend = Literal["docker-local-cpu-v1", "salad-gpu-v1"]
# A stage's scratch request (ADR 0059): 1 MiB to 64 GiB, granted only within the provider's
# operator ceiling. Absent means the operator's default.
ScratchBytes = Annotated[int, Field(strict=True, ge=1024 * 1024, le=64 * 1024**3)]
Cents = Annotated[int, Field(strict=True, ge=0, le=5000)]

# Experiment budget ceiling (experiment_budget_models.py ceiling_cents le=5000): a stage's
# worst-case hold across all its attempts must fit under it.
EXPERIMENT_CEILING_CENTS = 5000
# A grant must outlive the stage's max duration by this much so a run can finish and settle
# (ADR 0048): admission refuses a shorter ``grant_ttl_seconds``.
GRANT_SETTLE_MARGIN_SECONDS = 300
# Total stage inputs across a spec: 32 stages x 8 inputs would be 256 edges; the cap keeps
# validation and composition bounded well below that dense worst case.
MAX_INPUT_EDGES = 256

LOCAL_CPU_BACKEND = "docker-local-cpu-v1"
SALAD_GPU_BACKEND = "salad-gpu-v1"
SALAD_BACKEND = SALAD_GPU_BACKEND
# The execution target a salad-gpu-v1 stage routes to (registered third_party_compute).
SALAD_TARGET = "salad_container"
# Provider ids and runtimes the v1 backends map to (ADR 0040 section 1).
LOCAL_DOCKER_PROVIDER = "local-docker"
DOCKER_CPU_RUNTIME = "docker-cpu-v1"
SALAD_PROVIDER = "salad"
SALAD_RUNTIME = "salad-workload-v1"


from .campaign_gate_models import UnlockList  # noqa: E402


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SnapshotSource(_Strict):
    kind: Literal["snapshot"]
    name: Key
    snapshot_id: Identifier
    digest: Sha256Digest
    data_class: DataClass


class StageSource(_Strict):
    kind: Literal["stage"]
    name: Key
    stage: Key
    paths: Annotated[list[RawString4000], Field(min_length=1, max_length=128)]


StageInput = Annotated[SnapshotSource | StageSource, Field(discriminator="kind")]


# Exit codes that mean the infrastructure killed the container (SIGKILL from the engine or
# the kernel's out-of-memory killer, ADR 0058) rather than the task failing.
DEFAULT_INFRA_EXIT_CODES = (137,)
DEFAULT_INFRA_RETRIES = 0


class RetryPolicy(_Strict):
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=5)] = 1
    transient_exit_codes: Annotated[
        list[Annotated[int, Field(strict=True, ge=1, le=255)]], Field(max_length=16)
    ] = []
    # ADR 0058: extra attempts, beyond max_attempts, that an infrastructure failure (the
    # container killed, an infra exit code) may use. Off unless the spec says so, so an
    # approval never covers spend its approver did not see: when enabled it is in the digest
    # and counted in the stage's worst-case exposure. Never for a task failure, a policy
    # refusal or a budget refusal. Each is a new attempt with its own hold and receipt.
    infra_retries: Annotated[int, Field(strict=True, ge=0, le=3)] = DEFAULT_INFRA_RETRIES
    infra_exit_codes: Annotated[
        list[Annotated[int, Field(strict=True, ge=1, le=255)]], Field(min_length=1, max_length=8)
    ] = list(DEFAULT_INFRA_EXIT_CODES)  # noqa: RUF012 - pydantic copies defaults
    # Other registered providers an infrastructure retry may move to, in order. Each must
    # already be registered, run the same runtime and admit the stage's inputs (checked at
    # submit and approval); the campaign's own provider ban still applies.
    reroute_providers: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]],
        Field(max_length=4),
    ] = []  # noqa: RUF012
    # ADR 0067: stage metrics that report the stage stopped on a resource limit (its time
    # budget, for example) when their recorded value is above zero. Such an attempt is an
    # infrastructure failure, retried under ``infra_retries`` like a killed container.
    resource_limit_metrics: Annotated[list[MetricName], Field(max_length=4)] = []  # noqa: RUF012

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler):
        """Absent stays absent: a spec written before ADR 0058 keeps its digest."""
        value = handler(self)
        if self.infra_retries == DEFAULT_INFRA_RETRIES:
            value.pop("infra_retries", None)
        if tuple(self.infra_exit_codes) == DEFAULT_INFRA_EXIT_CODES:
            value.pop("infra_exit_codes", None)
        if not self.reroute_providers:
            value.pop("reroute_providers", None)
        if not self.resource_limit_metrics:
            value.pop("resource_limit_metrics", None)
        return value


class MetricSpec(_Strict):
    name: MetricName
    # Must be one of the stage's output_paths.
    path: RawString4000
    json_pointer: Annotated[str, StringConstraints(max_length=512, pattern=r"^(/[^/]*)*$")]


class StopRule(_Strict):
    metric: MetricName
    op: Literal["lt", "le", "gt", "ge", "eq"]
    value: float


class SaladBriefTemplate(_Strict):
    """Program brief fields a spec supplies; the runner fills compute and source ids."""

    objective: NonBlank4000
    hypothesis: NonBlank4000
    success_gate: NonBlank4000
    failure_gate: NonBlank4000
    stop_gate: NonBlank4000
    target_domain: NonBlank4000
    provenance_policy: NonBlank4000
    dataset_recommendation: NonBlank4000
    representation_recommendation: NonBlank4000
    split_recommendation: NonBlank4000
    metric_recommendation: NonBlank4000
    uncertainty: NonBlank4000


class SaladSourceTemplate(_Strict):
    uri: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    quoted_passage: NonBlank4000
    content_digest: Sha256Digest
    locator: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None


class SaladGateTemplate(_Strict):
    """Version-2 bound gate fields; the runner fills every id, split and variant binding."""

    category: MetricCategory
    metric: NonBlank200
    unit: NonBlank200
    procedure: NonBlank4000
    comparison: Literal["ge", "gt", "le", "lt", "eq"]
    threshold: Annotated[float, Field(allow_inf_nan=False)]
    statistic: Literal["mean", "ci_low", "ci_high"]
    minimum_replicates: Annotated[int, Field(strict=True, ge=2, le=1000)]
    minimum_samples: Annotated[int, Field(strict=True, ge=1, le=10**9)]
    allowed_sources: Annotated[
        list[Literal["derived", "human_entered"]], Field(min_length=1, max_length=2)
    ] = ["derived"]  # noqa: RUF012 - pydantic copies defaults


class SaladProgramTemplate(_Strict):
    outcome: OutcomeId
    brief: SaladBriefTemplate
    source: SaladSourceTemplate
    gate: SaladGateTemplate


class SaladStageSpec(_Strict):
    """Salad-specific stage fields; every policy value comes from the operator profile."""

    profile: Key
    worker_principal_id: Identifier
    workload: WorkloadContractV1
    program: SaladProgramTemplate
    grant_ttl_seconds: Annotated[int, Field(strict=True, ge=3600, le=43200)] = 21600
    # Whether the stage needs a GPU. The default is True, which is every existing Salad stage;
    # ``false`` declares a CPU-only stage that may run only on an operator CPU profile (and a GPU
    # stage never on one). Absent when True, so existing spec digests are unchanged.
    gpu: bool = True
    # ADR 0076: the workload carries a local-docker stage's own argv (an image entry in its
    # manifest) instead of equalling it. Set only by the stage adapter's expansion; absent when
    # False, so every existing spec digest is unchanged.
    adapted: bool = False

    @model_serializer(mode="wrap")
    def _omit_default_gpu(self, handler):
        value = handler(self)
        if self.gpu:
            value.pop("gpu", None)
        if not self.adapted:
            value.pop("adapted", None)
        return value


class SaladAdapterBody(_Strict):
    profile: Key


class SaladAdapterRequest(_Strict):
    """``provider_config`` of a Salad candidate that asks for the stage adapter (ADR 0076).

    Expanded to a full ``SaladStageSpec`` at submit; never stored, never run."""

    adapt: SaladAdapterBody


def is_adapter_request(config: Any) -> bool:
    return isinstance(config, dict) and "adapt" in config


class StageSpec(_Strict):
    key: Key
    backend: Backend
    image: PinnedImage
    argv: Annotated[list[RawString4000], Field(min_length=1, max_length=128)]
    # Refused for docker-local-cpu-v1: ExecutionSpec has no environment.
    env: dict[str, RawString4000] | None = None
    output_paths: Annotated[list[RawString4000], Field(min_length=1, max_length=128)]
    max_output_bytes: Annotated[int, Field(strict=True, ge=1, le=ABSOLUTE_MAX_ARTIFACT_BYTES)]
    max_duration_seconds: StrictDuration
    egress: EgressPolicy | None = None
    max_exposure_cents: Cents = 0
    grant_ttl_seconds: Annotated[int, Field(strict=True, ge=60, le=43200)] = 900
    retry: RetryPolicy = RetryPolicy()
    scratch_bytes: ScratchBytes | None = None
    inputs: Annotated[list[StageInput], Field(min_length=1, max_length=8)]
    metrics: Annotated[list[MetricSpec], Field(max_length=16)] = []
    stop_when: Annotated[list[StopRule], Field(max_length=8)] = []
    # ADR 0039: outputs are always private_derived, and no stage declaration can make a
    # snapshot public (only a human staging declaration with evidence does). The field is
    # kept so the spec shape is stable, but only the one value is accepted.
    output_data_class: Literal["private_derived"] = "private_derived"
    # Present iff backend is salad-gpu-v1; absent keeps every CPU spec digest unchanged.
    salad: SaladStageSpec | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_salad(self, handler):
        value = handler(self)
        if self.salad is None:
            value.pop("salad", None)
        if self.scratch_bytes is None:
            value.pop("scratch_bytes", None)
        return value


class CampaignSpec(_Strict):
    """Spec version 1: unchanged shape and digests (ADR 0038)."""

    version: Literal[1]
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    budget_cap_cents: Cents
    currency: Literal["USD"] = "USD"
    deadline_seconds: Annotated[int, Field(strict=True, ge=60, le=7 * 86_400)]
    worker_principal_id: Identifier
    stages: Annotated[list[StageSpec], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def _structure(self) -> CampaignSpec:
        for stage in self.stages:
            _check_v1_shape(stage)
        validate_structure(normalize(self))
        return self


CampaignSpecV1 = CampaignSpec


# -- spec version 2 (ADR 0040) --------------------------------------------------------

ProviderId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
OutputKind = Literal[
    "dataset", "model", "cache", "metrics", "code", "log", "evidence_rows", "other"
]
MAX_EVIDENCE_CELLS = 5000
MAX_STAGES_V2 = 64
MAX_INPUT_EDGES_V2 = 512
MAX_SPEC_BYTES_V2 = 1024 * 1024
MAX_OUTPUT_BYTES = ABSOLUTE_MAX_ARTIFACT_BYTES


class EvidenceRowsSpec(_Strict):
    """The pre-registered grid an ``evidence_rows`` output must fill exactly (ADR 0052).

    The human approves the split lock, the variants, the seeds, the metrics (with their
    units) and the partitions with the spec digest. The platform ingests the stage's rows only
    if they cover this grid with no gap and no extra cell, so a result can neither be
    cherry-picked nor padded after it is seen.
    """

    split_lock_id: Identifier
    variants: Annotated[list[Identifier], Field(min_length=1, max_length=8)]
    seeds: Annotated[
        list[Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]],
        Field(min_length=1, max_length=64),
    ]
    metrics: Annotated[dict[MetricName, Identifier], Field(min_length=1, max_length=8)]
    partitions: Annotated[list[Identifier], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def _grid(self) -> EvidenceRowsSpec:
        for label, values in (
            ("variants", self.variants),
            ("seeds", self.seeds),
            ("partitions", self.partitions),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"evidence {label} must be unique")
        cells = len(self.variants) * len(self.seeds) * len(self.metrics) * len(self.partitions)
        if cells > MAX_EVIDENCE_CELLS:
            raise ValueError(f"evidence grid exceeds {MAX_EVIDENCE_CELLS} cells")
        return self


class OutputSpec(_Strict):
    name: Key
    path: RawString4000
    kind: OutputKind = "other"
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_OUTPUT_BYTES)]
    # Present iff kind is evidence_rows; absent keeps every existing spec digest unchanged.
    evidence: EvidenceRowsSpec | None = None

    @model_validator(mode="after")
    def _evidence_matches_kind(self) -> OutputSpec:
        if (self.kind == "evidence_rows") != (self.evidence is not None):
            raise ValueError("evidence is required for, and only for, kind evidence_rows")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_evidence(self, handler):
        value = handler(self)
        if self.evidence is None:
            value.pop("evidence", None)
        return value


class StageOutputSource(_Strict):
    kind: Literal["stage"]
    name: Key
    stage: Key
    outputs: Annotated[list[Key], Field(min_length=1, max_length=128)]


class ReceiptLineage(_Strict):
    """Where a pinned artifact input came from (ADR 0080), recorded by submit.

    ``predecessor`` is the chain name the author used (absent when the source campaign was
    named by id); ``source_receipt_sha256`` is the digest of the predecessor's terminal
    receipt as read at submit. It is part of the spec the approver reviews."""

    predecessor: Key | None = None
    source_receipt_sha256: Sha256Digest

    @model_serializer(mode="wrap")
    def _omit_absent_predecessor(self, handler):
        value = handler(self)
        if self.predecessor is None:
            value.pop("predecessor", None)
        return value


class ArtifactSource(_Strict):
    kind: Literal["artifact"]
    name: Key
    campaign_id: Identifier
    stage: Key
    output: Key
    sha256: Sha256Digest
    # Present only when submit resolved a ``receipt_output`` reference; absent keeps every
    # existing spec digest unchanged.
    resolved_from: ReceiptLineage | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_lineage(self, handler):
        value = handler(self)
        if self.resolved_from is None:
            value.pop("resolved_from", None)
        return value


class ReceiptOutputSource(_Strict):
    """A reference to an output a finished campaign's receipt lists (ADR 0080).

    Accepted only in a submit request: submit resolves it to a pinned ``artifact`` input
    (campaign id and sha256 read from the predecessor's succeeded receipt) before the spec
    is digested, stored or approved, so approval binds the exact bytes. Name the source
    campaign by ``campaign_id`` or, within a submit that carries ``predecessors``, by the
    ``predecessor`` name."""

    kind: Literal["receipt_output"]
    name: Key
    campaign_id: Identifier | None = None
    predecessor: Key | None = None
    stage: Key
    output: Key

    @model_validator(mode="after")
    def _one_source(self) -> ReceiptOutputSource:
        if (self.campaign_id is None) == (self.predecessor is None):
            raise ValueError("name exactly one of campaign_id and predecessor")
        return self


StageInputV2 = Annotated[
    SnapshotSource | StageOutputSource | ArtifactSource | ReceiptOutputSource,
    Field(discriminator="kind"),
]


class GateMetricDeclaration(_Strict):
    """A stage metric (by name) that becomes a gate metric record on a pre-registered split."""

    name: MetricName
    partition: Identifier
    unit: Identifier


class GateEvidenceSpec(_Strict):
    """Pre-registered split lock, seed and metrics a stage's attempt is bound to (ADR 0049).

    The human approves these with the spec digest, so a seed, partition or unit can never be
    relabelled after results are seen. The split lock must already exist in the project.
    """

    split_lock_id: Identifier
    seed: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    metrics: Annotated[list[GateMetricDeclaration], Field(min_length=1, max_length=16)]


# ADR 0068: the purpose a sealed partition is opened for; one claim per (digest, scope).
ClaimScope = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")]


class HoldoutClaimSpec(_Strict):
    """A sealed partition the stage opens once (ADR 0068).

    ``input`` names one of the stage's own inputs: a ``snapshot`` or ``artifact`` input whose
    pinned digest equals ``partition_digest``. The platform records at most one claim per
    (project, ``partition_digest``, ``claim_scope``) and refuses every later one."""

    input: Key
    partition_digest: Sha256Digest
    claim_scope: ClaimScope


class EvaluationSpec(_Strict):
    """An evaluation stage: it runs the spec's scorer image on custody inputs (ADR 0068).

    ``final`` marks the stage the spec designates as final: no other stage may consume its
    outputs, so a score on a sealed partition can never feed back into selection. Only a
    final stage may declare ``holdout``. The scored values are the stage's ``metrics``."""

    final: bool = False
    holdout: HoldoutClaimSpec | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler):
        value = handler(self)
        if self.holdout is None:
            value.pop("holdout", None)
        return value


# ADR 0060: a stage may name further provider candidates after its own, in order.
MAX_ALTERNATE_PROVIDERS = 4


class ProviderCandidate(_Strict):
    """One more place a stage may run (ADR 0060). Approved with the spec digest.

    ``max_exposure_cents`` is this candidate's cost estimate, held while it runs; absent, the
    stage's own ``max_exposure_cents`` is used. It may never exceed the stage's value, which
    stays the per-run cap a person approved."""

    provider: ProviderId
    runtime: ProviderId
    provider_config: dict[str, Any] = {}  # noqa: RUF012 - pydantic copies defaults
    max_exposure_cents: Cents | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler):
        value = handler(self)
        if self.max_exposure_cents is None:
            value.pop("max_exposure_cents", None)
        return value


class StageV2(_Strict):
    key: Key
    provider: ProviderId
    runtime: ProviderId
    # Validated by the provider's strict config model; part of the digest; never secrets.
    provider_config: dict[str, Any] = {}  # noqa: RUF012 - pydantic copies defaults
    image: PinnedImage
    argv: Annotated[list[RawString4000], Field(min_length=1, max_length=128)]
    env: dict[str, RawString4000] | None = None
    egress: EgressPolicy | None = None
    max_duration_seconds: StrictDuration
    max_exposure_cents: Cents = 0
    grant_ttl_seconds: Annotated[int, Field(strict=True, ge=60, le=43200)] = 900
    retry: RetryPolicy = RetryPolicy()
    scratch_bytes: ScratchBytes | None = None
    inputs: Annotated[list[StageInputV2], Field(min_length=1, max_length=8)]
    outputs: Annotated[list[OutputSpec], Field(min_length=1, max_length=128)]
    metrics: Annotated[list[MetricSpec], Field(max_length=16)] = []
    stop_when: Annotated[list[StopRule], Field(max_length=8)] = []
    # Narrows the provider's trust setting for this stage; never widens it (ADR 0040 section 4).
    send_classes: Annotated[list[TrustClass], Field(min_length=1, max_length=3)] | None = None
    # Absent keeps every existing spec digest unchanged.
    gate_evidence: GateEvidenceSpec | None = None
    # ADR 0060: ordered further candidates after ``provider``; absent keeps the digest.
    alternate_providers: (
        Annotated[list[ProviderCandidate], Field(min_length=1, max_length=MAX_ALTERNATE_PROVIDERS)]
        | None
    ) = None
    # ADR 0068: an evaluation stage and its one-time holdout claim; absent keeps the digest.
    evaluation: EvaluationSpec | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler):
        value = handler(self)
        if self.evaluation is None:
            value.pop("evaluation", None)
        if self.send_classes is None:
            value.pop("send_classes", None)
        if self.gate_evidence is None:
            value.pop("gate_evidence", None)
        if self.scratch_bytes is None:
            value.pop("scratch_bytes", None)
        if self.alternate_providers is None:
            value.pop("alternate_providers", None)
        return value


class CampaignSpecV2(_Strict):
    version: Literal[2]
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    budget_cap_cents: Cents
    currency: Literal["USD"] = "USD"
    deadline_seconds: Annotated[int, Field(strict=True, ge=60, le=7 * 86_400)]
    # Providers this campaign must never use (narrows; a stage naming one is refused).
    forbid_providers: Annotated[list[ProviderId], Field(max_length=16)] = []  # noqa: RUF012
    stages: Annotated[list[StageV2], Field(min_length=1, max_length=MAX_STAGES_V2)]

    @model_validator(mode="after")
    def _structure(self) -> CampaignSpecV2:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > MAX_SPEC_BYTES_V2:
            raise ValueError("Campaign spec exceeds 1 MiB of canonical JSON")
        validate_structure(normalize(self))
        return self


AnyCampaignSpec = Annotated[CampaignSpec | CampaignSpecV2, Field(discriminator="version")]
_SPEC_ADAPTER: TypeAdapter[Any] = TypeAdapter(AnyCampaignSpec)


def parse_spec(value: Any) -> CampaignSpec | CampaignSpecV2:
    """Validate a spec of either version from a mapping or a spec model."""
    if isinstance(value, CampaignSpec | CampaignSpecV2):
        return value
    return _SPEC_ADAPTER.validate_python(value)


# Provider validation (runtimes, config, output limits) reads the mutable provider registry,
# so it runs only where a spec enters or is approved: a stored, approved spec is parsed
# structurally and a registry change never stops an in-flight campaign from parsing.
_PROVIDER_CHECKS: ContextVar[bool] = ContextVar("campaign_provider_checks", default=True)


def parse_spec_json(text: str, *, providers: bool = False) -> CampaignSpec | CampaignSpecV2:
    """Parse a stored spec. Structural only unless ``providers`` (submit and approve)."""
    token = _PROVIDER_CHECKS.set(providers)
    try:
        spec = _SPEC_ADAPTER.validate_json(text)
    finally:
        _PROVIDER_CHECKS.reset(token)
    if isinstance(spec, CampaignSpecV2) and any(
        isinstance(item, ReceiptOutputSource) for stage in spec.stages for item in stage.inputs
    ):
        raise ValueError("A stored spec cannot hold an unresolved receipt_output input.")
    return spec


def _check_v1_shape(stage: StageSpec) -> None:
    """v1 field rules tied to its two backends (v2 stages are checked by their provider)."""
    if stage.backend == LOCAL_CPU_BACKEND and stage.env is not None:
        raise ValueError(f"Stage {stage.key}: env is not supported for {LOCAL_CPU_BACKEND}")
    if stage.scratch_bytes is not None and stage.backend != LOCAL_CPU_BACKEND:
        raise ValueError(
            f"Stage {stage.key}: scratch_bytes is supported only for {LOCAL_CPU_BACKEND}"
        )
    if stage.egress is not None and stage.backend != LOCAL_CPU_BACKEND:
        raise ValueError(f"Stage {stage.key}: egress is supported only for {LOCAL_CPU_BACKEND}")
    if (stage.salad is not None) != (stage.backend == SALAD_BACKEND):
        raise ValueError(f"Stage {stage.key}: salad is required for, and only for, {SALAD_BACKEND}")


# -- normal form -----------------------------------------------------------------------


@dataclass(frozen=True)
class NormalInput:
    kind: Literal["snapshot", "stage", "artifact"]
    name: str
    # snapshot
    snapshot_id: str | None = None
    digest: str | None = None
    data_class: str | None = None
    # stage: upstream stage and its resolved output paths
    stage: str | None = None
    paths: tuple[str, ...] = ()
    # artifact
    campaign_id: str | None = None
    output: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class NormalOutput:
    name: str
    path: str
    kind: str
    media_type: str | None
    max_bytes: int
    evidence: EvidenceRowsSpec | None = None


@dataclass(frozen=True)
class NormalCandidate:
    provider: str
    runtime: str
    provider_config: dict[str, Any]
    max_exposure_cents: int | None = None


@dataclass(frozen=True)
class NormalStage:
    key: str
    provider: str
    runtime: str
    provider_config: dict[str, Any]
    image: str
    argv: tuple[str, ...]
    env: dict[str, str] | None
    egress: EgressPolicy | None
    max_duration_seconds: Any
    max_exposure_cents: int
    grant_ttl_seconds: int
    retry: RetryPolicy
    inputs: tuple[NormalInput, ...]
    outputs: tuple[NormalOutput, ...]
    metrics: tuple[MetricSpec, ...]
    stop_when: tuple[StopRule, ...]
    max_output_bytes: int
    send_classes: tuple[str, ...] | None = None
    gate_evidence: GateEvidenceSpec | None = None
    # v1 only: the spec-level (or Salad block) worker; v2 stages take the provider's.
    worker_principal_id: str | None = None
    scratch_bytes: int | None = None
    # ADR 0060: further provider candidates, in order, after ``provider``.
    alternates: tuple[NormalCandidate, ...] = ()
    # ADR 0068: an evaluation stage (and its one-time holdout claim).
    evaluation: EvaluationSpec | None = None

    @property
    def holdout(self) -> HoldoutClaimSpec | None:
        return None if self.evaluation is None else self.evaluation.holdout

    @property
    def output_paths(self) -> list[str]:
        return [item.path for item in self.outputs]

    @property
    def placement_ids(self) -> tuple[str, ...]:
        """The stage's own provider and its ``alternate_providers``: any attempt may use them."""
        return (self.provider, *(item.provider for item in self.alternates))

    @property
    def reroute_ids(self) -> tuple[str, ...]:
        """ADR 0058 ``retry.reroute_providers``: one candidate list with the others (ADR 0060
        section 5a), usable only by an infrastructure retry and under ADR 0058's limits."""
        return tuple(self.retry.reroute_providers)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        """Every provider the approved spec lets this stage run on, in candidate order."""
        return (*self.placement_ids, *self.reroute_ids)

    @property
    def has_candidates(self) -> bool:
        """True when the runner places this stage (it names more than one provider)."""
        return len(self.candidate_ids) > 1


def stage_on_candidate(stage: NormalStage, provider_id: str) -> NormalStage:
    """The stage as it runs on one of its approved candidates (ADR 0060).

    The candidate's runtime, provider_config and cost estimate replace the stage's; the
    candidate list is kept so the stage can be placed again. A provider id the stage does not
    name is a programming error (KeyError): nothing outside the approved list is reachable."""
    if provider_id == stage.provider:
        return stage
    if provider_id in stage.reroute_ids and provider_id not in stage.placement_ids:
        # ADR 0058: another worker-grant instance of the same runtime. It runs with its own
        # worker principal and without the stage's provider_config, at the stage's own hold.
        return dataclasses.replace(
            stage, provider=provider_id, worker_principal_id=None, provider_config={}
        )
    for item in stage.alternates:
        if item.provider == provider_id:
            return dataclasses.replace(
                stage,
                provider=item.provider,
                runtime=item.runtime,
                provider_config=dict(item.provider_config),
                max_exposure_cents=(
                    stage.max_exposure_cents
                    if item.max_exposure_cents is None
                    else item.max_exposure_cents
                ),
                worker_principal_id=None,
            )
    raise KeyError(f"Stage {stage.key} has no candidate provider {provider_id!r}")


def stage_candidates(stage: NormalStage) -> tuple[NormalStage, ...]:
    """Each approved candidate of a stage, as the stage would run on it, in order."""
    return tuple(stage_on_candidate(stage, pid) for pid in stage.candidate_ids)


def placement_candidates(stage: NormalStage) -> tuple[NormalStage, ...]:
    """The stage's own provider and alternates as it would run on each (reroute targets are
    checked by ADR 0058's ``check_reroute`` instead)."""
    return tuple(stage_on_candidate(stage, pid) for pid in stage.placement_ids)


@dataclass(frozen=True)
class NormalSpec:
    version: int
    name: str
    budget_cap_cents: int
    deadline_seconds: int
    forbid_providers: tuple[str, ...]
    stages: tuple[NormalStage, ...]
    worker_principal_id: str | None = None

    def stage(self, key: str) -> NormalStage:
        return next(item for item in self.stages if item.key == key)

    @property
    def max_edges(self) -> int:
        return MAX_INPUT_EDGES if self.version == 1 else MAX_INPUT_EDGES_V2


def _normalize_v1(spec: CampaignSpec) -> NormalSpec:
    stages = []
    for stage in spec.stages:
        is_salad = stage.backend == SALAD_BACKEND
        inputs: list[NormalInput] = []
        for item in stage.inputs:
            if isinstance(item, SnapshotSource):
                inputs.append(
                    NormalInput(
                        "snapshot",
                        item.name,
                        snapshot_id=item.snapshot_id,
                        digest=item.digest,
                        data_class=item.data_class,
                    )
                )
            else:
                inputs.append(
                    NormalInput("stage", item.name, stage=item.stage, paths=tuple(item.paths))
                )
        config = stage.salad.model_dump(mode="json") if stage.salad is not None else {}
        stages.append(
            NormalStage(
                key=stage.key,
                provider=SALAD_PROVIDER if is_salad else LOCAL_DOCKER_PROVIDER,
                runtime=SALAD_RUNTIME if is_salad else DOCKER_CPU_RUNTIME,
                provider_config=config,
                image=stage.image,
                argv=tuple(stage.argv),
                env=None if stage.env is None else dict(stage.env),
                egress=stage.egress,
                max_duration_seconds=stage.max_duration_seconds,
                max_exposure_cents=stage.max_exposure_cents,
                grant_ttl_seconds=(
                    stage.salad.grant_ttl_seconds if stage.salad else stage.grant_ttl_seconds
                ),
                retry=stage.retry,
                inputs=tuple(inputs),
                outputs=tuple(
                    NormalOutput(f"out{index}", path, "other", None, stage.max_output_bytes)
                    for index, path in enumerate(stage.output_paths)
                ),
                metrics=tuple(stage.metrics),
                stop_when=tuple(stage.stop_when),
                max_output_bytes=stage.max_output_bytes,
                worker_principal_id=(
                    stage.salad.worker_principal_id if stage.salad else spec.worker_principal_id
                ),
                scratch_bytes=stage.scratch_bytes,
            )
        )
    return NormalSpec(
        1,
        spec.name,
        spec.budget_cap_cents,
        spec.deadline_seconds,
        (),
        tuple(stages),
        spec.worker_principal_id,
    )


def _normalize_v2(spec: CampaignSpecV2) -> NormalSpec:
    declared = {stage.key: {out.name: out.path for out in stage.outputs} for stage in spec.stages}
    stages = []
    for stage in spec.stages:
        inputs: list[NormalInput] = []
        for item in stage.inputs:
            if isinstance(item, SnapshotSource):
                inputs.append(
                    NormalInput(
                        "snapshot",
                        item.name,
                        snapshot_id=item.snapshot_id,
                        digest=item.digest,
                        data_class=item.data_class,
                    )
                )
            elif isinstance(item, StageOutputSource):
                upstream = declared.get(item.stage)
                if upstream is None:
                    raise ValueError(f"Stage {stage.key} input {item.name} names unknown stage")
                missing = [name for name in item.outputs if name not in upstream]
                if missing:
                    raise ValueError(
                        f"Stage {stage.key} input {item.name} uses undeclared outputs "
                        f"of {item.stage}: {sorted(missing)}"
                    )
                inputs.append(
                    NormalInput(
                        "stage",
                        item.name,
                        stage=item.stage,
                        paths=tuple(upstream[name] for name in item.outputs),
                    )
                )
            elif isinstance(item, ReceiptOutputSource):
                # Structure checks of a submit request see the reference as an artifact
                # input; submit replaces it before anything is stored (ADR 0080) and
                # parse_spec_json refuses a stored spec that still holds one.
                inputs.append(
                    NormalInput(
                        "artifact",
                        item.name,
                        campaign_id=item.campaign_id or "",
                        stage=item.stage,
                        output=item.output,
                        sha256="0" * 64,
                    )
                )
            else:
                inputs.append(
                    NormalInput(
                        "artifact",
                        item.name,
                        campaign_id=item.campaign_id,
                        stage=item.stage,
                        output=item.output,
                        sha256=item.sha256,
                    )
                )
        stages.append(
            NormalStage(
                key=stage.key,
                provider=stage.provider,
                runtime=stage.runtime,
                provider_config=dict(stage.provider_config),
                image=stage.image,
                argv=tuple(stage.argv),
                env=None if stage.env is None else dict(stage.env),
                egress=stage.egress,
                max_duration_seconds=stage.max_duration_seconds,
                max_exposure_cents=stage.max_exposure_cents,
                grant_ttl_seconds=stage.grant_ttl_seconds,
                retry=stage.retry,
                inputs=tuple(inputs),
                outputs=tuple(
                    NormalOutput(o.name, o.path, o.kind, o.media_type, o.max_bytes, o.evidence)
                    for o in stage.outputs
                ),
                metrics=tuple(stage.metrics),
                stop_when=tuple(stage.stop_when),
                max_output_bytes=sum(o.max_bytes for o in stage.outputs),
                send_classes=None if stage.send_classes is None else tuple(stage.send_classes),
                gate_evidence=stage.gate_evidence,
                scratch_bytes=stage.scratch_bytes,
                alternates=tuple(
                    NormalCandidate(
                        item.provider,
                        item.runtime,
                        dict(item.provider_config),
                        item.max_exposure_cents,
                    )
                    for item in stage.alternate_providers or ()
                ),
                evaluation=stage.evaluation,
            )
        )
    return NormalSpec(
        2,
        spec.name,
        spec.budget_cap_cents,
        spec.deadline_seconds,
        tuple(spec.forbid_providers),
        tuple(stages),
    )


def normalize(spec: CampaignSpec | CampaignSpecV2 | NormalSpec) -> NormalSpec:
    """Pure: either spec version to the in-memory form only the runner reads.

    The normal form is never digested or stored; approval binds the spec as submitted.
    """
    if isinstance(spec, NormalSpec):
        return spec
    if isinstance(spec, CampaignSpec):
        return _normalize_v1(spec)
    return _normalize_v2(spec)


# -- providers -------------------------------------------------------------------------


def stage_provider(provider_id: str) -> Any:
    """The registered provider; raises KeyError for an unknown id."""
    from .campaign_providers import BUILTIN_MODULES, get_provider, load_builtin_providers

    try:
        load_builtin_providers()
    except ModuleNotFoundError as error:
        if error.name not in BUILTIN_MODULES:
            raise
    return get_provider(provider_id)


def _provider(provider_id: str) -> Any:
    try:
        return stage_provider(provider_id)
    except KeyError:
        raise ValueError(f"Unknown compute provider: {provider_id!r}") from None


def topological_order(spec: CampaignSpec | CampaignSpecV2 | NormalSpec) -> list[str]:
    """Kahn's algorithm with ties broken by key; raises ValueError on a cycle."""
    normal = normalize(spec)
    depends = {
        stage.key: {item.stage for item in stage.inputs if item.kind == "stage"}
        for stage in normal.stages
    }
    order: list[str] = []
    ready = sorted(key for key, deps in depends.items() if not deps)
    remaining = {key: set(deps) for key, deps in depends.items() if deps}
    while ready:
        key = ready.pop(0)
        order.append(key)
        for other, deps in list(remaining.items()):
            deps.discard(key)
            if not deps:
                del remaining[other]
                ready.append(other)
        ready.sort()
    if remaining:
        raise ValueError(f"Campaign stages form a cycle: {sorted(remaining)}")
    return order


def stage_classes(
    spec: CampaignSpec | CampaignSpecV2 | NormalSpec,
) -> dict[str, tuple[DataClass, DataClass]]:
    """(input_class, output_class) per stage.

    A stage input is public only when every source is a snapshot the spec declares public;
    any stage-output or artifact source (outputs are always private_derived, ADR 0039) or
    any private_derived snapshot makes the stage input private_derived. This is the spec's
    own claim; the control plane additionally checks it against the recorded class.
    """
    normal = normalize(spec)
    topological_order(normal)  # raises ValueError on a cycle
    classes: dict[str, tuple[DataClass, DataClass]] = {}
    for stage in normal.stages:
        restricted = any(
            item.kind != "snapshot" or item.data_class == PRIVATE_DERIVED for item in stage.inputs
        )
        classes[stage.key] = (PRIVATE_DERIVED if restricted else PUBLIC, OUTPUT_DATA_CLASS)
    return classes


def stage_input_class(spec: CampaignSpec | CampaignSpecV2 | NormalSpec, key: str) -> DataClass:
    """Most restrictive class among a stage's sources."""
    return stage_classes(spec)[key][0]


def stage_output_class(spec: CampaignSpec | CampaignSpecV2 | NormalSpec, key: str) -> DataClass:
    """Outputs never become less restrictive than their inputs."""
    return stage_classes(spec)[key][1]


def needs_composition(stage: NormalStage) -> bool:
    return len(stage.inputs) > 1 or any(item.kind != "snapshot" for item in stage.inputs)


def stage_execution_spec(
    spec: CampaignSpec | CampaignSpecV2 | NormalSpec, key: str, input_digest: str
) -> dict[str, Any]:
    stage = normalize(spec).stage(key)
    return _provider(stage.provider).execution_spec(stage, input_digest)


def validate_structure(spec: NormalSpec) -> None:
    """Raise ValueError with a precise message when the DAG is not well formed."""
    keys = [stage.key for stage in spec.stages]
    if len(set(keys)) != len(keys):
        raise ValueError("Campaign stage keys must be unique")
    stages = {stage.key: stage for stage in spec.stages}
    for stage in spec.stages:
        names = [item.name for item in stage.inputs]
        if len(set(names)) != len(names):
            raise ValueError(f"Stage {stage.key} input names must be unique")
        if len({o.name for o in stage.outputs}) != len(stage.outputs) or len(
            {o.path for o in stage.outputs}
        ) != len(stage.outputs):
            raise ValueError(f"Stage {stage.key} output names and paths must be unique")
        for item in stage.inputs:
            if item.kind != "stage":
                continue
            if item.stage == stage.key:
                raise ValueError(f"Stage {stage.key} cannot consume its own outputs")
            if item.stage not in stages:
                raise ValueError(f"Stage {stage.key} input {item.name} names unknown stage")
            undeclared = set(item.paths) - set(stages[item.stage].output_paths)
            if undeclared:
                raise ValueError(
                    f"Stage {stage.key} input {item.name} uses undeclared outputs "
                    f"of {item.stage}: {sorted(undeclared)}"
                )
        metric_names = [metric.name for metric in stage.metrics]
        if len(set(metric_names)) != len(metric_names):
            raise ValueError(f"Stage {stage.key} metric names must be unique")
        for metric in stage.metrics:
            if metric.path not in stage.output_paths:
                raise ValueError(f"Stage {stage.key} metric {metric.name} path is not an output")
        if stage.gate_evidence is not None:
            # A retry re-admits the same variant, and one seed binds once per variant and
            # lock (no seed shopping), so a retry could never bind. Refuse the combination.
            if stage.retry.max_attempts > 1:
                raise ValueError(
                    f"Stage {stage.key} gate_evidence requires retry.max_attempts of 1"
                )
            declared = [m.name for m in stage.gate_evidence.metrics]
            if len(set(declared)) != len(declared) or not set(declared) <= set(metric_names):
                raise ValueError(
                    f"Stage {stage.key} gate_evidence metrics must be unique stage metrics"
                )
        for rule in stage.stop_when:
            if rule.metric not in metric_names:
                raise ValueError(f"Stage {stage.key} stop rule names unknown metric")
        limits = stage.retry.resource_limit_metrics
        if len(set(limits)) != len(limits) or not set(limits) <= set(metric_names):
            raise ValueError(
                f"Stage {stage.key}: resource_limit_metrics must be distinct metrics of the stage"
            )
        stopping = {rule.metric for rule in stage.stop_when} & set(limits)
        if stopping:
            raise ValueError(
                f"Stage {stage.key}: resource_limit_metrics must not also be stop_when metrics "
                f"({', '.join(sorted(stopping))}): one meaning per metric (ADR 0067)"
            )
        if stage.evaluation is not None:
            _validate_evaluation(spec, stage)
        reroutes = stage.retry.reroute_providers
        if len(set(reroutes)) != len(reroutes) or stage.provider in reroutes:
            raise ValueError(f"Stage {stage.key}: reroute_providers must be distinct from it")
        banned = set(reroutes) & set(spec.forbid_providers)
        if banned:
            raise ValueError(
                f"Stage {stage.key}: reroute provider {sorted(banned)[0]} is forbidden"
            )
        # An enabled infrastructure retry is another attempt with its own hold, so it counts
        # (billable providers included); with it off this is the formula as it always was.
        worst_hold = stage.max_exposure_cents * (
            stage.retry.max_attempts + stage.retry.infra_retries
        )
        if worst_hold > EXPERIMENT_CEILING_CENTS:
            raise ValueError(
                f"Stage {stage.key} max_exposure_cents x (max_attempts + infra_retries) = "
                f"{worst_hold} "
                f"exceeds the experiment budget ceiling of {EXPERIMENT_CEILING_CENTS} cents"
            )
        if stage.max_exposure_cents > spec.budget_cap_cents:
            raise ValueError(f"Stage {stage.key} hold exceeds the campaign budget cap")
        if stage.provider in spec.forbid_providers:
            raise ValueError(f"Stage {stage.key}: provider {stage.provider} is forbidden")
        if len(set(stage.candidate_ids)) != len(stage.candidate_ids):
            raise ValueError(f"Stage {stage.key}: provider candidates must be distinct")
        for item in stage.alternates:
            if item.provider in spec.forbid_providers:
                raise ValueError(f"Stage {stage.key}: provider {item.provider} is forbidden")
            if (
                item.max_exposure_cents is not None
                and item.max_exposure_cents > stage.max_exposure_cents
            ):
                # The stage's own value is the per-run cap the approver saw (ADR 0060).
                raise ValueError(
                    f"Stage {stage.key}: candidate {item.provider} estimate exceeds the "
                    "stage's max_exposure_cents"
                )
    if sum(len(stage.inputs) for stage in spec.stages) > spec.max_edges:
        raise ValueError(f"Campaign declares more than {spec.max_edges} stage inputs in total")
    classes = stage_classes(spec)
    for stage in spec.stages:
        input_class = classes[stage.key][0]
        if stage.egress is not None and input_class == "private_derived":
            raise ValueError(f"Stage {stage.key}: private-derived inputs cannot use egress")
        if (
            stage.send_classes is not None
            and input_class == "private_derived"
            and "private_derived" not in stage.send_classes
        ):
            raise ValueError(
                f"Stage {stage.key}: send_classes excludes private_derived but the stage "
                "has non-public inputs"
            )
        if _PROVIDER_CHECKS.get():
            for candidate in placement_candidates(stage):
                _validate_provider(candidate)


def _validate_evaluation(spec: NormalSpec, stage: NormalStage) -> None:
    """ADR 0068: the shape of an evaluation stage and its one-time holdout claim."""
    label = f"Stage {stage.key}"
    evaluation = stage.evaluation
    assert evaluation is not None
    if not stage.metrics:
        raise ValueError(f"{label}: an evaluation stage must declare the metrics it scores")
    if evaluation.final:
        consumers = sorted(
            other.key
            for other in spec.stages
            if any(item.kind == "stage" and item.stage == stage.key for item in other.inputs)
        )
        if consumers:
            raise ValueError(
                f"{label}: a final evaluation stage's outputs cannot feed another stage "
                f"({consumers[0]})"
            )
    holdout = evaluation.holdout
    if holdout is None:
        return
    if not evaluation.final:
        raise ValueError(f"{label}: only a stage designated final may open a holdout claim")
    source = next((item for item in stage.inputs if item.name == holdout.input), None)
    if source is None or source.kind not in {"snapshot", "artifact"}:
        raise ValueError(
            f"{label}: holdout input {holdout.input!r} must name a snapshot or artifact input"
        )
    pinned = source.digest if source.kind == "snapshot" else source.sha256
    if pinned != holdout.partition_digest:
        raise ValueError(f"{label}: holdout partition_digest does not match its input's digest")
    if source.kind == "snapshot" and source.data_class == PUBLIC:
        raise ValueError(f"{label}: a sealed partition cannot be declared public")
    # One-time means one scorer run: a retry, including an infrastructure retry, would let a
    # scorer that exits on purpose see the partition again (no score shopping).
    if stage.retry.max_attempts != 1 or stage.retry.infra_retries:
        raise ValueError(
            f"{label}: a holdout claim requires retry.max_attempts of 1 and no infra_retries"
        )
    for other in spec.stages:
        if other.key == stage.key:
            continue
        for item in other.inputs:
            digest = item.digest if item.kind == "snapshot" else item.sha256
            if digest == holdout.partition_digest:
                raise ValueError(
                    f"Stage {other.key}: input {item.name!r} is the sealed partition of "
                    f"{stage.key}; only its holdout stage may read it"
                )
    claims = [
        (other.holdout.partition_digest, other.holdout.claim_scope)
        for other in spec.stages
        if other.holdout is not None
    ]
    if claims.count((holdout.partition_digest, holdout.claim_scope)) > 1:
        raise ValueError(f"{label}: two stages claim the same sealed partition and scope")


def _validate_provider(stage: NormalStage) -> None:
    provider = _provider(stage.provider)
    descriptor = provider.descriptor
    label = f"Stage {stage.key}"
    if stage.runtime not in descriptor.runtimes:
        raise ValueError(f"{label}: provider {stage.provider} has no runtime {stage.runtime}")
    if is_adapter_request(stage.provider_config):
        # ADR 0076: a request, expanded by submit before anything is digested or stored. A
        # request that is still here when a stage is run is refused by the full model.
        try:
            SaladAdapterRequest.model_validate(stage.provider_config)
        except ValidationError:
            raise ValueError(f"{label}: invalid salad adapter request") from None
        if stage.provider != SALAD_PROVIDER or stage.runtime != SALAD_RUNTIME:
            raise ValueError(f"{label}: only a salad candidate takes the stage adapter")
        return
    model = provider.config_model
    if model is None:
        if stage.provider_config:
            raise ValueError(f"{label}: provider {stage.provider} takes no provider_config")
    else:
        try:
            model.model_validate(stage.provider_config)
        except ValidationError as error:
            first = error.errors()[0] if error.errors() else {}
            where = ".".join(str(part) for part in first.get("loc", ()))
            raise ValueError(
                f"{label}: invalid provider_config {where}: {str(first.get('msg', ''))[:200]}"
            ) from None
    limits = descriptor.output_limits
    if len(stage.outputs) > limits.max_files:
        raise ValueError(f"{label}: provider {stage.provider} allows {limits.max_files} outputs")
    if stage.max_output_bytes > limits.max_total_bytes:
        raise ValueError(f"{label}: outputs exceed the provider's {limits.max_total_bytes} bytes")
    if any(item.max_bytes > limits.max_file_bytes for item in stage.outputs):
        raise ValueError(f"{label}: an output exceeds the provider's per-file limit")
    if any(item.evidence is not None for item in stage.outputs) and (
        "ingest_evidence_rows" not in descriptor.delegable_steps
    ):
        # Fail closed at submission and approval: a provider that cannot take the ingest step
        # would otherwise run a stage whose rows could never reach a gate (ADR 0052).
        raise ValueError(f"{label}: provider {stage.provider} cannot deliver evidence_rows outputs")
    provider.validate_stage(stage)
    provider.execution_spec(stage, "0" * 64)


def spec_digest(spec: CampaignSpec | CampaignSpecV2) -> str:
    """Digest of the spec exactly as submitted (v1 digests are unchanged)."""
    return canonical_digest(spec.model_dump(mode="json"))


# API request bodies


class SubmitCampaign(_Strict):
    spec: AnyCampaignSpec
    # ADR 0080: chain names for ``receipt_output`` references (name -> earlier campaign id).
    predecessors: dict[Key, Identifier] = Field(default_factory=dict, max_length=16)

    @model_serializer(mode="wrap")
    def _omit_empty_predecessors(self, handler):
        # Absent when empty, so a request digest from before ADR 0080 is unchanged.
        value = handler(self)
        if not self.predecessors:
            value.pop("predecessors", None)
        return value


class StagePreapproval(_Strict):
    """Opt-in Salad review pre-approval for one stage (ADR 0044); approve body only."""

    stage_key: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    # The Salad candidate this covers when it is not the stage's own provider: one of the
    # stage's ``alternate_providers`` (ADR 0066). Absent keeps every earlier body's digest.
    provider: ProviderId | None = None
    source_assessment_digest: Sha256Digest
    plan_digest: Sha256Digest
    source_use_digest: Sha256Digest
    max_attempt_cents: Annotated[int, Field(strict=True, ge=1, le=5000)]
    max_total_cents: Annotated[int, Field(strict=True, ge=1, le=5000)]
    expires_at: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = None


class ApproveCampaign(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    expected_spec_digest: Sha256Digest
    preapprovals: Annotated[list[StagePreapproval], Field(max_length=64)] = Field(
        default_factory=list
    )
    # Conditional unlock (ADR 0045): checked in the admission transaction, fail closed.
    unlock: UnlockList | None = None


def approve_payload(body: ApproveCampaign) -> dict[str, Any]:
    """The recorded approve payload: unset optional parts are omitted, so an approval that
    uses neither pre-approvals (ADR 0044) nor an unlock (ADR 0045) keeps its old digest."""
    payload = body.model_dump(mode="json", exclude_none=True)
    if not payload.get("preapprovals"):
        payload.pop("preapprovals", None)
    return payload


class CancelCampaign(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


CAMPAIGN_STATUSES = (
    "submitted",
    "running",
    "paused_delegation_lapsed",
    "stopping",
    "cancel_requested",
    "blocked",
    "succeeded",
    "stopped",
    "failed",
    "cancelled",
)
TERMINAL_CAMPAIGN = frozenset({"succeeded", "stopped", "failed", "cancelled"})
STAGE_STATUSES = (
    "pending",
    "preparing",
    "running",
    "succeeded",
    "failed",
    "refused_budget",
    "blocked_unknown",
    "cancelled",
    "skipped",
)
ATTEMPT_STATUSES = (
    "reserved",
    "granted",
    "admitted",
    "running",
    "exited",
    "collected",
    "resolved",
    "measured",
    "reconciled",
    "succeeded",
    "failed_transient",
    "failed_permanent",
    "unknown",
    "refused_budget",
    "cancelled",
)
FAILURE_CLASSES = (
    "t1_grant_expired_unleased",
    "t2_declared_exit_code",
    "undeclared_exit_code",
    "salad_not_passed",
    "observed_unknown",
    "quarantine_failed",
    "missing_receipt",
    "missing_manifest",
    "step_refused",
    "budget_refused",
    "exposure_unknown",
    "t1_salad_unclaimed",
    "infra_container_killed",
    "infra_resource_limit",
    "not_started",
    "exit_confirmed_unresolved",
    "cancelled",
    "holdout_already_claimed",
    "sealed_partition",
)
