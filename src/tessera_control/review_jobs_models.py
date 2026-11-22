"""Request shapes for client-requested research/review jobs (ADR 0084).

A review job is a proposal for a reviewer, never a verdict: nothing here authorizes execution.
Prompt text is never stored, only its pinned digest; finding text is untrusted data.
"""

import math
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .models import NonBlank200, NonBlank4000, Sha256Digest, StrictRequest, StrictRevision

JOB_KINDS = ("plan_review", "dataset_scout", "technique_scout", "pre_gate_review", "code_review")
TIERS = ("haiku", "sonnet", "opus", "fable")
# ADR 0047 class names. Control derives a snapshot's class from its stored record; a client never
# supplies one. Snapshots resolve to public or private_derived (ADR 0039).
DATA_CLASSES = ("public", "private_code_approved", "private_derived")
MAX_BUDGET_CENTS = 10000
MAX_FINDINGS = 200
VERDICTS = ("MERGE", "MERGE-WITH-FOLLOWUPS", "CHANGES-REQUIRED")
TOKEN_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")


class ReviewInput(StrictRequest):
    snapshot_id: NonBlank200
    digest: Sha256Digest


class CodeReviewSubject(StrictRequest):
    """A content-addressed code snapshot (or base..head diff) and a pinned rubric (ADR 0092).

    Only digests: code and rubric text never reach control.
    """

    mode: Literal["snapshot", "diff"]
    head_digest: Sha256Digest
    base_digest: Sha256Digest | None = None
    rubric_digest: Sha256Digest

    @model_validator(mode="after")
    def base_rules(self):
        if self.mode == "diff" and self.base_digest is None:
            raise ValueError("a diff review needs base_digest")
        if self.mode == "snapshot" and self.base_digest is not None:
            raise ValueError("a snapshot review takes no base_digest")
        if self.base_digest is not None and self.base_digest == self.head_digest:
            raise ValueError("base_digest must differ from head_digest")
        return self


class SubmitReviewJob(StrictRequest):
    kind: Literal[
        "plan_review", "dataset_scout", "technique_scout", "pre_gate_review", "code_review"
    ]
    title: NonBlank200
    inputs: Annotated[list[ReviewInput], Field(min_length=1, max_length=16)]
    prompt_digest: Sha256Digest
    destination_id: NonBlank200
    tier: Literal["haiku", "sonnet", "opus", "fable"]
    budget_cents: Annotated[int, Field(strict=True, ge=0, le=MAX_BUDGET_CENTS)]
    egress: Literal["none", "model_runtime"] = "none"
    code_review: CodeReviewSubject | None = None

    @model_validator(mode="after")
    def code_review_subject(self):
        if (self.kind == "code_review") != (self.code_review is not None):
            raise ValueError("code_review subject is required for, and only for, kind code_review")
        if self.code_review is not None:
            # Binding: every subject digest is a declared input, so data class, sealed-holdout
            # refusal and destination policy apply to it.
            bound = {item.digest for item in self.inputs}
            subject = self.code_review
            named = [subject.head_digest, subject.rubric_digest]
            if subject.base_digest is not None:
                named.append(subject.base_digest)
            if any(digest not in bound for digest in named):
                raise ValueError("code_review digests must each be the digest of an input")
        return self


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


UInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
LimitKey = Annotated[str, Field(min_length=1, max_length=64)]
LimitValue = (
    Annotated[int, Field(strict=True, ge=0)]
    | Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
)
# ``cost`` is in cents; these bases are money spent against the job budget, so ``cost_cents``
# is required and bounded by it.
BILLED_COST_BASES = ("reported_cost", "computed_from_pinned_prices")
# A billed (or possibly billed) call whose usage is unknown (a timeout or 5xx after the request
# was sent): ``cost`` is the client's reserved preflight worst case in cents (counted input plus
# the output cap at pinned prices), a reservation and NOT a measured cost. It records the spend
# honestly as an over-estimate; such a receipt can never complete a job.
RESERVED_COST_BASIS = "reserved_worst_case"


class ModelCallReceipt(StrictRequest):
    """Model-call receipt for a code review (ADR 0083 fields). No prompt, completion, error
    text or credential: any other key is refused.

    Both identity bases are unverified: ``provider_response_model`` is what the provider's
    response named, not proof of the model that ran.

    ``reserved_worst_case`` (see :data:`RESERVED_COST_BASIS`) is a reservation for a call whose
    usage is unknown, not a measured cost: ``cost_cents`` is required and is ``ceil(cost)``, and
    ``model_reported`` may be ``None`` (no response named a model).
    """

    model_configured: NonBlank200
    model_reported: Annotated[str, Field(max_length=200)] | None = None
    model_identity_basis: Literal["harness_reported_unverified", "provider_response_model"]
    tier: Literal["haiku", "sonnet", "opus", "fable"]
    input_digests: Annotated[dict[NonBlank200, Sha256Digest], Field(max_length=32)]
    tokens: dict[
        Literal["input", "output", "reasoning", "cache_read", "cache_write", "total"],
        UInt,
    ]
    cost: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    cost_basis: Literal[
        "included_subscription_usage",
        "reported_cost",
        "computed_from_pinned_prices",
        "reserved_worst_case",
    ]
    cost_cents: UInt | None = None
    policy_sha256: Sha256Digest
    attempts: Annotated[int, Field(strict=True, ge=1, le=2)]
    # Optional ADR 0083 fields (digests, counters and caps only).
    run_id: NonBlank200 | None = None
    seq: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)] | None = None
    input_class: Literal["public", "private_code_approved", "private_derived"] | None = None
    prompt_sha256: Sha256Digest | None = None
    profile_sha256: Sha256Digest | None = None
    input_sha256: Sha256Digest | None = None
    output_sha256: Sha256Digest | None = None
    limits: Annotated[dict[LimitKey, LimitValue], Field(max_length=16)] | None = None
    daily_tokens_after: UInt | None = None
    daily_cost_cents_after: LimitValue | None = None  # cents; the producer may sum fractional cents
    cost_cents_estimate_max: LimitValue | None = (
        None  # cents; the producer may sum fractional cents
    )
    observed_at: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def reserved_cents(self):
        if self.cost_basis == RESERVED_COST_BASIS and (
            self.cost_cents is None or self.cost_cents != math.ceil(self.cost)
        ):
            raise ValueError("a reserved_worst_case receipt needs cost_cents = ceil(cost)")
        return self


class IngestFindings(StrictRequest):
    findings: Annotated[list[ReviewFinding], Field(max_length=MAX_FINDINGS)]
    review_md_digest: Sha256Digest | None = None
    receipt_digest: Sha256Digest | None = None
    # Code review only (ADR 0092); refused for other kinds by the store.
    verdict: Literal["MERGE", "MERGE-WITH-FOLLOWUPS", "CHANGES-REQUIRED"] | None = None
    receipt: ModelCallReceipt | None = None

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


class SubmitCodeReview(StrictRequest):
    """Submit a code review of a branch on the platform's own git remote (ADR 0108).

    Shapes only; the store applies the focused-test, round and chain rules with stable reason
    codes, and the git reader validates names against the operator's repository mapping.
    """

    repo: Annotated[str, Field(min_length=1, max_length=64)]
    branch: Annotated[str, Field(min_length=1, max_length=200)]
    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    base_branch: Annotated[str, Field(min_length=1, max_length=200)] = "main"
    tier: Literal["haiku", "sonnet", "opus", "fable"]
    destination_id: NonBlank200 = "anthropic"
    budget_cents: Annotated[int, Field(strict=True, ge=0, le=MAX_BUDGET_CENTS)]
    # Bounded loosely here so an over-long list is the store's stable focused_tests_invalid.
    focused_tests: Annotated[list[Annotated[str, Field(max_length=1000)]], Field(max_length=200)]
    previous_job_id: NonBlank200 | None = None
    recheck_findings: (
        Annotated[
            list[Annotated[int, Field(strict=True, ge=-(2**31), le=2**31)]],
            Field(max_length=MAX_FINDINGS),
        ]
        | None
    ) = None
