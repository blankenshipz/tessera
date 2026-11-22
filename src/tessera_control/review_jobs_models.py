"""Request shapes for client-requested research/review jobs (ADR 0084).

A review job is a proposal for a reviewer, never a verdict: nothing here authorizes execution.
Prompt text is never stored, only its pinned digest; finding text is untrusted data.
"""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from .models import NonBlank200, NonBlank4000, Sha256Digest, StrictRequest, StrictRevision

JOB_KINDS = ("plan_review", "dataset_scout", "technique_scout", "pre_gate_review")
TIERS = ("haiku", "sonnet", "opus", "fable")
# ADR 0047 class names. Control derives a snapshot's class from its stored record; a client never
# supplies one. Snapshots resolve to public or private_derived (ADR 0039).
DATA_CLASSES = ("public", "private_code_approved", "private_derived")
MAX_BUDGET_CENTS = 10000
MAX_FINDINGS = 200


class ReviewInput(StrictRequest):
    snapshot_id: NonBlank200
    digest: Sha256Digest


class SubmitReviewJob(StrictRequest):
    kind: Literal["plan_review", "dataset_scout", "technique_scout", "pre_gate_review"]
    title: NonBlank200
    inputs: Annotated[list[ReviewInput], Field(min_length=1, max_length=16)]
    prompt_digest: Sha256Digest
    destination_id: NonBlank200
    tier: Literal["haiku", "sonnet", "opus", "fable"]
    budget_cents: Annotated[int, Field(strict=True, ge=0, le=MAX_BUDGET_CENTS)]
    egress: Literal["none", "model_runtime"] = "none"


class ApproveReviewTier(StrictRequest):
    expected_revision: StrictRevision
    rationale: NonBlank4000


class FindingEvidence(StrictRequest):
    source: Literal["subject", "context", "citation"]
    digest: Sha256Digest | None = None
    locator: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    uri: Annotated[str, Field(min_length=1, max_length=2000)] | None = None


class ReviewFinding(StrictRequest):
    id: NonBlank200
    severity: Literal["info", "minor", "major", "blocking"]
    category: NonBlank200
    claim: Annotated[str, Field(min_length=1, max_length=4096)]
    evidence: Annotated[list[FindingEvidence], Field(max_length=32)]
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class IngestFindings(StrictRequest):
    findings: Annotated[list[ReviewFinding], Field(max_length=MAX_FINDINGS)]
    review_md_digest: Sha256Digest | None = None
    receipt_digest: Sha256Digest | None = None

    @field_validator("findings")
    @classmethod
    def unique_ids(cls, value: list[ReviewFinding]) -> list[ReviewFinding]:
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("finding ids must be unique")
        return value


class ResolveReviewFinding(StrictRequest):
    decision: Literal["accept", "reject"]
    rationale: NonBlank4000
    expected_revision: StrictRevision
    expected_digest: Sha256Digest
