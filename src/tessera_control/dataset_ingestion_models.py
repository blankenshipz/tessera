"""Strict, metadata-only requests for bounded local dataset ingestion records."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .research_models import Digest, NonBlank200, NonBlank4000, Revision, StrictResearchRequest

ColumnName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    ),
]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class DatasetColumn(StrictResearchRequest):
    name: ColumnName
    logical_type: Literal["string", "integer", "number", "boolean", "categorical", "datetime"]
    nullable: bool
    unit: Unit | None = None
    missingness: Literal["none", "declared", "unknown"]

    @model_validator(mode="after")
    def require_missingness_for_nullable(self) -> DatasetColumn:
        if self.nullable and self.missingness == "none":
            raise ValueError("nullable columns require a missingness declaration")
        return self


class DatasetSchema(StrictResearchRequest):
    profile: Literal["mixfm-outcome-0-v1", "mixfm-outcome-1-v1"]
    columns: Annotated[list[DatasetColumn], Field(min_length=1, max_length=128)]

    @field_validator("columns")
    @classmethod
    def unique_column_names(cls, columns: list[DatasetColumn]) -> list[DatasetColumn]:
        if len({column.name for column in columns}) != len(columns):
            raise ValueError("dataset schema column names must be unique")
        return columns


class DatasetSourceRecord(StrictResearchRequest):
    source_id: NonBlank200
    expected_source_revision: Revision
    expected_content_digest: Digest
    license_expression: Literal[
        "CC-BY-4.0", "CC0-1.0", "ODC-BY-1.0", "PDDL-1.0", "custom_declared", "unknown"
    ]
    license_status: Literal["declared", "unsupported"]
    provenance_kind: Literal["source_assertion", "publisher_manifest", "manual_record"]
    provenance_status: Literal["declared", "unsupported"]


class ValidationSummary(StrictResearchRequest):
    status: Literal["not_run", "declared"]
    summary: NonBlank4000
    checked_rows: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)] = 0


class SplitStrategy(StrictResearchRequest):
    kind: Literal["random", "grouped", "temporal", "predefined", "holdout"]
    declaration: NonBlank4000
    leakage_policy: NonBlank4000
    leakage_status: Literal["declared", "unsupported"]


class RegisterDatasetSnapshot(StrictResearchRequest):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    name: NonBlank200
    snapshot_digest: Digest
    dataset_schema: DatasetSchema = Field(alias="schema", serialization_alias="schema")
    record_count: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    sources: Annotated[list[DatasetSourceRecord], Field(min_length=1, max_length=32)]
    validation_summary: ValidationSummary
    split_strategy: SplitStrategy

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, sources: list[DatasetSourceRecord]) -> list[DatasetSourceRecord]:
        if len({source.source_id for source in sources}) != len(sources):
            raise ValueError("dataset source records must be unique")
        return sources


class CreateIngestionProposal(StrictResearchRequest):
    snapshot_id: NonBlank200
    expected_snapshot_revision: Revision
    intended_use: NonBlank4000


class ReviewIngestionProposal(StrictResearchRequest):
    expected_revision: Revision
    decision: Literal["approve", "reject"]
    rationale: NonBlank4000
