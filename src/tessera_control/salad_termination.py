"""Provider-confirmed termination of a reallocated Salad route (ADR 0149).

A route whose allocation changed is fenced for good: its ``termination_confirmed`` is never set,
so retained output is never imported and the group is never started or deleted through it. That
fence stays. What it also did was leave the attempt with no way to prove its spend had ended, so
the hold stayed until a person reconciled it.

``provider_termination`` is a pure read of the route state control already keeps. It confirms
termination only from provider readbacks recorded on the route after its stop effect (provenance
``provider``, which the trusted intake accepts only for a claimed effect of the bound server): the
latest such readback must read the exact group ``stopped`` with termination confirmed (no
instance, every count present and zero, no change in flight) under the route's own provider
identity. A later adverse readback undoes it. The guardian's status, a worker report, a
synthetic readback or a readback from before the stop never confirm anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import _parse_utc, canonical_digest

CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
NOT_APPLICABLE = "not_applicable"

# Stage-status safe codes (short, no free text) for an attempt left held.
UNCONFIRMED_CODE = "termination_unconfirmed"
UNCONFIRMED_NEW_ALLOCATION_CODE = "termination_unconfirmed_new_allocation"
UNCONFIRMED_EVENT = "campaign.salad_termination_unconfirmed"


@dataclass(frozen=True)
class TerminationReading:
    status: str
    reason: str
    observed_at: str | None = None
    receipt_digest: str | None = None
    new_allocation_unconfirmed: bool = False
    provider_reads: int = 0

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIRMED

    @property
    def code(self) -> str:
        return (
            UNCONFIRMED_NEW_ALLOCATION_CODE if self.new_allocation_unconfirmed else UNCONFIRMED_CODE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "receipt_digest": self.receipt_digest,
            "new_allocation_unconfirmed": self.new_allocation_unconfirmed,
            "provider_reads": self.provider_reads,
        }


def _time(value: Any):
    try:
        return _parse_utc(str(value))
    except (TypeError, ValueError):
        return None


def provider_termination(route: dict[str, Any] | None) -> TerminationReading:
    """Whether the provider itself confirmed a reallocated route's group terminated."""
    if not route or route.get("reallocated") is not True:
        return TerminationReading(NOT_APPLICABLE, "route_not_reallocated")
    effects = route.get("effects") or []
    stops = [e for e in effects if e.get("action") == "stop"]
    if route.get("desired") != "stop" or not stops:
        return TerminationReading(UNCONFIRMED, "no_stop_requested")
    stopped_at = _time(stops[0].get("requested_at"))
    if stopped_at is None:
        return TerminationReading(UNCONFIRMED, "stop_time_unreadable")
    requested = {e.get("id"): _time(e.get("requested_at")) for e in effects}
    after = []
    for receipt in route.get("receipts") or []:
        if receipt.get("provenance") != "provider":
            continue
        effect_at = requested.get(receipt.get("effect_id"))
        observed = _time(receipt.get("observed_at"))
        if effect_at is None or observed is None or effect_at < stopped_at or observed < stopped_at:
            continue
        after.append(receipt)
    original = route.get("allocation_id")
    new_allocation = any(
        r.get("proxy_reason") == "reallocation_uncertain"
        or (r.get("allocation_id") is not None and r.get("allocation_id") != original)
        for r in after
    )
    if not after:
        return TerminationReading(UNCONFIRMED, "no_provider_read_after_stop")
    latest = after[-1]
    if (
        latest.get("termination_confirmed") is True
        and latest.get("state") == "stopped"
        and route.get("provider_id")
        and latest.get("provider_id") == route.get("provider_id")
        and latest.get("allocation_id") is None
    ):
        return TerminationReading(
            CONFIRMED,
            "provider_read_exact_group_stopped",
            observed_at=latest.get("observed_at"),
            receipt_digest=canonical_digest(latest),
            provider_reads=len(after),
        )
    return TerminationReading(
        UNCONFIRMED,
        "provider_read_not_terminal",
        new_allocation_unconfirmed=new_allocation,
        provider_reads=len(after),
    )
