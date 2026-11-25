"""Infrastructure retries of a campaign stage (ADR 0058).

A stage attempt can end because the infrastructure under it failed, not because the task
did: the engine or the kernel killed the container (exit 137, the out-of-memory killer
among them). The runner then retries the stage itself, within a bound the approved spec
sets (``retry.infra_retries``, default 2), and nothing else qualifies: a task failure, a
refused step, a policy denial or a budget refusal is never retried here, and a failure
whose effect is unknown stays with a person.

Each retry is a new attempt with its own hold (charged against the campaign budget and the
project limits like any attempt), its own receipt entry and the reason recorded; it never
uses a scientific retry. Where the spec names ``reroute_providers`` the next attempt may
run on one of those registered providers instead, never on one it does not name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

INFRA_PREFIX = "infra_"
INFRA_CONTAINER_KILLED = "infra_container_killed"
# ADR 0067: a stage that stopped because it reached a resource limit (its time budget, as its
# own stop metric reports, or a kill at the stage time limit or for memory).
INFRA_RESOURCE_LIMIT = "infra_resource_limit"
RESOURCE_LIMIT_CLASSES = frozenset({INFRA_CONTAINER_KILLED, INFRA_RESOURCE_LIMIT})


# ADR 0179: the provider moved the instance before any custody work (begin-once, an input
# served, an output requested or written). Nothing was produced or received, so a FRESH attempt
# on a new instance may retry it; the old instance is never re-bound. Bounded by
# ``retry.max_reallocation_retries``, and every attempt keeps its own hold under the budget cap.
REALLOCATED_PRECUSTODY = "provider_reallocated_before_custody"


def reallocation_attempts(attempts: Sequence[Mapping[str, Any]]) -> int:
    """Attempts of a stage that ended on a reallocation before custody began."""
    return sum(1 for a in attempts if (a["failure_class"] or "") == REALLOCATED_PRECUSTODY)


def infra_attempts(attempts: Sequence[Mapping[str, Any]]) -> int:
    """Attempts of a stage that ended for an infrastructure reason."""
    return sum(1 for a in attempts if str(a["failure_class"] or "").startswith(INFRA_PREFIX))


def resource_limit_stop(
    metrics: Sequence[str], measurements_json: str | None
) -> tuple[str, float] | None:
    """The first declared stop metric whose recorded value is above zero, else ``None``.

    Only a value the platform measured counts: a metric with no recorded value, a non-number
    or zero is not a stop."""
    if not metrics or not measurements_json:
        return None
    import json

    recorded = {
        item.get("name"): item.get("value")
        for item in json.loads(measurements_json)
        if isinstance(item, dict)
    }
    for name in metrics:
        value = recorded.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return name, float(value)
    return None
