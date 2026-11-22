"""Strict requests for gate evidence: split locks, leakage audits, typed metrics,
replicate groups and the research program -> outcome entity.

Every statistic and digest named here is computed by the platform from stored
records. Callers name records; they never supply the numbers a gate compares.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .research_models import Digest, NonBlank200, NonBlank4000, StrictResearchRequest

# Outcome ids are research-program data declared by plans, not platform schema.
OutcomeId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
PartitionName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
MemberKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]
CiMethod = Literal["t_interval_95", "percentile_bootstrap_95"]
MeasurementSource = Literal["human_entered", "derived"]
# Top-level measurement value keys only the server may write (derived provenance).
RESERVED_MEASUREMENT_KEYS = frozenset(
    {"derivation", "json_pointer", "artifact_sha256", "source", "measurement_source"}
)
MAX_SPLIT_MEMBERS = 200_000


class SplitPartition(StrictResearchRequest):
    """Exact partition membership; `groups` (e.g. scaffold ids) parallels `keys`."""

    keys: Annotated[list[MemberKey], Field(min_length=1, max_length=MAX_SPLIT_MEMBERS)]
    groups: Annotated[list[MemberKey], Field(max_length=MAX_SPLIT_MEMBERS)] | None = None

    @model_validator(mode="after")
    def unique_keys_and_parallel_groups(self) -> SplitPartition:
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("partition member keys must be unique")
        if self.groups is not None and len(self.groups) != len(self.keys):
            raise ValueError("partition groups must parallel member keys")
        return self


class CreateSplitLock(StrictResearchRequest):
    """Freeze exact membership. The server computes and returns the membership digest;
    `expected_membership_digest` lets a caller assert it reproduced the same split."""

    name: NonBlank200
    partitions: Annotated[dict[PartitionName, SplitPartition], Field(min_length=1, max_length=16)]
    dataset_snapshot_id: NonBlank200 | None = None
    expected_membership_digest: Digest | None = None

    @field_validator("partitions")
    @classmethod
    def bounded_total(cls, partitions: dict[str, SplitPartition]) -> dict[str, SplitPartition]:
        if sum(len(p.keys) for p in partitions.values()) > MAX_SPLIT_MEMBERS:
            raise ValueError("split lock exceeds the member limit")
        return partitions


class CreateCrossSetLeakageAudit(StrictResearchRequest):
    """Overlap of a reference set (e.g. a pretraining corpus lock) with eval partitions."""

    reference_lock_id: NonBlank200
    reference_partitions: Annotated[list[PartitionName], Field(min_length=1, max_length=16)]
    eval_partitions: Annotated[list[PartitionName], Field(min_length=1, max_length=16)]


class MetricDeclaration(StrictResearchRequest):
    """One metric the attempt will report: the measurement `name` it will record,
    the locked partition it is computed on, and its unit."""

    name: NonBlank200
    partition: PartitionName
    unit: NonBlank200


class BindAttemptSplit(StrictResearchRequest):
    """Pre-register the split an admitted attempt evaluates on, before dispatch."""

    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    split_lock_id: NonBlank200
    # Pre-registered here so a seed, metric, partition or unit can never be
    # relabelled after results are seen.
    seed: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    metrics: Annotated[list[MetricDeclaration], Field(min_length=1, max_length=32)]

    @field_validator("metrics")
    @classmethod
    def unique_metric_names(cls, metrics: list[MetricDeclaration]) -> list[MetricDeclaration]:
        if len({m.name for m in metrics}) != len(metrics):
            raise ValueError("declared metric names must be unique")
        return metrics


class RecordMetric(StrictResearchRequest):
    """Type one stored measurement of a succeeded/admitted attempt as a per-seed metric."""

    # Metric, partition, unit and seed all come from the pre-dispatch binding.
    measurement_id: NonBlank200
    # Artifact lineage links checkpoint identity later; here it is a plain digest string.
    checkpoint_sha: Digest | None = None


class CreateReplicateGroup(StrictResearchRequest):
    """Aggregate EVERY metric record of one variant/metric/split partition.

    Members are selected by the server, never by the caller, so seeds cannot be
    cherry-picked; creation fails while any bound attempt is unaccounted for.
    """

    experiment_variant_id: NonBlank200
    metric: NonBlank200
    split_lock_id: NonBlank200
    partition: PartitionName
    ci_method: CiMethod = "t_interval_95"


class CreatePairedReplicateGroup(StrictResearchRequest):
    """Pair two experiment variants' per-seed metric records and aggregate the delta
    ``arm_a - arm_b``. Records are selected by the server; seeds must match exactly."""

    arm_a_variant_id: NonBlank200
    arm_b_variant_id: NonBlank200
    metric: NonBlank200
    split_lock_id: NonBlank200
    partition: PartitionName
    ci_method: CiMethod = "t_interval_95"


class CreateResearchProgram(StrictResearchRequest):
    name: NonBlank200
    objective: NonBlank4000


class AttachProgramOutcome(StrictResearchRequest):
    outcome: OutcomeId
    study_id: NonBlank200
    title: NonBlank200
