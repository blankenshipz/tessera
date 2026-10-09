"""Closed bounded CSV declarations and observed byte-derived snapshot records."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_DATA_BYTES = 4 * 1024 * 1024
MAX_DATA_ROWS = 10_000
MAX_MANIFEST_BYTES = 64 * 1024
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DatasetColumn(ClosedModel):
    name: Name
    type: Literal["string", "integer", "number", "split"]
    role: Literal["id", "feature", "target", "split"]


class DatasetSource(ClosedModel):
    kind: Literal["synthetic", "local", "published"]
    reference: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    license: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")]


class DatasetDeclaration(ClosedModel):
    version: Literal["tessera-dataset-snapshot-v1"]
    profile: Literal["bounded-tabular-regression-v1"]
    columns: Annotated[tuple[DatasetColumn, ...], Field(min_length=4, max_length=35)]
    source: DatasetSource

    @field_validator("columns", mode="before")
    @classmethod
    def columns_array(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def supported_schema(self):
        if len({c.name.casefold() for c in self.columns}) != len(self.columns):
            raise ValueError("Column names must be unique without case aliases")
        for role in ("id", "target", "split"):
            if sum(c.role == role for c in self.columns) != 1:
                raise ValueError("Exactly one id, target and split column is required")
        if not 1 <= sum(c.role == "feature" for c in self.columns) <= 32:
            raise ValueError("One to32 numeric features required")
        for c in self.columns:
            allowed = {
                "id": {"string"},
                "split": {"split"},
                "feature": {"number", "integer"},
                "target": {"number", "integer"},
            }
            if c.type not in allowed[c.role]:
                raise ValueError("Column type is unsupported for its declared role")
        return self


class DatasetSplit(ClosedModel):
    label: Literal["train", "validation", "test"]
    row_count: Annotated[int, Field(ge=1, le=MAX_DATA_ROWS)]
    sha256: Digest


class DatasetManifest(DatasetDeclaration):
    data_path: Literal["dataset.csv"]
    data_sha256: Digest
    data_size: Annotated[int, Field(ge=1, le=MAX_DATA_BYTES)]
    row_count: Annotated[int, Field(ge=3, le=MAX_DATA_ROWS)]
    splits: Annotated[tuple[DatasetSplit, ...], Field(min_length=3, max_length=3)]
    source_verification: Literal["declared-not-independently-verified"]
    license_verification: Literal["declared-not-legally-verified"]
    scientific_validation: Literal["not-established"]

    @field_validator("splits", mode="before")
    @classmethod
    def splits_array(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def split_counts(self):
        if tuple(s.label for s in self.splits) != ("train", "validation", "test"):
            raise ValueError("All three split labels in canonical order required")
        if sum(s.row_count for s in self.splits) != self.row_count:
            raise ValueError("Split row counts must sum to observed row count")
        return self


class UploadDatasetSnapshot(ClosedModel):
    """Proposed phase2 command; no caller verified/schema-observation flags."""

    declaration: DatasetDeclaration
    content_base64: Annotated[str, Field(min_length=4, max_length=((MAX_DATA_BYTES + 2) // 3) * 4)]
    expected_data_sha256: Digest
    expected_data_size: Annotated[int, Field(ge=1, le=MAX_DATA_BYTES)]
    expected_manifest_digest: Digest
    expected_input_snapshot_digest: Digest
