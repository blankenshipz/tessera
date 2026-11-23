"""A remote candidate for heavy stages, added at submit (ADR 0141).

The operator's ``placement_preferences`` may name ``heavy_remote_candidate``: ``{provider,
max_exposure_cents, profile?}``. When it is set, submit gives every heavy stage (ADR 0107's
signals, with the operator's ``heavy_memory_bytes`` threshold) that lists no remote candidate a
derived candidate on that provider (the ADR 0076 stage adapter, on the project's registered
profile) as its first alternate, before the spec is digested. The candidate is therefore part of
what a person, an approved submission or a delegation approves, and it passes every submit,
approval and placement check the provider applies; nothing is added at placement time.

Each heavy stage that lists no remote candidate gets a ``heavy_remote_candidate`` record in its
stored spec: ``added`` with the reason ``operator_heavy_remote_candidate``, or the reason no
candidate was added (egress, scratch, no fitting profile, a cap it would exceed, a refused
check ...). A submitted record is discarded and recomputed, so only Tessera writes it; with it
goes at most the first alternate, and only one of the derived shape (never one the author
listed). A refused candidate's record also names its structured ``cause`` (amendment
2026-11-23c, ``refusal_cause``). Light stages, stages that already list a remote
candidate, v1 specs and a missing setting are left exactly as they were: the ADR 0107 warning
then records any local fallback at placement.

A ``gpu``-tagged heavy stage gets a GPU candidate only (amendment 2026-11-23): the adapter's
candidate on a GPU profile the operator opened to adapted stages with a ``gpu_hour_usd`` price
(or the setting's ``gpu_profile``), estimated at that price under the same caps; with no such
profile the record says ``no_gpu_profile``, and a CPU profile is never offered to it.

The candidate's estimate (the whole duration at the profile's rate) is held while it runs. It is
never above the operator's ``max_exposure_cents``, the stage's per-run cap, the campaign's budget
cap or what a covering program budget still admits: a candidate that would exceed one is not
added, with the reason recorded, and no cap is ever raised.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .campaign_models import (
    DOCKER_CPU_RUNTIME,
    HEAVY_REMOTE_ADDED,
    HEAVY_REMOTE_REFUSED,
    MAX_ALTERNATE_PROVIDERS,
    SALAD_RUNTIME,
    CampaignSpecV2,
    normalize,
    stage_on_candidate,
    stage_provider,
)
from .campaign_providers import TRUST_CLASSES
from .store import StoreError

FIELD = "heavy_remote_candidate"
MAX_DETAIL = 500
# Every reason ``_decide`` and ``apply`` record for a candidate not added. Each is an explicit
# fallback reason for ADR 0107 (amendment 2026-11-23b): the stage cannot run there (egress, GPU,
# scratch, runtime, an unpinned input, no fitting profile), a cap or budget it would exceed, the
# campaign forbids the provider, the provider is unregistered, the spec has no room for another
# alternate, or a submit check (trust, data class, licence label) refused the candidate.
NOT_ADDED_REASONS = frozenset(
    {
        "egress_bound",
        "gpu_candidate_unavailable",
        "forbidden_by_campaign",
        "provider_unregistered",
        "scratch_requested",
        "runtime_not_adaptable",
        "input_not_pinned",
        "alternates_full",
        "no_fitting_profile",
        "profile_unfit",
        "over_operator_cap",
        "over_stage_cap",
        "over_campaign_budget",
        "program_budget",
        HEAVY_REMOTE_REFUSED,
    }
)
# Amendment 2026-11-23c: the structured cause of a refused candidate (never parsed from text).
LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_LABELS = 64


class HeavyRemoteCandidate(BaseModel):
    """The operator setting (``placement_preferences.heavy_remote_candidate``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Only the Salad stage adapter (ADR 0076) can derive a candidate from a local CPU stage.
    provider: Literal["salad"]
    # The most a candidate added this way may hold per run; a larger estimate is not added.
    max_exposure_cents: Annotated[int, Field(strict=True, ge=1, le=5000)]
    # One profile of the provider; absent, the project's adapter-enabled CPU profiles in order.
    profile: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")] | None = None
    # The same for a ``gpu``-tagged stage: one GPU profile, else the project's GPU profiles whose
    # stage_adapter names an operator gpu_hour_usd, in order (amendment 2026-11-23).
    gpu_profile: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")] | None = None


def configured() -> HeavyRemoteCandidate | None:
    from . import campaign_placement_preference as preference

    prefs = preference.active()
    return None if prefs is None else prefs.heavy_remote_candidate


def _derived_shape(item: dict[str, Any], record: dict[str, Any]) -> bool:
    """Whether ``item`` has the shape ``apply`` gives the alternate a record claims it added:
    the record's provider and profile, the Salad runtime and an adapted config."""
    config = item.get("provider_config") or {}
    return (
        item.get("provider") == record.get("provider")
        and item.get("runtime") == SALAD_RUNTIME
        and config.get("adapted") is True
        and config.get("profile") == record.get("profile")
    )


def _strip(spec: CampaignSpecV2) -> CampaignSpecV2:
    """``spec`` without submitted records, and without the one alternate a record claims was
    added: only the first alternate, and only if it has the shape ``apply`` derives. Alternates
    the author listed are kept."""
    data = spec.model_dump(mode="json")
    changed = False
    for stage in data["stages"]:
        record = stage.pop(FIELD, None)
        if record is None:
            continue
        changed = True
        alternates = list(stage.get("alternate_providers") or ())
        if record.get("added") and alternates and _derived_shape(alternates[0], record):
            alternates = alternates[1:]
        if alternates:
            stage["alternate_providers"] = alternates
        else:
            stage.pop("alternate_providers", None)
    return type(spec).model_validate(data) if changed else spec


def _record(reason: str, provider: str, signals: list[str], **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "added": reason == HEAVY_REMOTE_ADDED,
        "reason": reason,
        "provider": provider,
        "signals": signals[:16],
    }
    for key, item in extra.items():
        if item is not None:
            value[key] = item[:MAX_DETAIL] if key == "detail" else item
    return value


def _profiles(
    provider: Any, setting: HeavyRemoteCandidate, tenant: tuple[str, str], gpu: bool
) -> list:
    """The provider's adapter-enabled profiles of the wanted kind for this org and project: CPU-only
    ones for a CPU stage, GPU ones that name the operator's GPU price for a GPU stage. A profile of
    the other kind is never offered."""
    registry = getattr(provider, "profiles", None)
    name = setting.gpu_profile if gpu else setting.profile
    found = []
    for profile in getattr(registry, "profiles", None) or ():
        if (profile.org_id, profile.project_id) != tenant:
            continue  # another tenant's profile is never considered or named
        if name is not None and profile.name != name:
            continue
        adapter = profile.stage_adapter
        if adapter is None or profile.gpu != gpu:
            continue
        if gpu and adapter.gpu_hour_usd is None:
            continue
        found.append(profile)
    return found


PINNED_KINDS = frozenset({"snapshot", "artifact", "public_artifact"})


def _unpinned_input(spec: CampaignSpecV2, key: str, inputs: Any) -> str | None:
    """Why an input of stage ``key`` is neither pinned nor late-bindable (None when all are).

    Pinned inputs carry their digest in the spec. A ``stage`` input is late-bound (ADR 0140):
    it must name another stage of this same campaign (spec validation keeps the graph acyclic),
    so the DAG edge the person approves covers the digest the runner binds at stage start, with
    data class and licence re-checked there. Anything else is not served."""
    others = {item.key for item in spec.stages} - {key}
    for item in inputs:
        kind = getattr(item, "kind", None)
        if kind in PINNED_KINDS:
            continue
        if kind == "stage":
            if getattr(item, "stage", None) in others:
                continue
            return f"input {item.name!r} names no other stage of this campaign"
        return f"input {getattr(item, 'name', '?')!r} of kind {kind!r} is not pinned"
    return None


def _decide(
    connection: sqlite3.Connection,
    spec: CampaignSpecV2,
    index: int,
    setting: HeavyRemoteCandidate,
    tenant: tuple[str, str],
    entries: Callable[[Any], Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """(record, candidate entry or None) for one stage, or None when the rule does not concern
    it (light, or it already lists a remote candidate)."""
    from . import campaign_heavy_guard as guard
    from . import campaign_placement_preference as preference
    from . import program_budget
    from .campaign_salad_adapter import (
        candidate_estimate_cents,
        derive_candidate_config,
        upstream_outputs,
    )

    stage = spec.stages[index]
    normal = normalize(spec).stage(stage.key)
    campaign = {"org_id": tenant[0], "project_id": tenant[1]}
    # Heavy by ADR 0107's signals; egress is set aside here only to say why it is not served.
    is_heavy, signals = guard.heavy(dataclasses.replace(normal, egress=None), connection, campaign)
    if not is_heavy:
        return None
    pid = setting.provider
    kinds = [preference.candidate_kind(normal, item) for item in normal.candidate_ids]
    if pid in normal.candidate_ids or any(kind.startswith("remote_") for kind in kinds):
        return None

    def skip(reason: str, detail: str | None = None, **extra: Any):
        return _record(reason, pid, signals, detail=detail, **extra), None

    if normal.egress is not None:
        return skip("egress_bound", "a stage that declares egress is kept off rented capacity")
    # A gpu-tagged stage only ever gets a GPU candidate, any other stage only a CPU one.
    gpu = bool(preference.GPU_TAGS & set(normal.placement_tags))
    if pid in spec.forbid_providers:
        return skip("forbidden_by_campaign")
    try:
        provider = stage_provider(pid)
    except KeyError:
        return skip("provider_unregistered")
    if normal.scratch_bytes is not None:
        return skip("scratch_requested", f"{pid} grants no scratch request")
    if normal.runtime != DOCKER_CPU_RUNTIME:
        return skip("runtime_not_adaptable", f"only a {DOCKER_CPU_RUNTIME} stage is adapted")
    unpinned = _unpinned_input(spec, stage.key, normal.inputs)
    if unpinned is not None:
        return skip("input_not_pinned", unpinned)
    if len(stage.alternate_providers or ()) >= MAX_ALTERNATE_PROVIDERS:
        return skip("alternates_full")
    profiles = _profiles(provider, setting, tenant, gpu)
    if not profiles and gpu:
        return skip(
            "no_gpu_profile",
            "the stage needs a GPU and no GPU profile of this project is opened to adapted stages "
            "with an operator gpu_hour_usd",
        )
    if not profiles:
        return skip("no_fitting_profile", "no adapter-enabled CPU profile for this project")
    first: tuple[dict[str, Any], None] | None = None
    for profile in profiles:
        try:
            # A stage-output input is sized by the upstream outputs' declared max_bytes, as
            # the adapter does for an explicit request (ADR 0140); unbounded is refused.
            config = derive_candidate_config(
                stage, profile, entries, upstream_outputs(spec), gpu=gpu
            )
        except StoreError as error:
            unfit = "gpu_profile_unfit" if gpu else "profile_unfit"
            outcome = skip(unfit, str(error), profile=profile.name)
            first = first or outcome
            continue
        estimate = candidate_estimate_cents(stage, profile)
        bounds = (
            ("over_operator_cap", setting.max_exposure_cents, "the operator's max_exposure_cents"),
            ("over_stage_cap", stage.max_exposure_cents, "the stage's max_exposure_cents"),
            ("over_campaign_budget", spec.budget_cap_cents, "the campaign's budget_cap_cents"),
        )
        refused = next((b for b in bounds if estimate > b[1]), None)
        if refused is None:
            budget = program_budget.check_admission(connection, tenant[1], estimate)
            if budget is not None:
                outcome = skip("program_budget", budget, profile=profile.name)
                first = first or outcome
                continue
        else:
            outcome = skip(
                refused[0],
                f"the estimate {estimate} cents exceeds {refused[2]} ({refused[1]})",
                profile=profile.name,
            )
            first = first or outcome
            continue
        entry = {
            "provider": pid,
            "runtime": SALAD_RUNTIME,
            "provider_config": config,
            "max_exposure_cents": estimate,
        }
        return (
            _record(
                HEAVY_REMOTE_ADDED,
                pid,
                signals,
                profile=profile.name,
                max_exposure_cents=estimate,
            ),
            entry,
        )
    return first


def _check_candidate(store: Any, connection, live, project_id, spec, key: str, pid: str) -> None:
    """The submit checks every approved candidate passes (ADR 0060), for the added one only."""
    from . import provider_trust

    normal = normalize(spec)
    candidate = stage_on_candidate(normal.stage(key), pid)
    provider = stage_provider(pid)
    provider.check_stage(connection, project_id, candidate)
    store.check_grant_lifetime(provider, candidate)
    provider_trust.check_narrowing(
        pid,
        None if candidate.send_classes is None else list(candidate.send_classes),
        store.stage_worker(provider, candidate),
        connection=connection,
    )
    store.check_stage_inputs(
        connection,
        candidate,
        provider,
        provider_trust.current_policy(connection).digest,
        spec=normal,
        scope=(live["org_id"], project_id),
    )


def refusal_cause(error: BaseException) -> dict[str, Any]:
    """The structured cause of a submit check's refusal of the added candidate, from the
    exception's type and attributes only (its message is never parsed): ``license_label`` with
    the effective ``labels`` checked (``LicenseRefused``), ``data_class`` with the class the
    provider is not trusted to receive (``DataClassConflict``), ``input_not_admitted`` for a
    data refusal that names no class, otherwise ``other``. No path, value or secret."""
    from .campaign_store import DataClassConflict, LicenseRefused

    if isinstance(error, LicenseRefused):
        labels = sorted({str(item) for item in error.labels if LABEL_RE.match(str(item))})
        return {"cause": "license_label", "labels": labels[:MAX_LABELS]}
    if isinstance(error, DataClassConflict):
        if error.data_class in TRUST_CLASSES:
            return {"cause": "data_class", "data_class": error.data_class}
        return {"cause": "input_not_admitted"}
    return {"cause": "other"}


def apply(
    store: Any,
    connection: sqlite3.Connection,
    live: dict[str, Any],
    project_id: str,
    spec: Any,
    entries: Callable[[Any], Any] | None,
) -> Any:
    """``spec`` with the operator's heavy remote candidate added where it applies, and a record
    on each heavy stage that lists no remote candidate. Called at submit before the digest."""
    if not isinstance(spec, CampaignSpecV2):
        return spec  # v1 has no alternates: unchanged (ADR 0107 records any local fallback)
    current = _strip(spec)
    setting = configured()
    if setting is None:
        return current
    tenant = (live["org_id"], project_id)
    for index in range(len(current.stages)):
        decided = _decide(connection, current, index, setting, tenant, entries)
        if decided is None:
            continue
        record, entry = decided
        data = current.model_dump(mode="json")
        stage = data["stages"][index]
        if entry is not None:
            stage["alternate_providers"] = [entry, *(stage.get("alternate_providers") or ())]
            stage[FIELD] = record
            try:
                trial = type(current).model_validate(data)
                _check_candidate(
                    store, connection, live, project_id, trial, stage["key"], record["provider"]
                )
            except sqlite3.OperationalError:
                raise  # storage trouble is not a reason to drop the candidate
            except (StoreError, ValueError, ValidationError) as error:
                data = current.model_dump(mode="json")
                stage = data["stages"][index]
                stage[FIELD] = _record(
                    HEAVY_REMOTE_REFUSED,
                    record["provider"],
                    record["signals"],
                    profile=record.get("profile"),
                    detail=f"{type(error).__name__}: {error}",
                    **refusal_cause(error),
                )
            else:
                current = trial
                continue
        else:
            stage[FIELD] = record
        current = type(current).model_validate(data)
    return current


def records(spec_json: str) -> dict[str, dict[str, Any]]:
    """Each stage's recorded ``heavy_remote_candidate`` by stage key (receipts)."""
    out = {}
    for stage in json.loads(spec_json).get("stages", []):
        record = stage.get(FIELD)
        if record is not None:
            out[stage["key"]] = record
    return out
