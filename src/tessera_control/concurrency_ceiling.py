"""The operator's ceiling on a project's execution concurrency (ADR 0171).

A project's ``max_concurrency`` (how many execution grants may be unresolved at once) is set by
a human through ``PUT /projects/{id}/execution-limits``. This module bounds that value for the
whole installation. The ceiling is 8 unless the operator sets the environment variable below for
the control service before it starts; it is read once, on first use, by
``operator_concurrency_ceiling`` (the app factory reads it at startup).

Only the operator's environment sets it: no API field, MCP tool, campaign spec or stage input
reaches it. A value that is not a plain ASCII whole number, or lies outside 1 to
``HARD_PROJECT_CONCURRENCY_CAP``, stops startup with a clear error rather than falling back to
any other value. An empty value counts as unset (the default 8), as for the other operator limits.
The hard cap is also the database CHECK on ``execution_project_limits.max_concurrency``.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENV_PROJECT_CONCURRENCY_CEILING = "TESSERA_MAX_PROJECT_CONCURRENCY"
DEFAULT_PROJECT_CONCURRENCY_CEILING = 8
# The largest ceiling an operator may configure. Every grant admission counts the project's
# unresolved grants, and each grant holds a lease, a worker slot and exposure; 64 is eight times
# the default and well past one host's useful parallelism. Raising it needs a migration.
HARD_PROJECT_CONCURRENCY_CAP = 64

_DIGITS = re.compile(r"[0-9]{1,6}")


class ConcurrencyCeilingError(ValueError):
    """The operator's project concurrency ceiling is malformed or out of range."""


@dataclass(frozen=True)
class ConcurrencyCeiling:
    value: int = DEFAULT_PROJECT_CONCURRENCY_CEILING
    # True when the operator set the environment variable (even to 8).
    configured: bool = False

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 1 <= self.value <= HARD_PROJECT_CONCURRENCY_CAP:
            raise ConcurrencyCeilingError(
                f"{ENV_PROJECT_CONCURRENCY_CEILING} must be a whole number from 1 to "
                f"{HARD_PROJECT_CONCURRENCY_CAP}."
            )

    def evidence(self) -> dict[str, Any]:
        """The field recorded on ``execution.limits_configured`` (absent means the default 8)."""
        return {"concurrency_ceiling": self.value} if self.configured else {}

    def admits(self, stored_max_concurrency: int) -> int:
        """The concurrency a grant admission enforces: the stored limit, never above the ceiling."""
        return min(int(stored_max_concurrency), self.value)


def concurrency_ceiling(environ: Mapping[str, str] | None = None) -> ConcurrencyCeiling:
    """The effective ceiling: the default, or the operator's environment value."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_PROJECT_CONCURRENCY_CEILING)
    if raw is None or raw == "":
        return ConcurrencyCeiling()
    if not isinstance(raw, str) or _DIGITS.fullmatch(raw) is None:
        raise ConcurrencyCeilingError(
            f"{ENV_PROJECT_CONCURRENCY_CEILING} must be a whole number from 1 to "
            f"{HARD_PROJECT_CONCURRENCY_CAP} (ASCII digits only)."
        )
    return ConcurrencyCeiling(int(raw), configured=True)


@functools.lru_cache(maxsize=1)
def operator_concurrency_ceiling() -> ConcurrencyCeiling:
    """The process-wide ceiling, read from the environment once.

    Tests that change the environment call ``operator_concurrency_ceiling.cache_clear()``."""
    return concurrency_ceiling()
