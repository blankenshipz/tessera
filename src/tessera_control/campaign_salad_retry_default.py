"""Submit-time default of ``retry.infra_retries`` for a stage that may run on Salad (ADR 0215).

A Salad route can be stopped by its own safety plane (a guardian restart, an unreadable rate or
accounting) or by the provider moving it: infrastructure, never evidence about the stage. With
``infra_retries`` at the model default of 0 every such stop fails the stage permanently, so a
submitted stage that may run on Salad and does not set ``retry.infra_retries`` gets
``SALAD_DEFAULT_INFRA_RETRIES`` written into its spec here, before the spec is digested.

The value is explicit in the stored spec, so it is in the digest the approver approves and in
the stage's worst-case exposure (``RetryPolicy.worst_case_attempts``), exactly as if the author
had written it. Nothing is applied to a stored spec: a spec stored without the key keeps meaning
0 and keeps its digest, and a re-run derived from a stored spec is not defaulted again. An
explicit value (0 included) is always kept. The default is not applied where the stage would no
longer validate with it (a one-time holdout claim, which allows no infrastructure retry, or a
worst-case hold over the experiment ceiling or the per-task ceiling of a named operator profile
or of a profile on its operator fallback chain): every check runs unchanged and none is loosened.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .campaign_models import SALAD_BACKEND, SALAD_PROVIDER

SALAD_DEFAULT_INFRA_RETRIES = 2


def may_run_on_salad(stage: Any) -> bool:
    """True for a v1 ``salad-gpu-v1`` stage or a v2 stage with Salad as any candidate."""
    if getattr(stage, "backend", None) == SALAD_BACKEND:
        return True
    if getattr(stage, "provider", None) == SALAD_PROVIDER:
        return True
    return any(
        getattr(item, "provider", None) == SALAD_PROVIDER
        for item in (getattr(stage, "alternate_providers", None) or ())
    )


def _profile_names(stage: Any) -> set[str]:
    """Every Salad operator profile the stage names (v1 block, v2 candidate config or an adapter
    request), primary and alternates."""
    configs: list[Any] = []
    salad = getattr(stage, "salad", None)
    if salad is not None:
        configs.append(salad.model_dump(mode="json"))
    if getattr(stage, "provider", None) == SALAD_PROVIDER:
        configs.append(getattr(stage, "provider_config", None))
    for item in getattr(stage, "alternate_providers", None) or ():
        if getattr(item, "provider", None) == SALAD_PROVIDER:
            configs.append(item.provider_config)
    names: set[str] = set()
    for config in configs:
        if not isinstance(config, dict):
            continue
        if isinstance(config.get("adapt"), dict):
            config = config["adapt"]
        if isinstance(config.get("profile"), str):
            names.add(config["profile"])
        names.update(n for n in config.get("alternate_profiles") or () if isinstance(n, str))
    return names


def _with_operator_chain(names: set[str], profiles: Any) -> set[str]:
    """``names`` plus every profile of their operator ``fallback_profiles`` chains: a stage may
    be moved onto one of those at runtime, where ``operator_fallback_refusal`` applies the same
    per-task bound to the stage's approved ``hold_attempts``."""
    chain = getattr(profiles, "fallback_chain", None)
    if chain is None:
        return names
    found = set(names)
    for name in names:
        found.update(chain(name))
    return found


def _fits_profiles(stage: Any, profiles: Any) -> bool:
    """True unless a named operator profile's per-task ceiling, or that of a profile on their
    operator fallback chain, would refuse the stage's worst-case hold with the default (the
    bound ``check_stage`` applies at submit and ``operator_fallback_refusal`` at a move), so
    the default never silently removes a fallback the stage would have run on at 0."""
    if profiles is None:
        return True  # nothing configured: submit refuses the stage on its own terms
    for name in sorted(_with_operator_chain(_profile_names(stage), profiles)):
        profile = profiles.get(name)
        if profile is None:
            continue  # an unknown profile is refused by submit, default or not
        if stage.max_exposure_cents * stage.retry.hold_attempts > profile.max_task_cents:
            return False
    return True


def _registered_profiles() -> Any:
    from .campaign_models import stage_provider

    try:
        return getattr(stage_provider(SALAD_PROVIDER), "profiles", None)
    except KeyError:
        return None


def _unset(stage: Any) -> bool:
    """True when the submitted stage did not write ``retry.infra_retries`` itself."""
    return "retry" not in stage.model_fields_set or (
        "infra_retries" not in stage.retry.model_fields_set
    )


def with_salad_infra_retry_default(spec: Any, profiles: Any = None) -> Any:
    """``spec`` with ``retry.infra_retries`` set to the Salad default on each Salad stage that
    left it unset and validates with it; ``spec`` itself when nothing changes.

    Only for a freshly submitted spec (parsed from the request body, so ``model_fields_set``
    says what the author wrote), never for a stored spec or a re-run derived from one."""
    # Decided on the submitted spec only: a round trip through the dump drops an explicit 0
    # (it serializes as absent), so it must never be re-read from ``current``.
    targets = [
        index
        for index, stage in enumerate(spec.stages)
        if may_run_on_salad(stage) and _unset(stage)
    ]
    if not targets:
        return spec
    if profiles is None:
        profiles = _registered_profiles()
    current = spec
    for index in targets:
        data = current.model_dump(mode="json")
        retry = dict(data["stages"][index].get("retry") or {})
        retry["infra_retries"] = SALAD_DEFAULT_INFRA_RETRIES
        data["stages"][index]["retry"] = retry
        try:
            trial = type(current).model_validate(data)
        except (ValidationError, ValueError):
            continue  # the stage does not admit it: unchanged, every check as before
        if _fits_profiles(trial.stages[index], profiles):
            current = trial
    return current


__all__ = [
    "SALAD_DEFAULT_INFRA_RETRIES",
    "may_run_on_salad",
    "with_salad_infra_retry_default",
]
