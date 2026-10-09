"""Content-free public harness capture declarations, never transcript payloads."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .agent_provenance_guard_models import PublicGuardPhaseBinding

HarnessID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")]
HarnessDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
SourceSequence = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
PublicEvent = Literal[
    "message", "tool", "permission", "delegation", "generated_output", "guard_phase"
]


class PublicSourceGap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    first: SourceSequence
    last: SourceSequence
    reason: Literal[
        "source_unavailable", "redaction_withheld", "delivery_unknown", "unsupported_public_event"
    ]

    @model_validator(mode="after")
    def ordered(self):
        if self.last < self.first:
            raise ValueError("Source gap range is reversed.")
        return self


class PublicHarnessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["tessera-public-harness-metadata-v1"]
    run_id: HarnessID
    session_id: HarnessID
    harness_instance_id: HarnessID
    task_id: HarnessID | None
    context_id: HarnessID
    context_digest: HarnessDigest
    skill_digest: HarnessDigest
    config_digest: HarnessDigest
    input_context_id: HarnessID | None = None
    input_body_digest: HarnessDigest | None = None
    capture_policy_context_id: HarnessID | None = None
    public_event: PublicEvent
    source_event_id: HarnessID
    source_sequence: SourceSequence
    source_watermark: SourceSequence
    capture_state: Literal["bounded_public_stream", "unknown"]
    gaps: list[PublicSourceGap] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def source_bounds(self):
        fields = (self.input_context_id, self.input_body_digest, self.capture_policy_context_id)
        if any(v is not None for v in fields):
            if any(v is None for v in fields) or self.input_context_id != self.context_id:
                raise ValueError(
                    "Separated capture requires all input/policy references and input alias."
                )
            if self.input_context_id == self.capture_policy_context_id:
                raise ValueError("Curated input and capture policy must be distinct contexts.")
            if self.input_body_digest != self.context_digest:
                raise ValueError(
                    "Context digest must describe the curated input body, not policy JSON."
                )
        if self.source_sequence > self.source_watermark:
            raise ValueError("Source event exceeds its reported watermark.")
        last = 0
        for gap in self.gaps:
            if gap.first <= last or gap.last > self.source_watermark:
                raise ValueError("Source gaps must be ordered, disjoint and within the watermark.")
            if gap.first <= self.source_sequence <= gap.last:
                raise ValueError("A recorded source event cannot also be declared absent.")
            last = gap.last
        return self

    @model_serializer(mode="wrap")
    def omit_absent_context_separation(self, handler):
        value = handler(self)
        for field in ("input_context_id", "input_body_digest", "capture_policy_context_id"):
            if value.get(field) is None:
                value.pop(field, None)
        return value


class PublicHarnessDisclosure(BaseModel):
    """Strict JSON in a separately human-issued immutable agent context body."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["tessera-public-harness-disclosure-v1"]
    run_id: HarnessID
    task_id: HarnessID | None
    context_digest: HarnessDigest
    skill_digest: HarnessDigest
    config_digest: HarnessDigest
    allowed_public_events: list[PublicEvent] = Field(min_length=1, max_length=6)
    guard_phase_binding: PublicGuardPhaseBinding | None = None

    @model_serializer(mode="wrap")
    def omit_absent_guard_binding(self, handler):
        value = handler(self)
        if value.get("guard_phase_binding") is None:
            value.pop("guard_phase_binding", None)
        return value

    @model_validator(mode="after")
    def unique_events(self):
        if len(set(self.allowed_public_events)) != len(self.allowed_public_events):
            raise ValueError("Allowed public events must be unique.")
        return self


class SeparatedPublicHarnessDisclosure(PublicHarnessDisclosure):
    """Policy binds actual curated input; its own context ID is pinned externally."""

    schema_version: Literal["tessera-separated-public-harness-disclosure-v1"]
    input_context_id: HarnessID
    input_body_digest: HarnessDigest
    model_id: HarnessID
    session_id: HarnessID
    harness_instance_id: HarnessID

    @model_validator(mode="after")
    def input_digest(self):
        if self.context_digest != self.input_body_digest:
            raise ValueError("Policy context digest must pin the curated input body.")
        return self
