"""Capacity-aware placement of campaign stages on approved provider candidates (ADR 0060).

A stage names its own provider and, optionally, ``alternate_providers`` in order. When the
stage is ready for a new attempt the runner asks ``decide`` which candidate to use. Placement
never adds a destination: it only chooses among the candidates the approved spec names, after
an agent's steering (pin or exclude, a filter over that list), and every check it makes is an
existing one (the provider registry, ``check_stage`` enrollment,
``CampaignStore.check_stage_inputs`` trust, the campaign budget cap, the program budget, the
project limits, open provider exposure and the provider's own slot count). The first candidate
that passes all of them is chosen; when none does the stage waits, or fails exactly as a
single-provider stage would when no candidate can ever pass. The decision, with the reason each
other candidate was skipped, is recorded as a ``campaign.stage_placed`` event and copied into
the receipt.

A stage with no alternates is never routed through ``decide``: it keeps its single-provider
behaviour, events and receipts unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import program_budget
from .campaign_models import NormalStage, _validate_provider, stage_candidates, stage_provider
from .store import Conflict

PLACED_EVENT = "campaign.stage_placed"
WAITING_EVENT = "campaign.stage_waiting_placement"
STEERED_EVENT = "campaign.placement_steered"

# Reasons a candidate is skipped. A transient reason can clear without a person (a slot frees,
# a hold settles); a permanent one cannot during this attempt.
TRANSIENT_REASONS = frozenset(
    {"no_free_slot", "project_limits", "exposure_open", "program_budget", "budget_in_flight"}
)
PERMANENT_REASONS = frozenset(
    {
        "excluded_by_steering",
        "not_pinned",
        "unregistered",
        "provider_invalid",
        "not_enrolled",
        "untrusted",
        "over_run_cap",
        "over_campaign_budget",
        "plan_mismatch",
    }
)


@dataclass(frozen=True)
class Skip:
    provider_id: str
    reason: str
    detail: str = ""

    def payload(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class Decision:
    candidates: tuple[str, ...]
    chosen: str | None
    skipped: tuple[Skip, ...] = ()
    steering: dict[str, Any] | None = None
    hold_cents: int | None = None
    # Candidates whose checks were not run because an earlier one was chosen.
    not_evaluated: tuple[str, ...] = field(default=())
    # Chosen only to record the campaign budget refusal no candidate could avoid.
    budget_refusal: bool = False

    @property
    def waiting(self) -> bool:
        return self.chosen is None and any(s.reason in TRANSIENT_REASONS for s in self.skipped)

    def payload(self, stage_key: str, attempt: int) -> dict[str, Any]:
        return {
            "stage_key": stage_key,
            "attempt": attempt,
            "candidates": list(self.candidates),
            "provider_id": self.chosen,
            "hold_cents": self.hold_cents,
            "skipped": [item.payload() for item in self.skipped],
            "not_evaluated": list(self.not_evaluated),
            "steering": self.steering,
            "budget_refusal": self.budget_refusal,
        }


# -- steering ------------------------------------------------------------------------------


def _latest(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], event_type: str, stage_key: str
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.stage_key')=? ORDER BY cursor DESC LIMIT 1""",
        (campaign["project_id"], event_type, campaign["id"], stage_key),
    ).fetchone()


def steering_for(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> dict[str, Any] | None:
    """The latest steering recorded for a stage (it replaces earlier ones), or None."""
    row = _latest(connection, campaign, STEERED_EVENT, stage_key)
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    return {"pin": payload.get("pin"), "exclude": list(payload.get("exclude") or [])}


def steered_skip(provider_id: str, steering: Mapping[str, Any] | None) -> Skip | None:
    if not steering:
        return None
    if provider_id in (steering.get("exclude") or ()):
        return Skip(provider_id, "excluded_by_steering")
    pin = steering.get("pin")
    if pin is not None and provider_id != pin:
        return Skip(provider_id, "not_pinned", f"pinned to {pin}")
    return None


def allowed_after_steering(
    candidate_ids: tuple[str, ...], pin: str | None, exclude: list[str]
) -> list[str]:
    steering = {"pin": pin, "exclude": exclude}
    return [pid for pid in candidate_ids if steered_skip(pid, steering) is None]


# -- capacity --------------------------------------------------------------------------------


def project_limits_allow(connection: sqlite3.Connection, project_id: str, hold: int) -> bool:
    """The project's concurrency and monetary limits admit one more grant of ``hold``."""
    limits = connection.execute(
        "SELECT * FROM execution_project_limits WHERE project_id=?", (project_id,)
    ).fetchone()
    if limits is None:
        return True  # the store refuses with a precise reason
    unresolved = connection.execute(
        """SELECT count(*) AS n FROM execution_grants WHERE project_id=?
        AND status IN ('active','cancel_requested')""",
        (project_id,),
    ).fetchone()
    return bool(
        unresolved["n"] < limits["max_concurrency"]
        and limits["held_cents"] + limits["settled_cents"] + hold <= limits["monetary_limit_cents"]
    )


def placed_not_granted(rt: Any, provider_id: str) -> int:
    """Attempts already placed on a provider that do not hold a grant yet (any campaign).

    They will take a slot as soon as their grant is issued, so placement counts them against
    the provider's free slots and does not stack a second stage behind them."""
    row = rt.one(
        """SELECT count(*) AS n FROM campaign_stage_attempts
        WHERE provider_id=? AND status='reserved' AND grant_id IS NULL""",
        (provider_id,),
    )
    return int(row["n"]) if row else 0


def free_slots(rt: Any, ctx: Any, provider: Any, stage: NormalStage) -> int | None:
    """Free slots on a provider, or None when the provider does not bound them itself."""
    count = getattr(provider, "free_slots", None)
    if count is not None:
        return int(count(rt, ctx, stage))
    probe = getattr(provider, "slot_free", None)
    if probe is not None:
        return 1 if probe(rt, ctx, stage) else 0
    return None


# -- the decision ----------------------------------------------------------------------------


def _check(
    rt: Any,
    ctx: Any,
    base: NormalStage,
    stage: NormalStage,
    exposure_open: Callable[[sqlite3.Connection], bool],
) -> Skip | None:
    """The first existing check this candidate fails, or None when it may run now."""
    from .campaign_store import CampaignStore

    pid = stage.provider
    campaign = ctx.campaign
    try:
        provider = stage_provider(pid)
    except KeyError:
        return Skip(pid, "unregistered", "not in the provider registry")
    try:
        _validate_provider(stage)
    except ValueError as error:
        return Skip(pid, "provider_invalid", str(error)[:300])
    hold = stage.max_exposure_cents
    with rt.connection() as connection:
        try:
            provider.check_stage(connection, campaign["project_id"], stage)
        except Conflict as error:
            return Skip(pid, "not_enrolled", str(error)[:300])
        try:
            CampaignStore.check_stage_inputs(
                connection,
                stage,
                provider,
                approval_digest=(ctx.approval or {}).get("trust_policy_digest"),
            )
        except Conflict as error:
            return Skip(pid, "untrusted", str(error)[:300])
        if hold > base.max_exposure_cents:
            return Skip(pid, "over_run_cap", f"{hold} > {base.max_exposure_cents}")
        current = connection.execute(
            "SELECT budget_cap_cents, held_cents, settled_cents FROM campaigns WHERE id=?",
            (campaign["id"],),
        ).fetchone()
        committed = current["held_cents"] + current["settled_cents"]
        if committed + hold > current["budget_cap_cents"]:
            # Holds in flight may settle below their estimate and free budget; nothing held
            # means nothing can come back during this attempt.
            reason = "budget_in_flight" if current["held_cents"] > 0 else "over_campaign_budget"
            return Skip(pid, reason, f"{committed} + {hold} > {current['budget_cap_cents']}")
        if hold > 0:
            refusal = program_budget.check_admission(connection, campaign["project_id"], 0)
            if refusal is not None:
                return Skip(pid, "program_budget", refusal)
            if exposure_open(connection):
                return Skip(pid, "exposure_open", "an attempt's provider exposure is unresolved")
        if not project_limits_allow(connection, campaign["project_id"], hold):
            return Skip(pid, "project_limits", "project concurrency or monetary limit")
    slots = free_slots(rt, ctx, provider, stage)
    if slots is not None:
        pending = placed_not_granted(rt, pid)
        if slots - pending <= 0:
            return Skip(pid, "no_free_slot", f"{slots} free, {pending} placed awaiting a grant")
    return None


def decide(
    rt: Any,
    ctx: Any,
    base: NormalStage,
    exposure_open: Callable[[sqlite3.Connection], bool],
    extra_skip: Mapping[str, Skip] | None = None,
) -> Decision:
    """Choose the first approved candidate every existing check admits now (deterministic:
    the same recorded state gives the same choice)."""
    with rt.connection() as connection:
        steering = steering_for(connection, ctx.campaign, base.key)
    candidates = stage_candidates(base)
    ids = tuple(stage.provider for stage in candidates)
    skipped: list[Skip] = []
    for index, stage in enumerate(candidates):
        skip = (
            steered_skip(stage.provider, steering)
            or (extra_skip or {}).get(stage.provider)
            or _check(rt, ctx, base, stage, exposure_open)
        )
        if skip is None:
            return Decision(
                ids,
                stage.provider,
                tuple(skipped),
                steering,
                stage.max_exposure_cents,
                ids[index + 1 :],
            )
        skipped.append(skip)
    return Decision(ids, None, tuple(skipped), steering)


def failure_reason(decision: Decision) -> str:
    """The stage failure for a decision no candidate can ever pass, in the single-provider
    vocabulary where one applies."""
    reasons = {item.reason for item in decision.skipped}
    if reasons <= {"untrusted", "excluded_by_steering", "not_pinned"} and "untrusted" in reasons:
        return "input_not_admitted"
    if reasons <= {"unregistered", "provider_invalid", "excluded_by_steering", "not_pinned"}:
        return "provider_unavailable"
    return "no_eligible_provider"


# -- records ---------------------------------------------------------------------------------


def recorded(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str, attempt: int
) -> dict[str, Any] | None:
    """The placement recorded for one attempt number of a stage, or None."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.stage_key')=?
        AND json_extract(payload_json,'$.attempt')=? ORDER BY cursor DESC LIMIT 1""",
        (campaign["project_id"], PLACED_EVENT, campaign["id"], stage_key, attempt),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def placements(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> list[dict]:
    """Every recorded placement of a campaign, oldest first (status and receipts)."""
    rows = connection.execute(
        """SELECT payload_json, created_at FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (campaign["project_id"], PLACED_EVENT, campaign["id"]),
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload.pop("campaign_id", None)
        out.append({**payload, "recorded_at": row["created_at"]})
    return out


def status_view(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> list[dict]:
    """Per stage with candidates: the candidates, current steering, every recorded placement
    and, while it waits, the latest reasons. Read-only."""
    from .campaign_models import normalize, parse_spec_json

    try:
        normal = normalize(parse_spec_json(campaign["spec_json"]))
    except Exception:  # noqa: BLE001 - a status read never fails on a stored spec
        return []
    recorded_all = placements(connection, campaign)
    view = []
    for stage in normal.stages:
        if not stage.alternates:
            continue
        waiting = _latest(connection, campaign, WAITING_EVENT, stage.key)
        mine = [item for item in recorded_all if item.get("stage_key") == stage.key]
        last_wait = None if waiting is None else json.loads(waiting["payload_json"])
        if last_wait is not None and mine and mine[-1]["attempt"] >= last_wait.get("attempt", 0):
            last_wait = None  # placed since it last waited
        view.append(
            {
                "stage_key": stage.key,
                "candidates": list(stage.candidate_ids),
                "steering": steering_for(connection, campaign, stage.key),
                "placements": mine,
                "waiting": None
                if last_wait is None
                else {"attempt": last_wait.get("attempt"), "skipped": last_wait.get("skipped")},
            }
        )
    return view
