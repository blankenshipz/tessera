"""A Salad stage's approved alternate profiles: the one record of every profile move (ADR 0151,
ADR 0060 addendum, strict rule of ADR 0169).

A Salad stage may list ``alternate_profiles`` after its own ``profile`` (the order of
``salad_alternate_profiles.profile_order``). The stage moves to the next listed profile ONLY
before any possible start: an attempt the provider proves never started (no start effect, never
reallocated) and that settled at exactly zero (``campaign_class_fallback``, bounded by
``retry.max_class_fallbacks``). That move is recorded here as one
``campaign.salad_profile_switched`` event (reason ``prestart_no_spend``, ``started`` and
``spent`` false) in the same transaction as the attempt's transition.

After any possible start or spend there is NO profile move, with one evidence-gated exception
(``allocation_timeout_move``, amending ADR 0169 point 3): a CPU stage whose group was created
and started but never given an instance, proven by the guardian's own allocation deadline, a
confirmed stop, clean flags, no billed cost and a zero estimate, plus control's own ``stopped``
guardian read, moves to its next CPU profile (same selector, checks and bound; reason
``allocation_timeout``, ``started`` true, ``spent`` false). The attempt keeps its settlement at
its hold; nothing about settlement changes. When an attempt of a stage with
alternates ended unstable on its own route (ADR 0148: the allocation changed within the window of
first running; ADR 0149: the route was reallocated and the provider confirmed the group stopped),
the stage does not switch: it is held (``blocked_unknown``, the campaign blocks) with one
``campaign.salad_profile_fallback_stopped`` event, outcome ``post_start_switch_refused``, so a
person (or an approved delegation) makes the retry decision. The attempt keeps its own outcome
and settlement path; nothing new is held.

Which attempt runs on which profile is derived only from the immutable switch events: attempt N
runs on the ``to_profile`` of the latest switch on its candidate whose ``to_attempt`` is at most
N, else on the stage's own profile. The ``campaign_salad_stages.profile`` column, which the
executor's work listing reads, follows in the same runner transaction as the switch event; it is
a mirror, never the source.

A pre-start move whose next profile is not covered (``profile_not_approved``, with a detail
code: unavailable, over the per-run cap, a capability declaration that differs, or a review
pre-approval that does not bind that profile's plan) does not happen: the stage is held with one
stop event.

Operator capacity fallback: after the approved order is used up, the operator profile file's
``fallback_profiles`` chain of those profiles (``salad_alternate_profiles.operator_chain``)
supplies further candidates, under exactly the same pre-start trigger and the same
``retry.max_class_fallbacks`` bound. The chain is operator config, so no approved spec or digest
changes. Each candidate is checked at move time against the stage's approved hold
(``SaladRouteProvider.operator_fallback_refusal``: per-run cap, CPU worst case over the stage's
whole duration, per-task ceiling over every allowed attempt, then every submit check) and the
same capability and review pre-approval checks; one that is not covered is skipped (recorded on
the move) and the next is tried; none covered holds the stage (``profile_not_approved``, detail
of the first refusal, for example ``fallback_over_hold``). Every move records its ``source``
(``spec_alternate`` or ``operator_fallback``). A profile neither the approved spec lists nor the
operator chain (or a recorded operator move on that candidate) names is never reachable.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import campaign_allocation_instability as instability
from . import salad_alternate_profiles
from .salad_termination import provider_termination

SWITCH_EVENT = "campaign.salad_profile_switched"
STOP_EVENT = "campaign.salad_profile_fallback_stopped"

# Closed reason set for a switch: the strict rule allows a pre-start move, and one evidence-gated
# exception after a start effect (``allocation_timeout``).
REASON_PRESTART_NO_SPEND = "prestart_no_spend"
# The one post-start exception (amends ADR 0169 point 3): a CPU group that was started but
# never given an instance, proven by the guardian's allocation deadline and a zero estimate.
REASON_ALLOCATION_TIMEOUT = "allocation_timeout"
REASONS = frozenset({REASON_PRESTART_NO_SPEND, REASON_ALLOCATION_TIMEOUT})
# Closed set of post-start triggers that are recorded and refused (never a switch).
TRIGGER_ALLOCATION_UNSTABLE = "allocation_unstable"
TRIGGER_TERMINATION_CONFIRMED = "termination_confirmed_unstable"
POST_START_TRIGGERS = frozenset({TRIGGER_ALLOCATION_UNSTABLE, TRIGGER_TERMINATION_CONFIRMED})

# Closed outcome set when a move does not happen.
PROFILE_NOT_APPROVED = "profile_not_approved"
POST_START_REFUSED = "post_start_switch_refused"
OUTCOMES = frozenset({PROFILE_NOT_APPROVED, POST_START_REFUSED})
# Closed detail codes of ``profile_not_approved``.
NOT_APPROVED_DETAILS = frozenset(
    {
        "profile_unavailable",
        "over_run_cap",
        "capability_mismatch",
        "preapproval_not_covering",
        # Operator capacity fallback only: its worst case exceeds the stage's approved hold or
        # its per-task ceiling, or it fails another check an approved profile passes.
        "fallback_over_hold",
        "fallback_refused",
    }
)
SOURCE_SPEC = salad_alternate_profiles.SOURCE_SPEC
SOURCE_OPERATOR = salad_alternate_profiles.SOURCE_OPERATOR
# Every outcome holds the stage for a person (the campaign blocks).
HOLDING = OUTCOMES

# The runner's verdict for the latest failed attempt.
HOLD = "hold"


@dataclasses.dataclass(frozen=True)
class Verdict:
    action: str | None  # HOLD, or None (the existing behavior applies)
    outcome: str | None = None


# -- the approved order and the stage as it runs on a profile ----------


def _config(stage: Any) -> Any | None:
    """The stage's Salad block (any object with ``profile``), or None for a non-Salad stage."""
    config = getattr(stage, "provider_config", None)
    if isinstance(config, Mapping):
        return _Block(config) if config.get("profile") else None
    found = getattr(stage, "salad", None)
    return found if getattr(found, "profile", None) else None


class _Block:
    """Read-only view of a provider_config dict with the two fields the order needs."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.profile = config.get("profile")
        self.alternate_profiles = config.get("alternate_profiles") or ()


def has_alternates(stage: Any) -> bool:
    """Whether the approved stage lists alternate profiles (old single-profile stages: False)."""
    config = _config(stage)
    return config is not None and len(salad_alternate_profiles.profile_order(config)) > 1


def order(stage: Any) -> tuple[str, ...]:
    config = _config(stage)
    return () if config is None else salad_alternate_profiles.profile_order(config)


def _operator_profiles(stage: Any) -> Any:
    """The operator profile file of the stage's registered provider, or None."""
    from .campaign_models import stage_provider

    try:
        return getattr(stage_provider(stage.provider), "profiles", None)
    except (KeyError, AttributeError):
        return None


def operator_order(stage: Any) -> tuple[str, ...]:
    """The operator capacity fallbacks of the approved stage on its candidate, in order, after
    its approved order (empty unless the operator file names ``fallback_profiles``)."""
    config = _config(stage)
    if config is None:
        return ()
    return salad_alternate_profiles.operator_chain(config, _operator_profiles(stage))


def has_fallbacks(stage: Any) -> bool:
    """Whether the stage may move to another profile before any possible start: it lists
    alternates, or the operator file chains fallbacks after its profiles."""
    return has_alternates(stage) or bool(operator_order(stage))


def _move_bound(stage: Any) -> int:
    """The approved stage's ``retry.max_class_fallbacks`` (0 when unreadable: no move)."""
    try:
        return int(stage.retry.max_class_fallbacks)
    except (AttributeError, TypeError, ValueError):
        return 0


def reads_fallback(stage: Any) -> bool:
    """Whether a failure of the approved stage is read by the profile fallback rules (the
    pre-start move and the post-start strict rule).

    A stage whose approved spec lists alternates always is (exactly as before operator chains
    existed). An operator ``fallback_profiles`` chain counts only when the approved
    ``retry.max_class_fallbacks`` allows at least one move: a stage that can never use a
    fallback keeps the single-profile behavior, whatever the operator file says, so an operator
    setting never changes how an approved spec's failures are handled."""
    if has_alternates(stage):
        return True
    return _move_bound(stage) >= 1 and bool(operator_order(stage))


def _operator_targets(recorded: list[dict[str, Any]]) -> tuple[str, ...]:
    """Profiles recorded operator moves (``source: operator_fallback``) went to."""
    return tuple(
        str(item["to_profile"]) for item in recorded if item.get("source") == SOURCE_OPERATOR
    )


def on_profile(stage: Any, profile: str, operator: tuple[str, ...] = ()) -> Any:
    """The approved stage as it runs on one of its listed profiles, or on one of ``operator``
    (operator fallback profiles the caller took from the chain or a recorded operator move).

    The stage's own profile returns the stage unchanged. Any other profile is a programming
    error (KeyError): nothing outside the approved list and the given operator set is
    reachable."""
    listed = order(stage)
    if not listed or profile == listed[0]:
        return stage
    if profile not in listed and profile in operator:
        return salad_alternate_profiles.stage_on_operator_profile(stage, profile)
    if profile not in listed:
        raise KeyError(f"Stage {getattr(stage, 'key', '?')} lists no profile {profile!r}")
    derive = getattr(salad_alternate_profiles, "stage_on_profile", None)
    if derive is not None:
        return derive(stage, profile)
    config = dict(stage.provider_config)
    config["profile"] = profile
    # The order is always read from the approved stage, never from a switched copy.
    config.pop("alternate_profiles", None)
    return dataclasses.replace(stage, provider_config=config)


# -- the recorded switches ----------


def switches(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage_key: str,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every recorded switch of the stage, in order (tenant-scoped to the campaign); with
    ``provider_id``, only the switches made on that candidate (profiles belong to it)."""
    rows = connection.execute(
        """SELECT payload_json FROM events WHERE event_type=? AND org_id=? AND project_id=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.stage_key')=? ORDER BY cursor""",
        (SWITCH_EVENT, campaign["org_id"], campaign["project_id"], campaign["id"], stage_key),
    ).fetchall()
    found = [json.loads(row["payload_json"]) for row in rows]
    if provider_id is None:
        return found
    return [item for item in found if item.get("provider_id") in (None, provider_id)]


def stopped(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type=? AND org_id=? AND project_id=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.stage_key')=? ORDER BY cursor DESC LIMIT 1""",
        (STOP_EVENT, campaign["org_id"], campaign["project_id"], campaign["id"], stage_key),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def profile_for(recorded: list[dict[str, Any]], primary: str, attempt: int | None = None) -> str:
    """The profile attempt ``attempt`` runs on (None: the stage's current, next-attempt one)."""
    chosen = primary
    for item in recorded:
        if attempt is None or int(item["to_attempt"]) <= attempt:
            chosen = item["to_profile"]
    return chosen


def tried(recorded: list[dict[str, Any]], primary: str) -> tuple[str, ...]:
    """The profiles the stage already used on one candidate: its own, then every switch target.
    ``next_profile(order, tried(...))`` is the one selector of the next profile."""
    return tuple(dict.fromkeys((primary, *(str(item["to_profile"]) for item in recorded))))


def current_stage(rt: Any, ctx: Any, key: str, stage: Any) -> Any:
    """``stage`` (the approved stage on its placed candidate) on the profile it runs on now."""
    listed = order(stage)
    chain = operator_order(stage) if listed else ()
    if len(listed) < 2 and not chain:
        return stage
    with rt.store.runner_connection() as connection:
        recorded = switches(connection, ctx.campaign, key, stage.provider)
    profile = profile_for(recorded, listed[0])
    try:
        return on_profile(stage, profile, chain + _operator_targets(recorded))
    except KeyError:  # the stage moved to a candidate that does not list it: its own profile
        return stage


def candidate_profile(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], key: str, stage: Any
) -> str | None:
    """The profile the stage's next attempt runs on when placed on ``stage``'s candidate (None
    for a stage without alternates or operator fallbacks), from that candidate's recorded
    switches only."""
    listed = order(stage)
    if len(listed) < 2 and not (listed and operator_order(stage)):
        return None
    return profile_for(switches(connection, campaign, key, stage.provider), listed[0])


def attempt_class(connection: sqlite3.Connection, admitted_attempt_id: str) -> str | None:
    """The Salad profile (one GPU class) an admitted research attempt ran on, or None when it
    is not a Salad campaign attempt (or it ran on a candidate that is not Salad). Read from the
    campaign's own records: the approved spec and the switch events of the attempt's stage and
    candidate, else the stage's recorded profile."""
    try:
        row = connection.execute(
            """SELECT a.campaign_id, a.stage_key, a.attempt, a.provider_id, c.org_id,
            c.project_id, c.spec_json, s.profile FROM campaign_stage_attempts a
            JOIN campaigns c ON c.id = a.campaign_id
            JOIN campaign_salad_stages s ON s.campaign_id = a.campaign_id
            AND s.stage_key = a.stage_key WHERE a.admitted_attempt_id=? LIMIT 1""",
            (admitted_attempt_id,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        return None  # a store without campaign tables has no Salad attempt
    if row is None:
        return None
    campaign = {"id": row["campaign_id"], "org_id": row["org_id"], "project_id": row["project_id"]}
    placed = _placed_stage(row)
    if placed is not None:
        spec_stage, candidate = placed
        listed = order(candidate)
        if not listed:
            return None  # placed on a candidate that is not Salad: no GPU class of a profile
        # A single-profile candidate reports its own profile unless an operator fallback moved
        # it (then the recorded switches name the profile each attempt ran on).
        return attempt_profile(connection, campaign, spec_stage, dict(row)) or listed[0]
    recorded = switches(connection, campaign, row["stage_key"])
    if row["provider_id"]:
        recorded = [i for i in recorded if i.get("provider_id") in (None, row["provider_id"])]
    if not recorded:
        return None if row["profile"] is None else str(row["profile"])
    return profile_for(recorded, str(recorded[0]["from_profile"]), int(row["attempt"]))


def _placed_stage(row: Mapping[str, Any]) -> tuple[Any, Any] | None:
    """(approved stage, the stage on the attempt's candidate) from the campaign's approved
    spec, or None when the spec cannot be read (the recorded rows are then the only source)."""
    from .campaign_models import normalize, parse_spec_json, stage_on_candidate

    try:
        spec_stage = normalize(parse_spec_json(row["spec_json"])).stage(row["stage_key"])
        pid = row["provider_id"] or spec_stage.provider
        if pid == spec_stage.provider:
            return spec_stage, spec_stage
        return spec_stage, stage_on_candidate(spec_stage, pid)
    except Exception:  # noqa: BLE001 - an unreadable spec falls back to the recorded rows
        return None


# -- the post-start trigger (recorded and refused) ----------


def _route_state(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], route_id: str | None
) -> dict[str, Any] | None:
    if route_id is None:
        return None
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type='remote_execution.state'
        AND org_id=? AND project_id=? AND json_extract(payload_json,'$.id')=?
        ORDER BY cursor DESC LIMIT 1""",
        (campaign["org_id"], campaign["project_id"], route_id),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def unstable_reason(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], attempt: Mapping[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Why this ended attempt counts as unstable on its profile, with its evidence, or None.

    Only a Salad run whose verdict is ``failed`` qualifies (its termination was confirmed: ADR
    0149 never records a verdict for an unconfirmed reallocated route), and only when its own
    route shows the allocation change. Such a run started (or may have): it never moves."""
    if attempt.get("status") != "failed_permanent" or attempt.get("salad_verdict") != "failed":
        return None
    row = connection.execute(
        "SELECT route_id, guardian_final_json FROM campaign_salad_attempts WHERE attempt_id=?",
        (attempt["id"],),
    ).fetchone()
    if row is None:
        return None
    state = _route_state(connection, campaign, row["route_id"])
    if state is None:
        return None
    final = None
    if row["guardian_final_json"] is not None:
        try:
            final = json.loads(row["guardian_final_json"])
        except ValueError:
            final = None
    config = instability.setting() or instability.AllocationInstability()
    observed = instability.observe(
        state, final if isinstance(final, dict) else None, config.window_seconds
    )
    evidence: dict[str, Any] = {"route_id": state.get("id")}
    if observed is not None:
        evidence["allocation_instability"] = observed
    if observed is not None and observed.get("unstable") is True:
        return TRIGGER_ALLOCATION_UNSTABLE, evidence
    reading = provider_termination(state)
    if reading.confirmed:
        evidence["salad_termination"] = reading.as_dict()
        return TRIGGER_TERMINATION_CONFIRMED, evidence
    return None


# -- coverage of the next profile ----------


def not_covered(
    rt: Any,
    ctx: Any,
    key: str,
    stage: Any,
    alternate: Any,
    connection: sqlite3.Connection,
    operator: bool = False,
) -> str | None:
    """A detail code when the next profile may not run without a new human step, else None.

    ``stage`` is the approved stage on its current profile, ``alternate`` the same stage on the
    next profile; reads run in ``connection`` (the caller's transaction). Fails closed: anything
    that cannot be shown covered is not.

    ``operator``: ``alternate`` runs on an operator fallback profile (not in the approved spec).
    It must first pass ``SaladRouteProvider.operator_fallback_refusal`` (the approved hold and
    every submit check); under a review pre-approval it is covered only when the pre-approval
    binds exactly its plan digest (``_operator_preapproval_covers``)."""
    from . import campaign_preapproval
    from .campaign_models import stage_provider
    from .campaign_steps import StepFailed
    from .salad_campaign_profiles import capability_declaration
    from .store import canonical_digest

    try:
        provider = stage_provider(alternate.provider)
        if operator:
            refusal = provider.operator_fallback_refusal(
                connection, ctx.campaign["project_id"], alternate
            )
            if refusal is not None:
                return refusal
        driver = provider.driver(rt)
        profile = driver.profile(alternate)
        config = _salad_block(alternate)
    except (KeyError, AttributeError, StepFailed):
        return "profile_unavailable"
    # The same per-run cap: a profile move keeps the stage's approved ``max_exposure_cents``
    # (``stage_on_profile`` changes the profile only), so the alternate's hold is the stage's.
    # What can change after approval is the operator profile's own run cap: re-checked now.
    if int(alternate.max_exposure_cents) > int(profile.max_run_cents):
        return "over_run_cap"
    row = connection.execute(
        """SELECT p.declaration_digest FROM campaign_salad_stages s
        JOIN capability_policies p ON p.id = s.capability_id
        WHERE s.campaign_id=? AND s.stage_key=?""",
        (ctx.campaign["id"], key),
    ).fetchone()
    if row is not None:
        # The stage's capability authority is bound to its task plan; a profile whose
        # declaration differs would need a plan revision, which a fallback never makes.
        wanted = canonical_digest(capability_declaration(profile, config.workload, alternate.image))
        if wanted != row["declaration_digest"]:
            return "capability_mismatch"
    approval = ctx.approval or {}
    pre = (
        campaign_preapproval.stage_preapproval(connection, approval["id"], key)
        if approval.get("id")
        else None
    )
    if pre is None:
        return None
    if operator:
        if not _operator_preapproval_covers(connection, rt, ctx, key, pre, alternate, profile):
            return "preapproval_not_covering"
    elif not _preapproval_covers(connection, ctx, key, pre, alternate):
        return "preapproval_not_covering"
    return None


def _operator_preapproval_covers(
    connection: sqlite3.Connection,
    rt: Any,
    ctx: Any,
    key: str,
    pre: dict[str, Any],
    alternate: Any,
    profile: Any,
) -> bool:
    """Conservative coverage of an operator fallback by a review pre-approval (ADR 0044).

    A pre-approval binds plan digests a person saw; an operator fallback was never shown. It is
    covered only when every one of these holds, else the stage is held for a person:

    - the stage is on its own provider and the entry covers that provider;
    - the entry records no per-profile digests (a candidate with spec alternates binds per
      profile, and an unlisted profile would be declined at review time, ``wrong_profile``);
    - the fallback's CPU worst case and the stage's hold are within the entry's
      ``max_attempt_cents``, and every allowed attempt's hold within its ``max_total_cents``;
    - the plan digest the runner will propose on the fallback (the driver's own preview on the
      stage on that profile) is exactly the entry's ``plan_digest``."""
    import dataclasses

    from . import campaign_preapproval
    from .campaign_models import stage_provider

    try:
        own = ctx.spec.stage(key).provider
        if alternate.provider != own:
            return False
        if campaign_preapproval.covered_provider(connection, pre, own) != own:
            return False
        if campaign_preapproval.recorded_profiles(connection, pre) is not None:
            return False
        hold = int(alternate.max_exposure_cents)
        ceiling = int(pre["max_attempt_cents"])
        if hold > ceiling or profile.estimated_cents(int(alternate.max_duration_seconds)) > ceiling:
            return False
        if hold * int(alternate.retry.hold_attempts) > int(pre["max_total_cents"]):
            return False
        from .campaign_runner import _Ctx

        spec = ctx.spec
        shifted = dataclasses.replace(
            spec,
            stages=tuple(alternate if item.key == key else item for item in spec.stages),
        )
        driver = stage_provider(alternate.provider).driver(rt)
        digests = driver.preview_review_digests(_Ctx(ctx.campaign, shifted, None, 0), key, None)
        return digests.get("plan_digest") == pre["plan_digest"]
    except Exception:  # noqa: BLE001 - anything that cannot be shown covered is not
        return False


def _preapproval_covers(
    connection: sqlite3.Connection, ctx: Any, key: str, pre: dict[str, Any], alternate: Any
) -> bool:
    """The stage resolves its reviews under a pre-approval (ADR 0044/0066): the move goes
    ahead without a person only when that entry names the candidate the stage is placed on and
    binds this profile's plan digest up front (ADR 0151, recorded on the approve event). A stage
    without a pre-approval needs no such binding: a person reviews every attempt anyway."""
    from . import campaign_preapproval

    own = ctx.spec.stage(key).provider
    if campaign_preapproval.covered_provider(connection, pre, own) != alternate.provider:
        return False
    recorded = campaign_preapproval.recorded_profiles(connection, pre) or ()
    profile = _config(alternate).profile
    return any(item.get("profile") == profile and item.get("plan_digest") for item in recorded)


def _salad_block(stage: Any) -> Any:
    from .campaign_salad import salad_config

    return salad_config(stage)


# -- the decisions and their record ----------


def after_failure(
    rt: Any,
    ctx: Any,
    key: str,
    approved: Any,
    attempt: Mapping[str, Any],
    connection: sqlite3.Connection,
) -> Verdict:
    """What follows the latest ``failed_permanent`` attempt of a stage with alternates.

    The strict rule: a run that ended unstable on its own route started (or may have), so the
    stage never moves to another profile. It is held with one stop event
    (``post_start_switch_refused``) for an explicit retry decision. A pre-start move that was
    refused (``profile_not_approved``, recorded with the attempt's transition) holds it too.
    Anything else keeps the existing behavior. Idempotent: a recorded stop is re-applied, never
    recorded twice.

    Runs in ``connection``, the caller's runner transaction: the caller applies the verdict
    (the stage held or failed, the campaign blocked) in that same transaction, so the stop
    event and the stage's state change commit together or not at all."""
    listed = order(approved)
    if not reads_fallback(approved):
        return Verdict(None)
    campaign = ctx.campaign
    number = int(attempt["attempt"])
    stop = stopped(connection, campaign, key)
    if stop is not None and int(stop["attempt"]) == number:
        return Verdict(HOLD if stop["outcome"] in HOLDING else None, stop["outcome"])
    found = unstable_reason(connection, campaign, dict(attempt))
    if found is None:
        return Verdict(None)
    recorded = switches(connection, campaign, key, approved.provider)
    trigger, evidence = found
    settled = attempt.get("settled_cents")
    payload = {
        "from_profile": profile_for(recorded, listed[0], number),
        "reason": trigger,
        "order": list(listed),
        "tried": list(tried(recorded, listed[0])),
        "evidence": evidence,
        "attempt_id": attempt.get("id"),
        # The run reached its route: a start (and so spend) is possible. ``spent`` is the
        # settled amount's sign when settled, else null (not yet known, never assumed zero).
        "started": True,
        "spent": None if settled is None else int(settled) > 0,
        "outcome": POST_START_REFUSED,
        "to_profile": None,
        "detail": None,
    }
    # A runner that lost its lease records nothing (as for a switch).
    rt._hold_lease(connection)
    rt._emit_once_in(connection, ctx, STOP_EVENT, {"stage_key": key, "attempt": number}, payload)
    return Verdict(HOLD, POST_START_REFUSED)


class _Order:
    """``next_profile`` input for an already-computed approved order."""

    def __init__(self, listed: tuple[str, ...]) -> None:
        self.profile = listed[0]
        self.alternate_profiles = listed[1:]


# Outcomes of ``prestart_move``.
MOVED = "moved"
NO_PROFILE_LEFT = "no_profile_left"


def prestart_move(
    rt: Any,
    ctx: Any,
    key: str,
    approved: Any,
    attempt: Mapping[str, Any],
    code: str,
    connection: sqlite3.Connection,
) -> str:
    """Record the pre-start move of a stage with alternates (or operator fallbacks) to its next
    profile.

    Called by the runner in the transaction that records ``attempt`` as ``prestart_no_spend``
    (the provider proved it never started; it settled at exactly zero), so the attempt's
    transition and this record commit together. Returns ``MOVED`` (one switch event, the
    executor's profile mirror updated), ``NO_PROFILE_LEFT`` (this candidate has no further
    profile: placement moves the stage on or fails it, nothing recorded here) or
    ``PROFILE_NOT_APPROVED`` (one stop event; the caller must not record the move).

    The approved order comes first, exactly as before: its next profile is checked and, when not
    covered, holds the stage (no later profile is skipped to). Only when the approved order is
    used up does the operator chain (``operator_order``) supply candidates: each one not covered
    is skipped (named in the move's ``skipped``) and the next tried; when none is covered the
    stage is held with the first refusal's detail."""
    listed = order(approved)
    number = int(attempt["attempt"])
    recorded = switches(connection, ctx.campaign, key, approved.provider)
    if any(int(item["from_attempt"]) == number for item in recorded):
        return MOVED
    choice = _choose(rt, ctx, key, approved, number, recorded, connection)
    if choice is None:
        return NO_PROFILE_LEFT
    common = {
        "stage_key": key,
        "provider_id": approved.provider,
        "attempt_id": attempt["id"],
        "from_profile": choice.current,
        "reason": REASON_PRESTART_NO_SPEND,
        "prestart_reason": code,
        "order": list(listed),
        "source": choice.source,
        # Proven by the provider's own records and a zero settlement (ADR 0060 addendum).
        "started": False,
        "spent": False,
        "settled_cents": 0,
        "recorded_at": _now(rt),
    }
    if choice.source == SOURCE_OPERATOR:
        common |= {"operator_order": list(choice.chain), "skipped": choice.skipped}
    if choice.detail is not None:
        rt._emit_once_in(
            connection,
            ctx,
            STOP_EVENT,
            {"stage_key": key, "attempt": number},
            common
            | {
                "tried": list(choice.done),
                "outcome": PROFILE_NOT_APPROVED,
                "to_profile": choice.nxt,
                "detail": choice.detail,
            },
        )
        return PROFILE_NOT_APPROVED
    _record_switch(rt, ctx, key, number, choice.nxt, common, connection)
    return MOVED


@dataclasses.dataclass(frozen=True)
class _Choice:
    """The next profile of a move and how it was found (``detail``: why it is not covered)."""

    current: str
    done: tuple[str, ...]
    nxt: str
    detail: str | None
    source: str
    skipped: list[dict[str, str]]
    chain: tuple[str, ...]
    here: Any
    target: Any


def _choose(
    rt: Any,
    ctx: Any,
    key: str,
    approved: Any,
    number: int,
    recorded: list[dict[str, Any]],
    connection: sqlite3.Connection,
) -> _Choice | None:
    """The one selector of a move's next profile (None: this candidate has no profile left).
    The approved order first, then the operator chain, each candidate checked by
    ``not_covered``; reads only."""
    listed = order(approved)
    chain = operator_order(approved)
    reachable = chain + _operator_targets(recorded)
    current = profile_for(recorded, listed[0], number)
    done = tried(recorded, listed[0])
    here = on_profile(approved, current, reachable)
    nxt = salad_alternate_profiles.next_profile(_Order(listed), done)
    source = SOURCE_SPEC
    skipped: list[dict[str, str]] = []
    detail = None
    target = None
    if nxt is not None:
        target = on_profile(approved, nxt)
        detail = not_covered(rt, ctx, key, here, target, connection)
    else:
        candidates = [name for name in chain if name not in done]
        if not candidates:
            return None
        source = SOURCE_OPERATOR
        for name in candidates:
            target = on_profile(approved, name, chain)
            found = not_covered(rt, ctx, key, here, target, connection, operator=True)
            if found is None:
                nxt = name
                break
            skipped.append({"profile": name, "detail": found})
        if nxt is None:
            nxt, detail = skipped[0]["profile"], skipped[0]["detail"]
            target = None
    return _Choice(current, done, nxt, detail, source, skipped, chain, here, target)


def _record_switch(
    rt: Any,
    ctx: Any,
    key: str,
    number: int,
    nxt: str,
    common: dict[str, Any],
    connection: sqlite3.Connection,
) -> None:
    """One switch event and the executor's profile mirror, in the caller's transaction (a
    runner that lost its lease records nothing)."""
    campaign = ctx.campaign
    rt._hold_lease(connection)
    to_attempt = number + 1
    rt.store.record_runner_event(
        connection,
        campaign,
        SWITCH_EVENT,
        common | {"from_attempt": number, "to_attempt": to_attempt, "to_profile": nxt},
    )
    rt._touch(
        connection,
        "campaign_salad_stages",
        "campaign_id=? AND stage_key=?",
        (campaign["id"], key),
        profile=nxt,
    )
    rt._actions.append(f"salad_profile_switched:{key}:{to_attempt}:{nxt}")


# -- the allocation-timeout exception (amends ADR 0169 point 3) ----------


def timeout_moves(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> tuple[int, ...]:
    """The attempt numbers the stage moved away from after an allocation timeout (every
    candidate). Each counts against ``retry.max_class_fallbacks`` with the pre-start moves."""
    return tuple(
        int(item["from_attempt"])
        for item in switches(connection, campaign, stage_key)
        if item.get("reason") == REASON_ALLOCATION_TIMEOUT
    )


def allocation_timeout_move(
    rt: Any,
    ctx: Any,
    key: str,
    approved: Any,
    attempt: Mapping[str, Any],
    connection: sqlite3.Connection,
) -> str | None:
    """Record the move of a CPU stage to its next profile after an attempt whose group was
    created and started but was never given an instance (the guardian's allocation deadline
    fired). The one exception to ADR 0169 point 3 (no move after a start effect): it applies
    only when the provider proves, from control's own records, that no instance was ever seen
    and nothing accrued (``SaladRouteProvider.allocation_timeout_evidence``), and the current
    and next profiles are both CPU profiles. The caller (the runner) has already checked the
    stage's gate and ``retry.max_class_fallbacks`` and records the attempt's own transition in
    ``connection``, the same transaction.

    Returns ``MOVED`` (one switch event, reason ``allocation_timeout``; idempotent: a switch
    already recorded from this attempt is ``MOVED`` with nothing new), or None: no proof, no
    profile left, or the next profile is not covered. None records NOTHING (no stop event): the
    attempt keeps today's infrastructure retry path, and the stage is never held for this.

    Settlement is untouched: the attempt settled (or holds) exactly as before, at its hold
    under ``guardian_bounded_hold``; the event records that amount and basis, never zero."""
    from .campaign_models import stage_provider

    listed = order(approved)
    if not listed:
        return None
    number = int(attempt["attempt"])
    recorded = switches(connection, ctx.campaign, key, approved.provider)
    if any(int(item["from_attempt"]) == number for item in recorded):
        return MOVED
    try:
        provider = stage_provider(approved.provider)
    except KeyError:
        return None
    evidence_hook = getattr(provider, "allocation_timeout_evidence", None)
    cpu_hook = getattr(provider, "cpu_profile", None)
    if evidence_hook is None or cpu_hook is None:
        return None
    current = profile_for(recorded, listed[0], number)
    try:
        here = on_profile(approved, current, operator_order(approved) + _operator_targets(recorded))
        evidence = evidence_hook(connection, dict(attempt), here)
    except Exception:  # noqa: BLE001 - unreadable evidence is not proof: no move
        return None
    if evidence is None:
        return None
    try:
        choice = _choose(rt, ctx, key, approved, number, recorded, connection)
    except Exception:  # noqa: BLE001 - a next profile that cannot be read is not covered
        return None
    if choice is None or choice.detail is not None or choice.target is None:
        return None
    if not cpu_hook(choice.target):
        return None  # GPU profiles keep the strict rule
    common = {
        "stage_key": key,
        "provider_id": approved.provider,
        "attempt_id": attempt["id"],
        "from_profile": choice.current,
        "reason": REASON_ALLOCATION_TIMEOUT,
        "order": list(listed),
        "source": choice.source,
        # The group was started (a start effect exists) but no instance was ever allocated,
        # allocating or running (the guardian's deadline), and nothing was billed by the
        # guardian's own estimate. ``settled_cents`` is what the attempt settled at (its hold,
        # never zero) or null when it has not settled yet.
        "started": True,
        "spent": False,
        "settled_cents": attempt.get("settled_cents"),
        "settlement_basis": attempt.get("settlement_basis"),
        "estimated_exposure_cents": "0",
        "evidence": evidence,
        "recorded_at": _now(rt),
    }
    if choice.source == SOURCE_OPERATOR:
        common |= {"operator_order": list(choice.chain), "skipped": choice.skipped}
    _record_switch(rt, ctx, key, number, choice.nxt, common, connection)
    return MOVED


def _now(rt: Any) -> str:
    from .campaign_runner import _iso

    return _iso(rt.now())


# -- receipt ----------


def receipt_section(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str, approved: Any
) -> dict[str, Any] | None:
    """The stage's ``salad_profiles`` receipt entry (None for a stage without alternates that
    no operator fallback moved or held). Each switch names its ``source``; a switch recorded
    before sources were recorded was a spec alternate move (the only kind there was)."""
    listed = order(approved)
    if not listed:
        return None
    recorded = switches(connection, campaign, stage_key, approved.provider)
    stop = stopped(connection, campaign, stage_key)
    if len(listed) < 2 and not recorded and stop is None:
        return None
    return {
        "order": list(listed),
        "switches": [
            {
                name: item.get(name)
                for name in (
                    "from_attempt",
                    "to_attempt",
                    "attempt_id",
                    "from_profile",
                    "to_profile",
                    "reason",
                    "prestart_reason",
                    "started",
                    "spent",
                    "settled_cents",
                    "recorded_at",
                )
            }
            | {"source": item.get("source") or SOURCE_SPEC}
            | ({"skipped": item["skipped"]} if item.get("skipped") else {})
            # Only an allocation-timeout move records these (every other switch keeps its shape).
            | {
                name: item[name]
                for name in ("settlement_basis", "estimated_exposure_cents")
                if name in item
            }
            for item in recorded
        ],
        "stopped": None
        if stop is None
        else {
            name: stop.get(name)
            for name in (
                "attempt",
                "attempt_id",
                "outcome",
                "detail",
                "from_profile",
                "to_profile",
                "reason",
                "started",
                "spent",
            )
        }
        | ({"source": stop["source"]} if stop.get("source") else {}),
    }


def attempt_profile(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    spec_stage: Any,
    attempt: Mapping[str, Any],
) -> str | None:
    """The profile one attempt ran (or runs) on, or None when it was placed on a candidate
    that lists no alternate profiles (another provider, or a single-profile Salad candidate).

    ``spec_stage`` is the approved stage; ``attempt`` its ``campaign_stage_attempts`` row. A
    switch records ``to_attempt`` before placement, and placement may put that attempt on
    another candidate (ADR 0060, ADR 0148): only the switches recorded on the candidate the
    attempt actually ran on are read, so an attempt elsewhere never carries a Salad profile,
    and the next attempt back on that candidate runs on (and reports) its switched profile."""
    from .campaign_models import stage_on_candidate

    pid = attempt.get("provider_id") or spec_stage.provider
    try:
        placed = spec_stage if pid == spec_stage.provider else stage_on_candidate(spec_stage, pid)
    except KeyError:
        return None
    listed = order(placed)
    if not listed:
        return None
    recorded = switches(connection, campaign, spec_stage.key, pid)
    if len(listed) < 2 and not recorded:
        return None  # a single-profile candidate no operator fallback moved: unchanged
    return profile_for(recorded, listed[0], int(attempt["attempt"]))
