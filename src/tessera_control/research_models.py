"""Strict boundary models for the local research ledger.

These models intentionally describe recorded research claims, not execution.  In
particular, an imported/manual outcome has no execution receipt.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class StrictResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


NonBlank200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NonBlank4000 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
JsonMetadata = dict[str, Any]


def _bounded_json(value: dict[str, Any]) -> dict[str, Any]:
    """Keep recorded metadata JSON-shaped and bounded without a free-form blob."""

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 8:
            raise ValueError("metadata nesting exceeds 8 levels")
        if item is None or type(item) in {bool, int, float, str}:
            return
        if isinstance(item, list):
            if len(item) > 100:
                raise ValueError("metadata lists may contain at most 100 entries")
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 100 or any(type(key) is not str or len(key) > 200 for key in item):
                raise ValueError("metadata object keys are invalid")
            for child in item.values():
                visit(child, depth + 1)
            return
        raise ValueError("metadata must contain JSON values")

    visit(value)
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("metadata must be JSON serializable") from error
    if len(encoded.encode()) > 32_000:
        raise ValueError("metadata exceeds 32KB")
    return value


def _uri(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or (parsed.scheme in {"http", "https"} and not parsed.netloc):
        raise ValueError("uri must be absolute")
    return value


class CreateStudy(StrictResearchRequest):
    purpose: NonBlank4000
    protocol: NonBlank4000
    mode: Literal["exploratory", "confirmatory"] = "exploratory"


class RegisterVariant(StrictResearchRequest):
    name: NonBlank200
    method_digest: Digest
    input_digest: Digest
    metadata: JsonMetadata
    replication_label: Literal["rerun", "same_pipeline", "unavailable"] = "unavailable"

    _metadata_is_bounded = field_validator("metadata")(_bounded_json)


class RecordVariantStatus(StrictResearchRequest):
    expected_revision: Revision
    status: Literal["completed", "failed", "excluded", "abandoned"]
    reason: NonBlank4000 | None = None
    outcome: JsonMetadata | None = None
    provenance: Literal["manual", "imported_unverified"] = "manual"

    _outcome_is_bounded = field_validator("outcome")(
        lambda value: _bounded_json(value) if value else value
    )


class CreateSource(StrictResearchRequest):
    uri: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    quoted_passage: NonBlank4000
    content_digest: Digest
    locator: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
        | None
    ) = None

    _uri_is_absolute = field_validator("uri")(_uri)


class AssessSource(StrictResearchRequest):
    expected_revision: Revision
    verdict: Literal["supported", "unsupported", "unverified"]
    reason: NonBlank4000 | None = None


class CreateFinding(StrictResearchRequest):
    statement: NonBlank4000
    source_ids: Annotated[list[str], Field(min_length=1, max_length=200)]
    variant_ids: Annotated[list[str], Field(min_length=1, max_length=200)]


class AcceptFinding(StrictResearchRequest):
    expected_revision: Revision


class ReaffirmFinding(StrictResearchRequest):
    expected_revision: Revision
    reason: NonBlank4000


class CorrectSource(StrictResearchRequest):
    expected_revision: Revision
    uri: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    quoted_passage: NonBlank4000
    content_digest: Digest
    reason: NonBlank4000
    locator: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
        | None
    ) = None

    _uri_is_absolute = field_validator("uri")(_uri)


class CloseStudy(StrictResearchRequest):
    expected_revision: Revision


class ExportStudy(StrictResearchRequest):
    max_bytes: Annotated[int, Field(strict=True, ge=256, le=10_000_000)] = 1_000_000
