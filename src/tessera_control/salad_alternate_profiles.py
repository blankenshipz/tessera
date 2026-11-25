"""The ordered execution profiles a Salad stage may run on: its own, then its alternates.

A Salad stage names one operator profile (``SaladStageSpec.profile``) and may list further
profiles in ``alternate_profiles``, in the order they are tried (ADR 0151). The approved spec
names every profile up front, so a fallback needs no new human step; a profile that is not
listed is never used. This module is the one place that answers "which profiles may this stage
use, in what order", so the spec model, approval, the runner's fallback and the read views agree.

Submit checks every listed profile exactly as the stage's own (``SaladRouteProvider.check_stage``:
registered, same organization and project, same worker, GPU/CPU kind, image allowlist, per-run
and per-task ceilings, CPU worst-case estimate) and the stage adapter refuses an alternate that
would derive a different workload. So the only profile-dependent part of an approved stage is the
profile name itself, and ``stage_on_profile`` is an exact substitution.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

# Where a profile move's target came from (``campaign.salad_profile_switched`` ``source``).
SOURCE_SPEC = "spec_alternate"
SOURCE_OPERATOR = "operator_fallback"
SOURCES = frozenset({SOURCE_SPEC, SOURCE_OPERATOR})


def profile_order(config: Any) -> tuple[str, ...]:
    """The stage's own profile first, then its ``alternate_profiles`` in order (no repeats).

    ``config`` is a ``SaladStageSpec`` (or any object with ``profile`` and, optionally,
    ``alternate_profiles``); a stage with no alternates yields exactly its own profile."""
    own = str(config.profile)
    order = [own]
    for name in getattr(config, "alternate_profiles", None) or ():
        if str(name) not in order:
            order.append(str(name))
    return tuple(order)


def next_profile(config: Any, tried: tuple[str, ...] | list[str]) -> str | None:
    """The first listed profile not in ``tried``, or None when the list is exhausted."""
    done = set(tried)
    return next((name for name in profile_order(config) if name not in done), None)


def operator_chain(config: Any, profiles: Any) -> tuple[str, ...]:
    """The operator capacity fallbacks of a stage, tried only after its approved order.

    For each profile of ``profile_order(config)`` in order, its operator ``fallback_profiles``
    chain (``SaladCampaignProfiles.fallback_chain``), skipping any profile the approved order
    already lists (that one is tried in its approved place) and any repeat. ``profiles`` is the
    operator profile file (None: no fallbacks). Operator config, never part of the spec: reading
    it changes no approved digest. Every candidate is checked again before a move
    (``campaign_salad_fallback``); this only names the order."""
    if profiles is None:
        return ()
    listed = profile_order(config)
    found: list[str] = []
    for name in listed:
        for item in profiles.fallback_chain(name):
            if item not in listed and item not in found:
                found.append(item)
    return tuple(found)


def stage_on_operator_profile(spec_stage: Any, profile: str) -> Any:
    """The stage as it runs on an operator fallback profile (not one the spec lists).

    Only ``campaign_salad_fallback`` calls this, for a profile it took from the operator chain
    and checked (or a recorded ``operator_fallback`` switch on the stage's candidate). The Salad
    config's ``profile`` becomes ``profile`` and ``alternate_profiles`` is dropped, so the
    derived stage reaches nothing else (its order is always read from the approved stage).
    Every other field, the per-run cap and the retry bound included, is the approved stage's."""
    config = _config(spec_stage)
    if profile in profile_order(config):
        return stage_on_profile(spec_stage, profile)
    data = config.model_dump(mode="json") | {"profile": profile}
    data.pop("alternate_profiles", None)
    moved = type(config).model_validate(data)
    if getattr(spec_stage, "salad", None) is not None:
        return spec_stage.model_copy(update={"salad": moved})
    if dataclasses.is_dataclass(spec_stage):
        return dataclasses.replace(spec_stage, provider_config=moved.model_dump(mode="json"))
    return spec_stage.model_copy(update={"provider_config": moved.model_dump(mode="json")})


def _config(stage: Any) -> Any:
    from .campaign_models import SaladStageSpec

    found = getattr(stage, "salad", None)
    if found is None:
        found = getattr(stage, "provider_config", None)
    if isinstance(found, SaladStageSpec):
        return found
    if not found:
        raise KeyError(f"Stage {getattr(stage, 'key', '?')} has no Salad configuration")
    return SaladStageSpec.model_validate(found)


def covered_profiles(spec_stage: Any) -> tuple[str, ...]:
    """Every operator profile the approved stage may run on, in order (``profile_order``).

    ``spec_stage`` is a ``NormalStage`` (Salad ``provider_config``), a v1 ``StageSpec`` (its
    ``salad`` block) or a ``StageV2``. Raises KeyError for a stage without Salad fields."""
    return profile_order(_config(spec_stage))


def stage_on_profile(spec_stage: Any, profile: str) -> Any:
    """The stage as it runs on one of its approved profiles (ADR 0151).

    The Salad config's ``profile`` becomes ``profile``; ``alternate_profiles`` keeps the other
    approved profiles (the original order, ``profile`` removed), so the derived stage covers the
    same set and nothing outside it. Fallback order is always read from the *approved* stage
    (``profile_order``/``next_profile``), never from a derived one. Every other field is kept:
    submit already proved each listed profile accepts the same worker, workload, image and caps,
    so nothing else depends on the profile (the run-time selection is derived from the profile at
    use, as for the stage's own). The stage's own profile returns the stage unchanged.

    A profile the stage does not list is a programming error (KeyError): nothing outside the
    approved list is reachable. Works on a ``NormalStage`` (dataclass), a v1 ``StageSpec`` and a
    ``StageV2`` (pydantic models)."""
    config = _config(spec_stage)
    order = profile_order(config)
    if profile not in order:
        raise KeyError(
            f"Stage {getattr(spec_stage, 'key', '?')} has no approved profile {profile!r}"
        )
    if profile == config.profile:
        return spec_stage
    others = [name for name in order if name != profile]
    data = config.model_dump(mode="json") | {"profile": profile, "alternate_profiles": others}
    # Re-validated, so the derived config is exactly a well-formed approved shape.
    moved = type(config).model_validate(data)
    if getattr(spec_stage, "salad", None) is not None:
        return spec_stage.model_copy(update={"salad": moved})
    if dataclasses.is_dataclass(spec_stage):
        return dataclasses.replace(spec_stage, provider_config=moved.model_dump(mode="json"))
    return spec_stage.model_copy(update={"provider_config": moved.model_dump(mode="json")})


def attempt_profile(
    connection: sqlite3.Connection, attempt_id: str, default: str | None = None
) -> str | None:
    """The operator profile a Salad attempt ran (or runs) on, as the execution plane recorded it.

    Read, in order: a ``profile`` column of the attempt's ``campaign_salad_attempts`` row (when
    the schema has one), then the ``profile`` of the stage's ``campaign_salad_stages`` row, else
    ``default``. Review pre-approval (ADR 0151) refuses a plan or source-use review whose attempt
    profile it does not cover, so an execution plane that switches profile records the switch in
    one of these places before it proposes that attempt's reviews."""
    row = connection.execute(
        "SELECT * FROM campaign_salad_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is not None and "profile" in row.keys() and row["profile"]:
        return str(row["profile"])
    found = connection.execute(
        """SELECT s.profile AS profile FROM campaign_salad_stages s
        JOIN campaign_stage_attempts a ON a.campaign_id = s.campaign_id
        AND a.stage_key = s.stage_key WHERE a.id=?""",
        (attempt_id,),
    ).fetchone()
    if found is not None and found["profile"]:
        return str(found["profile"])
    return default
