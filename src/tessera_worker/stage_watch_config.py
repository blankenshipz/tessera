"""Operator configuration for the worker's stage watcher (ADR 0087, observe and report only).

On by default (ADR 0125): a worker with no explicit setting watches every local Docker stage
with the kill-switch marker ``<private workspace root>/stage-watch.kill`` (never created here;
present means engaged). Opt out only explicitly: ``--no-stage-watcher`` or
``"stage_watcher": {"enabled": false}``. This module imports nothing from
``tessera_worker.stage_watcher``, so a worker that opted out never loads the watcher library:
the runner (``tessera_worker.stage_watch_runner``) is imported lazily, only on the enabled
branch.

An enabled config without a ``kill_switch_path`` (a block that only tunes, or ``--stage-watcher``
alone) takes the default marker ``<private workspace root>/stage-watch.kill``; if that directory is
not usable it resolves to no watcher with one stderr line. A mapping (the poller's
``stage_watcher`` block) that omits ``enabled`` is enabled: opting out is the explicit
``"enabled": false``.
"""

from __future__ import annotations

import dataclasses
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

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
DEFAULT_KILL_SWITCH_NAME = "stage-watch.kill"


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

    @classmethod
    def from_mapping(cls, document: Any) -> StageWatchConfig:
        """A closed, strictly validated config from a JSON object; ``ValueError`` otherwise.

        Unknown keys are refused. The message is bounded and carries no value or path.
        """
        allowed = {field.name for field in fields(cls)}
        if not isinstance(document, Mapping) or not set(document) <= allowed:
            raise ValueError("stage watcher settings must be an object with known keys only.")
        # A block that is present is explicit configuration: tuning alone keeps the watcher on.
        return cls(**({"enabled": True} | dict(document)))


def _private_directory_reason(directory: Path) -> str | None:
    try:
        info = directory.lstat()
    except OSError:
        return "directory is unavailable"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "directory is not a plain directory"
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        return "directory is not private"
    return None


def default_stage_watch(directory: Path | str) -> tuple[StageWatchConfig, str | None]:
    """The default-on config for a worker whose private state directory is ``directory``.

    The marker ``<directory>/stage-watch.kill`` is only named, never created. When the
    directory is not private and usable, or the path is not a valid marker path, returns a
    disabled config and a short path-free reason: the worker still runs, with no watcher.
    """
    path = Path(os.path.abspath(directory))
    reason = _private_directory_reason(path)
    if reason is None:
        try:
            marker = str(path / DEFAULT_KILL_SWITCH_NAME)
            return StageWatchConfig(enabled=True, kill_switch_path=marker), None
        except ValueError:
            reason = "marker path is not valid"
    return StageWatchConfig(), reason


def resolve_stage_watch(
    explicit: StageWatchConfig | None, directory: Path | str
) -> StageWatchConfig:
    """``explicit`` when given (it wins, disabled included), else the default-on config.

    An enabled ``explicit`` without a marker path takes the default marker, like no config at all
    (tuning or ``--stage-watcher`` alone never switches the watcher off). A default that cannot be
    used is announced on stderr and resolves to no watcher.
    """
    if explicit is not None and not (explicit.enabled and explicit.kill_switch_path is None):
        return explicit
    config, reason = default_stage_watch(directory)
    if explicit is not None and reason is None:
        config = dataclasses.replace(explicit, kill_switch_path=config.kill_switch_path)
    if reason is not None:
        print(
            f"stage watcher: default marker unusable ({reason}); no watcher will start",
            file=sys.stderr,
        )
    return config
