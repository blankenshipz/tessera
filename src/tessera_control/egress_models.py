"""Frozen egress policy and worker journal models (ADR 0034 sections 1 and 3).

This module is imported by ``models`` so it must not import it; it carries its
own strict base instead of ``StrictRequest``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_serializer,
    model_validator,
)

from .capability_models import Destination
from .data_class import _NOT_A_LICENSE, LicenseId, SourceRef

MAX_EGRESS_BYTES = 2**31
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Count = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EgressSource(_Strict):
    """A human-approved declaration of where an allowlisted host's content comes from.

    It is part of the digest-bound plan and is recorded on the campaign receipt as a
    declaration. Tessera does not verify the license; it never makes outputs public.
    """

    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    license: LicenseId
    source: SourceRef

    @model_validator(mode="after")
    def license_is_identified(self) -> EgressSource:
        if self.license.upper() in _NOT_A_LICENSE:
            raise ValueError("an egress source needs an identified license")
        return self


class EgressPolicy(_Strict):
    """Plan-frozen allowlist; the workload never gets direct network access."""

    profile: Literal["allowlist-proxy-v1"]
    destinations: Annotated[list[Destination], Field(min_length=1, max_length=32)]
    max_egress_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_EGRESS_BYTES)]
    # Optional license/source declarations per allowlisted host; absent keeps every
    # existing plan digest unchanged.
    sources: Annotated[list[EgressSource], Field(min_length=1, max_length=32)] | None = None

    @model_validator(mode="after")
    def destinations_are_unique(self) -> EgressPolicy:
        pairs = [(item.host, item.port) for item in self.destinations]
        if len(set(pairs)) != len(pairs):
            raise ValueError("egress destinations must be unique")
        if self.sources is not None:
            hosts = {item.host for item in self.destinations}
            declared = [item.host for item in self.sources]
            if len(set(declared)) != len(declared) or not set(declared) <= hosts:
                raise ValueError("egress sources must name unique allowlisted hosts")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_sources(self, handler):
        value = handler(self)
        if self.sources is None:
            value.pop("sources", None)
        return value


class EgressDestinationSummary(_Strict):
    # Denied attempts may name any host the workload asked for, so this is not
    # a capability Destination.
    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    port: Annotated[int, Field(strict=True, ge=0, le=65535)]
    decision: Literal["allow", "deny"]
    connections: Count
    bytes_up: Count
    bytes_down: Count


class EgressJournalSummary(_Strict):
    connections_allowed: Count
    connections_denied: Count
    bytes_up: Count
    bytes_down: Count
    destinations: Annotated[list[EgressDestinationSummary], Field(max_length=256)]


class ReportEgressJournal(_Strict):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=1)]
    journal_sha256: Sha256
    summary: EgressJournalSummary
