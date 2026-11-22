"""Operator configuration for the worker's stage watcher (ADR 0087, observe and report only).

Off by default. This module imports nothing from ``tessera_worker.stage_watcher``, so a worker
that never enables the watcher never loads the watcher library: the runner
(``tessera_worker.stage_watch_runner``) is imported lazily, only on the enabled branch.

``kill_switch_path`` is required for a watcher to start. An enabled config without one is
accepted here (so a worker is never refused because of the watcher), but the runner starts no
watcher at all: fail closed, it reads as engaged and nothing is observed or reported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from tessera_control.stage_watch_limits import MAX_REPORTS_PER_OPERATION

DEFAULT_SILENT_SECONDS = 900
DEFAULT_HEARTBEAT_SECONDS = 60
DEFAULT_MAX_REPORTS = MAX_REPORTS_PER_OPERATION
# Per-report HTTP time cap for a watch report: short, so a slow control plane never holds the
# supervisor's tick (a timed-out report stays in the outbox and is retried next tick).
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 2.0
# Per supervisor tick, across ALL watchers: the runner shares the supervisor's thread with
# lease renewal, so a backlog (or a slow control plane) must never hold that thread. What does
# not fit stays recorded in the outbox for a later tick.
DEFAULT_TICK_BUDGET_SECONDS = 1.0
MIN_TICK_BUDGET_SECONDS = 0.1
MAX_TICK_BUDGET_SECONDS = 5.0
DEFAULT_TICK_MAX_POSTS = 4
MAX_TICK_POSTS = 32
MAX_SECONDS = 7 * 24 * 3600
_MAX_PATH_BYTES = 4096


def _bounded_int(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"stage watcher {name} must be an integer in {minimum}..{maximum}.")


@dataclass(frozen=True)
class StageWatchConfig:
    """Closed, validated stage watcher settings. ``StageWatchConfig()`` is disabled."""

    enabled: bool = False
    kill_switch_path: str | None = None
    silent_seconds: int = DEFAULT_SILENT_SECONDS
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    max_reports: int = DEFAULT_MAX_REPORTS
    delivery_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS
    # Also sample ``docker stats`` on the local Docker probe, so ``mem_bytes`` reaches the
    # report counters. Off by default (one extra read-only Docker call per tick).
    stats: bool = False
    tick_budget_seconds: float = DEFAULT_TICK_BUDGET_SECONDS
    tick_max_posts: int = DEFAULT_TICK_MAX_POSTS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("stage watcher enabled must be a bool.")
        if not isinstance(self.stats, bool):
            raise ValueError("stage watcher stats must be a bool.")
        path = self.kill_switch_path
        if path is not None:
            if isinstance(path, os.PathLike):
                path = os.fspath(path)
                object.__setattr__(self, "kill_switch_path", path)
            if (
                not isinstance(path, str)
                or not path
                or "\x00" in path
                or len(path.encode("utf-8", "surrogateescape")) > _MAX_PATH_BYTES
                or not os.path.isabs(path)
            ):
                raise ValueError("stage watcher kill switch path must be a bounded absolute path.")
        _bounded_int("silent_seconds", self.silent_seconds, 1, MAX_SECONDS)
        _bounded_int("heartbeat_seconds", self.heartbeat_seconds, 1, MAX_SECONDS)
        # At least 2: the watcher keeps the last slot for ``failed``.
        _bounded_int("max_reports", self.max_reports, 2, MAX_REPORTS_PER_OPERATION)
        timeout = self.delivery_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.1 <= timeout <= 10
        ):
            raise ValueError("stage watcher delivery timeout must be 0.1..10 seconds.")
        budget = self.tick_budget_seconds
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not MIN_TICK_BUDGET_SECONDS <= budget <= MAX_TICK_BUDGET_SECONDS
        ):
            raise ValueError(
                f"stage watcher tick budget must be {MIN_TICK_BUDGET_SECONDS}.."
                f"{MAX_TICK_BUDGET_SECONDS} seconds."
            )
        _bounded_int("tick_max_posts", self.tick_max_posts, 1, MAX_TICK_POSTS)

    @property
    def startable(self) -> bool:
        """Enabled and with a kill switch: the only state in which a watcher may start."""
        return self.enabled and self.kill_switch_path is not None
