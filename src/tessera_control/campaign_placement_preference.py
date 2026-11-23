"""Operator placement preference by resource class (ADR 0070).

The operator's ``--campaign-providers`` file may carry ``placement_preferences``: ordered rules
that each match a stage by its resource class (``max_duration_seconds``, ``scratch_bytes`` or a
declared ``placement_tags`` label) and name the providers such a stage should try first. A
matching rule only *reorders* the candidates the approved spec already lists (its own provider
and ``alternate_providers``, ADR 0060): a preferred provider the stage does not list is ignored,
``retry.reroute_providers`` targets stay last under ADR 0058's limits, and every placement check
still runs on every candidate. Without the section the built-in default policy (ADR 0088) applies.

The default policy (ADR 0088) needs no operator rule. A stage that lists alternates and declares
a signal is classed from its approved spec: heavy (declared memory or expected runtime at the
thresholds, or a ``heavy`` or ``training`` tag) tries a remote CPU candidate first, a stage tagged
``gpu`` a remote GPU one, and an egress-bound stage (it declares egress) keeps remote candidates
behind the others. A stage with no signal keeps the order its spec wrote. What a candidate is
(local or remote, CPU or GPU) comes from what its provider reports (``PlacementCapability``), so
this module names no vendor. Placement then runs ADR 0060 unchanged: a remote candidate that is
unregistered, untrusted, over the per-run cap or the budget is skipped with its reason and the
next candidate (the local provider) is tried: that is the fallback, and it is recorded.

The applied rule is returned with the order so the runner records it on the placement event and
the receipt: the approver approved the set of destinations by digest, and the record shows why
this attempt tried them in a different order than the spec wrote.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .campaign_heavy_remote import HeavyRemoteCandidate
from .campaign_models import MAX_PLACEMENT_TAGS, NormalStage, PlacementTag, ProviderId

MAX_RULES = 8
# Built-in default policy (ADR 0088); the operator may change the two thresholds or switch it off.
HEAVY_MEMORY_BYTES = 1536 * 1024**2  # 1.5 GiB
LONG_RUNTIME_SECONDS = 1800
HEAVY_TAGS = frozenset({"heavy", "training"})
GPU_TAGS = frozenset({"gpu"})
MAX_PREFERRED = 5
RuleName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceClass(_Closed):
    """A stage matches when ANY stated criterion holds (each is an ``at least`` bound)."""

    min_duration_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)] | None = None
    min_scratch_bytes: (
        Annotated[int, Field(strict=True, ge=1024 * 1024, le=64 * 1024**3)] | None
    ) = None
    any_tags: (
        Annotated[list[PlacementTag], Field(min_length=1, max_length=MAX_PLACEMENT_TAGS)] | None
    ) = None

    @model_validator(mode="after")
    def _some_criterion(self) -> ResourceClass:
        if (
            self.min_duration_seconds is None
            and self.min_scratch_bytes is None
            and self.any_tags is None
        ):
            raise ValueError("a placement preference rule needs at least one criterion")
        if self.any_tags is not None and len(set(self.any_tags)) != len(self.any_tags):
            raise ValueError("placement preference any_tags must be distinct")
        return self

    def matched(self, stage: NormalStage) -> list[str]:
        """The criteria this stage meets (empty: no match)."""
        hits = []
        if (
            self.min_duration_seconds is not None
            and int(stage.max_duration_seconds) >= self.min_duration_seconds
        ):
            hits.append("min_duration_seconds")
        if (
            self.min_scratch_bytes is not None
            and stage.scratch_bytes is not None
            and stage.scratch_bytes >= self.min_scratch_bytes
        ):
            hits.append("min_scratch_bytes")
        if self.any_tags is not None and set(self.any_tags) & set(stage.placement_tags):
            hits.append("any_tags")
        return hits


class PreferenceRule(_Closed):
    name: RuleName
    when: ResourceClass
    prefer: Annotated[list[ProviderId], Field(min_length=1, max_length=MAX_PREFERRED)]

    @model_validator(mode="after")
    def _distinct(self) -> PreferenceRule:
        if len(set(self.prefer)) != len(self.prefer):
            raise ValueError(f"placement preference {self.name}: prefer must be distinct")
        return self


class PlacementPreferences(_Closed):
    rules: Annotated[list[PreferenceRule], Field(max_length=MAX_RULES)] = []  # noqa: RUF012
    # ADR 0088: the built-in default policy runs for a stage no rule matches; false switches it off.
    default_policy: Annotated[bool, Field(strict=True)] = True
    heavy_memory_bytes: (
        Annotated[int, Field(strict=True, ge=1024 * 1024, le=2048 * 1024**3)] | None
    ) = None
    long_runtime_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)] | None = None
    # ADR 0107: what a heavy stage placed on a local candidate while a remote profile is
    # registered does: ``warn`` (placed, with a recorded warning) or ``refuse`` (it waits).
    heavy_local_fallback: Literal["warn", "refuse"] = "warn"
    # ADR 0107 amendment 2026-11-23b: a heavy stage reaching a local candidate with no explicit
    # fallback reason is ``refuse``d (the stage fails, ``heavy_local_unexplained``) or, opted
    # back in, placed with the recorded warning (``warn``).
    heavy_unexplained_local: Literal["refuse", "warn"] = "refuse"
    # ADR 0141: the remote candidate submit adds to a heavy stage that lists none (absent: none).
    heavy_remote_candidate: HeavyRemoteCandidate | None = None

    @model_validator(mode="after")
    def _distinct(self) -> PlacementPreferences:
        names = [rule.name for rule in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("placement preference rule names must be distinct")
        return self


_ACTIVE: PlacementPreferences | None = None


def parse(section: Any) -> PlacementPreferences | None:
    """Validate the operator section strictly (closed models, bounded values)."""
    return None if section is None else PlacementPreferences.model_validate(section)


def configure(section: Any, registered: Iterable[str]) -> None:
    """Install the operator's rules (None clears them). Every preferred provider must be a
    registered provider id; a malformed section raises ValueError and installs nothing."""
    global _ACTIVE
    _ACTIVE = None
    parsed = parse(section)
    if parsed is not None:
        known = set(registered)
        for rule in parsed.rules:
            unknown = [pid for pid in rule.prefer if pid not in known]
            if unknown:
                raise ValueError(
                    f"placement preference {rule.name} names unregistered providers {unknown}"
                )
    _ACTIVE = parsed


def active() -> PlacementPreferences | None:
    return _ACTIVE


def _thresholds() -> tuple[int, int]:
    prefs = _ACTIVE
    memory = (
        HEAVY_MEMORY_BYTES
        if prefs is None or prefs.heavy_memory_bytes is None
        else (prefs.heavy_memory_bytes)
    )
    seconds = (
        LONG_RUNTIME_SECONDS
        if prefs is None or prefs.long_runtime_seconds is None
        else (prefs.long_runtime_seconds)
    )
    return memory, seconds


def heavy_bytes_threshold() -> int:
    """The heavy threshold in bytes (``heavy_memory_bytes``, default 1.5 GiB)."""
    return _thresholds()[0]


def heavy_local_fallback() -> str:
    """``warn`` (the default, also when no section is configured) or ``refuse`` (ADR 0107)."""
    return "warn" if _ACTIVE is None else _ACTIVE.heavy_local_fallback


def heavy_unexplained_local() -> str:
    """``refuse`` (the default, also when no section is configured) or ``warn`` (ADR 0107
    amendment 2026-11-23b)."""
    return "refuse" if _ACTIVE is None else _ACTIVE.heavy_unexplained_local


def default_enabled() -> bool:
    return _ACTIVE is None or _ACTIVE.default_policy


def stage_class(stage: NormalStage) -> tuple[str, list[str]]:
    """The generic class the default policy gives a stage, and the signals that decided it.

    ``egress_bound`` (the stage declares egress: it fetches from the internet), ``gpu`` (tagged
    ``gpu``), ``heavy`` (declared memory or expected runtime at the thresholds, or a ``heavy`` or
    ``training`` tag) or ``light`` (no signal). Only what the approved spec declares is read, and
    ``max_duration_seconds`` is never one of it: it is a cap, not an estimate. No name, host or
    experiment is involved."""
    memory, seconds = _thresholds()
    heavy = []
    if stage.memory_bytes is not None and stage.memory_bytes >= memory:
        heavy.append("declared_memory")
    if stage.expected_duration_seconds is not None and stage.expected_duration_seconds >= seconds:
        heavy.append("long_expected_runtime")
    heavy += [f"tag:{tag}" for tag in sorted(HEAVY_TAGS & set(stage.placement_tags))]
    if stage.egress is not None:
        return "egress_bound", ["egress", *heavy]
    gpu = [f"tag:{tag}" for tag in sorted(GPU_TAGS & set(stage.placement_tags))]
    if gpu:
        return "gpu", [*gpu, *heavy]
    return ("heavy", heavy) if heavy else ("light", [])


def candidate_kind(stage: NormalStage, provider_id: str) -> str:
    """``remote_cpu``, ``remote_gpu``, ``local``, ``other`` or ``unknown``, from what the
    provider reports about itself (``PlacementCapability``, ADR 0088): no vendor is named here."""
    from .campaign_models import stage_on_candidate
    from .campaign_providers import get_provider, placement_capability_of

    try:
        provider = get_provider(provider_id)
    except KeyError:
        return "unknown"
    capability = placement_capability_of(provider, stage_on_candidate(stage, provider_id))
    if capability is None:
        return "other"
    return "local" if capability.tier == "local" else f"remote_{capability.hardware}"


# The kinds a class tries first. Egress-bound stages move only remote candidates behind the
# others (the local provider, or one the registry does not know). A light stage (no signal) keeps
# the order its spec wrote: no default reordering of an existing spec.
_NOT_REMOTE = ("local", "other", "unknown")
_PREFER_KINDS = {
    "heavy": ("remote_cpu",),
    "gpu": ("remote_gpu",),
    "egress_bound": _NOT_REMOTE,
}


def default_order(
    stage: NormalStage, guard_signals: Iterable[str] = ()
) -> tuple[tuple[str, ...], dict[str, Any] | None]:
    """The built-in default policy's order and its record (None: nothing to say).

    ``guard_signals`` are the heavy signals the placement guard found (ADR 0107: it also counts
    ``pretraining``, large pinned inputs and large declared outputs). They make a stage the
    class function calls ``light`` heavy here, so ordering and the guard agree; they never
    change an egress-bound or gpu stage."""
    spec_order = stage.candidate_ids
    placement = stage.placement_ids
    cls, signals = stage_class(stage)
    extra = [item for item in guard_signals if item not in signals]
    if cls == "light" and extra:
        cls, signals = "heavy", extra
    elif cls == "heavy":
        signals = [*signals, *extra]
    if cls == "light":
        return spec_order, None  # no signal: the spec author's explicit order stands
    wanted = _PREFER_KINDS[cls]
    kinds = {pid: candidate_kind(stage, pid) for pid in placement}
    front = [pid for pid in placement if kinds[pid] in wanted]
    order = (
        *front,
        *(pid for pid in placement if pid not in front),
        *(pid for pid in spec_order if pid not in placement),
    )
    record = {
        "source": "default_policy",
        "rule": f"default:{cls}",
        "class": cls,
        "signals": signals,
        "matched": signals,
        "prefer_kinds": list(wanted),
        "kinds": kinds,
        "spec_order": list(spec_order),
        "order": list(order),
        "reordered": order != spec_order,
    }
    unavailable = [pid for pid, kind in kinds.items() if kind == "unknown"]
    if unavailable:
        # A candidate the registry no longer knows cannot be ranked; it is named so the receipt
        # shows why a preferred destination was not tried first.
        record["unavailable"] = unavailable
    if not front:
        record["note"] = f"no {wanted[0]} candidate is listed, so the spec's order stands"
    return order, record


def preferred_order(
    stage: NormalStage, guard_signals: Iterable[str] = ()
) -> tuple[tuple[str, ...], dict[str, Any] | None]:
    """The stage's candidate list with the first matching operator rule applied, else the
    built-in default policy (ADR 0088), and the record of what ordered it (None when nothing
    did, and the list is then the spec's own order).

    Only the stage's own provider and ``alternate_providers`` move: the preferred ones that
    the stage lists come first, the rest keep the spec's order, and ``reroute_providers``-only
    targets stay at the end (ADR 0058). A stage without alternates is never reordered.
    ``guard_signals`` are passed to the default policy (see ``default_order``)."""
    spec_order = stage.candidate_ids
    if _ACTIVE is not None:
        for rule in _ACTIVE.rules:
            hits = rule.when.matched(stage)
            if not hits:
                continue
            placement = stage.placement_ids
            front = [pid for pid in rule.prefer if pid in placement]
            order = (
                *front,
                *(pid for pid in placement if pid not in front),
                *(pid for pid in spec_order if pid not in placement),
            )
            return order, {
                "rule": rule.name,
                "matched": hits,
                "prefer": list(rule.prefer),
                "spec_order": list(spec_order),
                "order": list(order),
                "reordered": order != spec_order,
            }
    if default_enabled() and stage.alternates:
        return default_order(stage, guard_signals)
    return spec_order, None


def status_view(stages: Iterable[NormalStage]) -> dict[str, Any]:
    """Read-only inspection (ADR 0070): the operator's rules and, for each stage a rule
    matches, the rule, the criteria met, the preferred providers the stage lists, and
    ``applies``. A rule applies only when it changes the order the spec wrote: the stage has
    ``alternate_providers`` and lists a preferred provider that is not already first.
    Otherwise the stage runs in its spec order, and the spec author would add the preferred
    provider as an alternate for the preference to take effect."""
    stages = list(stages)
    rules = [] if _ACTIVE is None else _ACTIVE.rules
    covered = []
    defaults = []
    if default_enabled():
        from .campaign_heavy_guard import heavy as guard_heavy

        for stage in stages:
            if not stage.alternates:
                continue
            # The store-derived ``large_inputs`` signal needs a connection and is not shown here.
            order, record = default_order(stage, guard_heavy(stage)[1])
            cls = record["class"] if record is not None else stage_class(stage)[0]
            signals = record["signals"] if record is not None else stage_class(stage)[1]
            defaults.append(
                {
                    "stage_key": stage.key,
                    "class": cls,
                    "signals": signals,
                    "order": list(order),
                    "applies": record is not None and record["reordered"],
                }
            )
    for stage in stages:
        for rule in rules:
            hits = rule.when.matched(stage)
            if not hits:
                continue
            listed = [pid for pid in rule.prefer if pid in stage.placement_ids]
            order, _ = preferred_order(stage)
            covered.append(
                {
                    "stage_key": stage.key,
                    "rule": rule.name,
                    "matched": hits,
                    "preferred_listed": listed,
                    # True only when the rule changes the order the spec wrote.
                    "applies": bool(stage.alternates) and order != stage.candidate_ids,
                }
            )
            break
    memory, seconds = _thresholds()
    return {
        "configured": _ACTIVE is not None,
        "rules": [rule.model_dump(mode="json", exclude_none=True) for rule in rules],
        "stages": covered,
        "default_policy": {
            "enabled": default_enabled(),
            "heavy_memory_bytes": memory,
            "long_runtime_seconds": seconds,
            "stages": defaults,
        },
    }
