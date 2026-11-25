"""Next-class or next-candidate move after a pre-start failure that spent nothing (ADR 0060
addendum; the strict rule of ADR 0169).

A stage may fail on one candidate before anything started there: the provider refused to create
the group, the group never settled, or its inputs were never delivered. When that attempt is
proven never started and settled at exactly zero, the runner records it as ``prestart_no_spend``
and places the next attempt on the next approved class or candidate, re-running every placement
check (registry, enrollment, trust, licence labels, per-run cap, campaign budget, program budget,
project limits, open exposure, free slots, the heavy-stage guard). The bound is off unless the
spec sets it, and each move's hold counts in the approved worst case
(``RetryPolicy.worst_case_attempts``).

A candidate may offer several classes (a provider's ``fallback_classes`` hook): a Salad stage's
``profile`` then its ``alternate_profiles`` (ADR 0151, the one definition of that list), one GPU
class each. The next attempt takes the candidate's next listed profile first
(``salad_alternate_profiles.next_profile``, the one selector), recorded as a
``campaign.salad_profile_switched`` event (reason ``prestart_no_spend``) in the same transaction
as the attempt's transition (``campaign_salad_fallback.prestart_move``); then the next candidate.
The next profile must be covered exactly as for any approved profile (same per-run cap,
capability declaration, review pre-approval binding its plan digest): otherwise the stage is held
(``profile_not_approved``) and nothing moves. A class that failed this way is not chosen again
for the stage. The stage moves at most ``retry.max_class_fallbacks`` times; when no class is
left, or the bound is spent, the stage fails closed with ``class_fallback_exhausted``.

Nothing here decides whether an attempt started: only the provider can prove that from its own
recorded evidence (the optional ``prestart_failure`` provider hook; a provider without it never
falls back). The runner additionally requires a settled amount of exactly zero, so an attempt
that may have started or been billed keeps its existing path (failed, or unknown and held until
a person reconciles it) and never changes class (the strict rule). A stage with neither
alternates nor another class, or with ``max_class_fallbacks`` 0, is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PRESTART_NO_SPEND = "prestart_no_spend"
EXHAUSTED = "class_fallback_exhausted"
EVENT = "campaign.stage_class_fallback"
# Placement skip reason for a candidate on which this stage already failed before start.
SKIP_REASON = "prestart_failed"

# The closed set of reasons a provider may report for a pre-start failure. Anything else is
# not a fallback trigger. Each names the step that failed before the provider's start call.
REASONS = frozenset(
    {
        "create_rejected",  # the provider refused to create the group (capacity, availability)
        "route_state_unreadable",
        "create_failed",
        "group_unsettled",
        "ticket_mint_failed",
        "input_delivery_unconfirmed",
        "start_failed",  # refused before the start effect was recorded
        "wall_bound_exceeded",
        "prestart_unclassified",
    }
)
# How the attempt reached its zero settlement: its own failed outcome, or a person's
# reconciliation of an attempt whose outcome was unknown.
PATHS = frozenset({"failed", "reconciled_unknown"})


def prestart_attempts(attempts: Sequence[Mapping[str, Any]]) -> int:
    """Attempts of a stage that moved to another candidate after a pre-start failure."""
    return sum(1 for a in attempts if a["failure_class"] == PRESTART_NO_SPEND)


def failures(attempts: Sequence[Mapping[str, Any]], default: str) -> dict[str, int]:
    """Per candidate, the attempts of this stage that failed there before start."""
    found: dict[str, int] = {}
    for a in attempts:
        if a["failure_class"] == PRESTART_NO_SPEND:
            pid = a["provider_id"] or default
            found[pid] = found.get(pid, 0) + 1
    return found


def _provider(stage: Any) -> Any:
    from .campaign_models import stage_provider

    try:
        return stage_provider(stage.provider)
    except KeyError:
        return None


def class_count(stage: Any) -> int:
    """How many classes one candidate offers (a provider's optional ``fallback_classes``
    hook, for example a Salad stage's profile and its ``alternate_profiles``); 1 otherwise."""
    hook = getattr(_provider(stage), "fallback_classes", None)
    if hook is None:
        return 1
    try:
        return max(1, int(hook(stage)))
    except Exception:  # noqa: BLE001 - an unreadable config offers its own class only
        return 1


def on_current_class(rt: Any, ctx: Any, key: str, stage: Any) -> Any:
    """The candidate stage on the class its recorded pre-start moves name: for a Salad stage
    with alternates the profile of its latest ``campaign.salad_profile_switched`` event on this
    candidate (``campaign_salad_fallback.current_stage``); any other stage unchanged."""
    from . import campaign_salad_fallback

    return campaign_salad_fallback.current_stage(rt, ctx, key, stage)


def enabled(base: Any, candidates: Sequence[Any]) -> bool:
    """True when the stage may move on after a pre-start failure: fallbacks are allowed and
    it names another candidate (``alternate_providers``) or another class of one."""
    if base.retry.max_class_fallbacks < 1:
        return False
    return bool(base.alternates) or any(class_count(item) > 1 for item in candidates)


def reason(
    provider: Any, connection: Any, attempt: Mapping[str, Any], settled: int | None
) -> str | None:
    """The closed reason when this attempt qualifies for a fallback, else None.

    Qualifies only when it settled at exactly zero and the provider proves, from its own
    records, that nothing started; a reason outside ``REASONS`` never qualifies."""
    if settled is None or int(settled) != 0:
        return None
    hook = getattr(provider, "prestart_failure", None)
    if hook is None:
        return None
    try:
        code = hook(connection, dict(attempt))
    except Exception:  # noqa: BLE001 - unreadable evidence is not proof: no fallback
        return None
    return code if code in REASONS else None


def payload(
    attempt: Mapping[str, Any],
    provider_id: str,
    class_index: int,
    code: str,
    path: str,
    used: int,
    limit: int,
) -> dict[str, Any]:
    """The event payload: closed codes and counts only, never free text."""
    return {
        "stage_key": attempt["stage_key"],
        "attempt": attempt["attempt"],
        "attempt_id": attempt["id"],
        "provider_id": provider_id,
        "class_index": class_index,
        "reason": code,
        "path": path,
        "settled_cents": 0,
        "class_fallbacks_used": used,
        "class_fallbacks_max": limit,
        "is_evidence": False,
    }
