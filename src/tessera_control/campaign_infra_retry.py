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


def infra_attempts(attempts: Sequence[Mapping[str, Any]]) -> int:
    """Attempts of a stage that ended for an infrastructure reason."""
    return sum(1 for a in attempts if str(a["failure_class"] or "").startswith(INFRA_PREFIX))
