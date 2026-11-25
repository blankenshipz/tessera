"""Allocation instability per GPU class and the next attempt's fallback (ADR 0148).

A rented GPU route can lose its instance shortly after it started: the provider reallocates the
running container onto another node. The control plane already records that on its own route
state (``remote_execution.state``: ``reallocated``, each provider readback with its time and
allocation id, and the selection's ``gpu_class``), and the guardian's final status for the attempt
records ``allocation_uncertain``. This module reads that evidence, counts the routes of a GPU class
whose allocation changed within ``window_seconds`` of first running, and, once a class reaches the
operator's ``threshold``, lets placement try an eligible candidate on a different class first for
the *next* attempt.

It never adds a destination or authority. Only candidates the approved spec lists may move, every
existing placement check still runs on each (ADR 0060: trust and the ADR 0040 data-class guard,
licence labels, the per-run cap, budgets, capacity), a third-party candidate must be covered by
the stage's own review pre-approval naming it (ADR 0066), and a placement already recorded for an
attempt number stays bound (an attempt in flight is never moved). When no candidate qualifies the
order is unchanged and the record says so (``allocation_unstable_no_alternate``).

The strict rule (ADR 0169): the reorder applies only while no earlier attempt of the stage may
have started (``may_reorder``: none, or only attempts recorded as never started), so a stage
never changes class after a possible start or spend; a retry stays where it ran.

The section ``placement_preferences.allocation_instability`` of the operator's provider file turns
it on; without it nothing is read, reordered or recorded.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

EVENT_ROUTE_STATE = "remote_execution.state"
# Closed outcome set recorded on the placement and the receipt.
FALLBACK = "allocation_unstable_fallback"
NO_ALTERNATE = "allocation_unstable_no_alternate"
OUTCOMES = frozenset({FALLBACK, NO_ALTERNATE})
# Closed reasons a candidate is not an instability fallback.
REFUSALS = frozenset(
    {
        "same_class_unstable",
        "needs_approval",
        "over_run_cap",
        "not_a_placement_candidate",
        "excluded",
    }
)
# Evidence sources for one unstable route.
SOURCE_ROUTE = "route_reallocated"
SOURCE_GUARDIAN = "guardian_allocation_uncertain"
# Attempts recorded as provably never started (ADR 0046, ADR 0060 addendum): only these leave a
# stage free to change class.
NEVER_STARTED = frozenset({"not_started", "prestart_no_spend"})
# Bound on how many route-state rows one evaluation reads.
MAX_ROWS = 2000


class AllocationInstability(BaseModel):
    """Operator setting (closed, bounded). Its presence is the opt-in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # An allocation change this soon after the route first ran counts as instability.
    window_seconds: Annotated[int, Field(strict=True, ge=60, le=6 * 3600)] = 900
    # Unstable routes of one GPU class (in the project, within the lookback) before the next
    # attempt prefers a different class.
    threshold: Annotated[int, Field(strict=True, ge=1, le=10)] = 2
    # How far back routes count.
    lookback_seconds: Annotated[int, Field(strict=True, ge=3600, le=7 * 86_400)] = 86_400


def setting() -> AllocationInstability | None:
    """The installed operator setting, or None (the default: off)."""
    from . import campaign_placement_preference

    prefs = campaign_placement_preference.active()
    return None if prefs is None else prefs.allocation_instability


# -- evidence ----------------------------------------------------------------------------------


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def route_class(state: Mapping[str, Any]) -> str | None:
    resources = ((state.get("proposal") or {}).get("selection") or {}).get("resources") or {}
    value = resources.get("gpu_class")
    return value if isinstance(value, str) and value else None


def _started(state: Mapping[str, Any]) -> datetime | None:
    """When the route first ran: the first readback that saw it running, else the first
    ``start``/``create`` effect."""
    for receipt in state.get("receipts") or ():
        if receipt.get("state") == "running":
            found = _parse(receipt.get("observed_at"))
            if found is not None:
                return found
    for effect in state.get("effects") or ():
        if effect.get("action") in ("start", "create"):
            found = _parse(effect.get("requested_at"))
            if found is not None:
                return found
    return None


def _reallocated_at(state: Mapping[str, Any]) -> datetime | None:
    """The first readback that showed a different allocation (or the proxy's reallocation
    reason), or None."""
    first = None
    for receipt in state.get("receipts") or ():
        allocation = receipt.get("allocation_id")
        changed = receipt.get("proxy_reason") == "reallocation_uncertain" or (
            first is not None and allocation and allocation != first
        )
        if changed:
            return _parse(receipt.get("observed_at"))
        first = first or allocation
    return None


def observe(
    state: Mapping[str, Any],
    guardian_final: Mapping[str, Any] | None,
    window_seconds: int,
) -> dict[str, Any] | None:
    """One route's instability observation, or None when its allocation never changed.

    ``unstable`` is true only when the change is shown within the window of first running. The
    guardian's final status carries no change time, so its ``observed_at`` is an upper bound:
    within the window by that bound counts, anything else is recorded and not counted."""
    started = _started(state)
    at = None
    source = None
    if state.get("reallocated"):
        source, at = SOURCE_ROUTE, _reallocated_at(state)
    if (at is None) and guardian_final and guardian_final.get("allocation_uncertain") is True:
        source, at = SOURCE_GUARDIAN, _parse(guardian_final.get("observed_at"))
    if source is None:
        return None
    elapsed = None if started is None or at is None else (at - started).total_seconds()
    within = elapsed is not None and 0 <= elapsed <= window_seconds
    return {
        "route_id": state.get("id"),
        "gpu_class": route_class(state),
        "source": source,
        "elapsed_seconds": None if elapsed is None else int(elapsed),
        "window_seconds": window_seconds,
        "unstable": within,
        "timing": "unknown" if elapsed is None else ("within" if within else "outside"),
    }


def _guardian_finals(
    connection: sqlite3.Connection, route_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not route_ids:
        return {}
    marks = ",".join("?" for _ in route_ids)
    rows = connection.execute(
        f"""SELECT route_id, guardian_final_json FROM campaign_salad_attempts
        WHERE route_id IN ({marks}) AND guardian_final_json IS NOT NULL""",
        route_ids,
    ).fetchall()
    found = {}
    for row in rows:
        try:
            value = json.loads(row["guardian_final_json"])
        except ValueError:
            continue
        if isinstance(value, dict):
            found[row["route_id"]] = value
    return found


def _latest_states(
    connection: sqlite3.Connection, org_id: str, project_id: str, since: str
) -> dict[str, dict[str, Any]]:
    """The latest route state of each route in this org and project updated since ``since``."""
    rows = connection.execute(
        """SELECT payload_json FROM events WHERE org_id=? AND project_id=? AND event_type=?
        AND created_at >= ? ORDER BY cursor DESC LIMIT ?""",
        (org_id, project_id, EVENT_ROUTE_STATE, since, MAX_ROWS),
    ).fetchall()
    states: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            state = json.loads(row["payload_json"])
        except ValueError:
            continue
        ident = state.get("id") if isinstance(state, dict) else None
        if (
            isinstance(ident, str)
            and ident not in states
            and state.get("org_id") == org_id
            and state.get("project_id") == project_id
        ):
            states[ident] = state
    return states


def _since(now: datetime, lookback_seconds: int) -> str:
    return (now - timedelta(seconds=lookback_seconds)).isoformat().replace("+00:00", "Z")


def class_counts(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    config: AllocationInstability,
    now: datetime | None = None,
) -> dict[str, int]:
    """Unstable routes per GPU class in one org and project, within the lookback (read-only)."""
    current = now or datetime.now(UTC)
    states = _latest_states(
        connection, org_id, project_id, _since(current, config.lookback_seconds)
    )
    finals = _guardian_finals(connection, list(states))
    counts: dict[str, int] = {}
    for ident, state in states.items():
        seen = observe(state, finals.get(ident), config.window_seconds)
        if seen is None or not seen["unstable"] or seen["gpu_class"] is None:
            continue
        counts[seen["gpu_class"]] = counts.get(seen["gpu_class"], 0) + 1
    return counts


def attempt_entry(
    connection: sqlite3.Connection, attempt: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The receipt's per-attempt observation (only when the setting is on and the attempt's
    route allocation changed)."""
    config = setting()
    if config is None:
        return None
    row = connection.execute(
        "SELECT route_id, guardian_final_json FROM campaign_salad_attempts WHERE attempt_id=?",
        (attempt["id"],),
    ).fetchone()
    if row is None or row["route_id"] is None:
        return None
    found = connection.execute(
        """SELECT payload_json FROM events WHERE event_type=?
        AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC LIMIT 1""",
        (EVENT_ROUTE_STATE, row["route_id"]),
    ).fetchone()
    if found is None:
        return None
    state = json.loads(found["payload_json"])
    if state.get("project_id") != attempt.get("project_id", state.get("project_id")):
        return None
    final = None
    if row["guardian_final_json"] is not None:
        try:
            final = json.loads(row["guardian_final_json"])
        except ValueError:
            final = None
    return observe(state, final if isinstance(final, dict) else None, config.window_seconds)


# -- placement ---------------------------------------------------------------------------------


def candidate_class(stage: Any) -> str | None:
    """The GPU class a candidate would run on, from its provider (``allocation_class``), or
    None (no GPU class, an unregistered provider, or a provider that does not say)."""
    from .campaign_models import stage_provider

    try:
        provider = stage_provider(stage.provider)
    except KeyError:
        return None
    report = getattr(provider, "allocation_class", None)
    if report is None:
        return None
    try:
        value = report(stage)
    except Exception:  # noqa: BLE001 - a candidate that cannot say has no class
        return None
    return value if isinstance(value, str) and value else None


def _covered(connection: sqlite3.Connection, ctx: Any, base: Any, provider_id: str) -> bool:
    """A third-party candidate is a fallback only under the stage's own pre-approval naming it
    (ADR 0066); a candidate that needs no per-attempt review is covered by the approved spec."""
    from . import campaign_preapproval
    from .campaign_models import stage_provider

    try:
        descriptor = stage_provider(provider_id).descriptor
    except KeyError:
        return False
    if not descriptor.third_party:
        return True
    approval = ctx.approval or {}
    if not approval.get("id"):
        return False
    pre = campaign_preapproval.stage_preapproval(connection, approval["id"], base.key)
    if pre is None:
        return False
    return campaign_preapproval.covered_provider(connection, pre, base.provider) == provider_id


def may_reorder(attempts: Any) -> bool:
    """True when no earlier attempt of the stage may have started (strict rule, ADR 0169)."""
    return all(a["failure_class"] in NEVER_STARTED for a in attempts)


def reorder(
    rt: Any,
    ctx: Any,
    base: Any,
    ids: tuple[str, ...],
    blocked: Mapping[str, Any],
    now: datetime | None = None,
) -> tuple[tuple[str, ...], dict[str, Any] | None]:
    """The candidate order for the next attempt and the record (None: setting off, or no
    candidate's class is unstable).

    ``blocked`` maps a candidate to a skip placement already knows (steering, reroute-only):
    such a candidate is never moved forward. When the first candidate's class has reached the
    threshold, the eligible candidates move to the front in their current order; the rest keep
    their positions. Nothing is skipped here: every candidate still meets every check."""
    from .campaign_models import stage_on_candidate

    config = setting()
    if config is None or not ids:
        return ids, None
    campaign = ctx.campaign
    with rt.connection() as connection:
        counts = class_counts(
            connection, campaign["org_id"], campaign["project_id"], config, now=now
        )
    classes = {pid: candidate_class(stage_on_candidate(base, pid)) for pid in ids}
    unstable = {cls for cls, n in counts.items() if n >= config.threshold}
    observed = {
        cls: counts[cls] for cls in sorted({c for c in classes.values() if c} & set(counts))
    }
    head = ids[0]
    if classes[head] is None or classes[head] not in unstable:
        if not observed:
            return ids, None
        return ids, {
            "outcome": None,
            "threshold": config.threshold,
            "window_seconds": config.window_seconds,
            "counts": observed,
            "classes": classes,
        }
    eligible: list[str] = []
    refused: list[dict[str, str]] = []
    with rt.connection() as connection:
        for pid in ids[1:]:
            reason = None
            stage = stage_on_candidate(base, pid)
            if pid in blocked:
                reason = "excluded"
            elif pid not in base.placement_ids:
                reason = "not_a_placement_candidate"
            elif classes[pid] is not None and classes[pid] in unstable:
                reason = "same_class_unstable"
            elif stage.max_exposure_cents > base.max_exposure_cents:
                reason = "over_run_cap"
            elif not _covered(connection, ctx, base, pid):
                reason = "needs_approval"
            if reason is None:
                eligible.append(pid)
            else:
                refused.append({"provider_id": pid, "reason": reason})
    order = (*eligible, *(pid for pid in ids if pid not in eligible))
    return order, {
        "outcome": FALLBACK if eligible else NO_ALTERNATE,
        "from_provider": head,
        "from_class": classes[head],
        "threshold": config.threshold,
        "window_seconds": config.window_seconds,
        "counts": observed,
        "classes": classes,
        "eligible": eligible,
        "refused": refused,
        "order": list(order),
    }


def with_outcome(record: dict[str, Any] | None, chosen: str | None) -> dict[str, Any] | None:
    """Fill in where the attempt went. A fallback whose eligible candidates all failed their
    checks ends on the unstable class again and says so (``allocation_unstable_no_alternate``)."""
    if record is None or record.get("outcome") is None:
        return record
    outcome = FALLBACK if chosen is not None and chosen in record["eligible"] else NO_ALTERNATE
    return {
        **record,
        "outcome": outcome,
        "chosen": chosen,
        "to_class": None if chosen is None else record["classes"].get(chosen),
    }
