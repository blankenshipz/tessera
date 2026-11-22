"""Read-only compute probes for the stage watcher.

A probe only reads the state of the attempt's own workload. It never starts, stops, resizes or
otherwise changes it, and it never raises on a bad or missing reply: anything it cannot read
cleanly is reported as state "unknown".
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

ProbeState = Literal["running", "exited", "absent", "unknown"]
PROBE_STATES: frozenset[str] = frozenset({"running", "exited", "absent", "unknown"})

# Same shape as tessera_worker.cpu.RunCommand: the arguments AFTER the `docker` executable.
RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_MEMORY = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*")
_MEMORY_UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}
_RUNNING_STATUSES = frozenset({"running", "restarting", "paused"})
_EXITED_STATUSES = frozenset({"exited", "dead"})


def _optional_count(name: str, value: object) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be None or a non-negative integer.")


@dataclass(frozen=True)
class ProbeSample:
    """One read-only observation of the attempt's workload.

    ``progress_marker`` is an internal liveness counter for probes with no byte or CPU
    counters (it is never a report counter). Any change in it counts as progress, and its
    presence alone makes ``has_progress_signal`` true. For a remote provider the marker is
    advanced by the provider-side driver once per successful poll, so a ``stuck`` built on it
    means "the provider-side driver stopped observing", not "the workload stalled".
    """

    state: ProbeState
    observed_at: datetime
    exit_code: int | None = None
    oom_killed: bool = False
    output_bytes: int | None = None
    cpu_ms: int | None = None
    mem_bytes: int | None = None
    restarts: int = 0
    progress_marker: int | None = None

    def __post_init__(self) -> None:
        if self.state not in PROBE_STATES:
            raise ValueError("state must be running, exited, absent or unknown.")
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime.")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code must be None or an integer.")
        if not isinstance(self.oom_killed, bool):
            raise ValueError("oom_killed must be a bool.")
        for name in ("output_bytes", "cpu_ms", "mem_bytes", "progress_marker"):
            _optional_count(name, getattr(self, name))
        if isinstance(self.restarts, bool) or not isinstance(self.restarts, int):
            raise ValueError("restarts must be a non-negative integer.")
        if self.restarts < 0:
            raise ValueError("restarts must be a non-negative integer.")

    @property
    def has_progress_signal(self) -> bool:
        return (
            self.output_bytes is not None
            or self.cpu_ms is not None
            or self.progress_marker is not None
        )


class ComputeProbe(Protocol):
    def sample(self, now: datetime) -> ProbeSample: ...


def parse_memory_usage(value: object) -> int | None:
    """Parse the used part of `docker stats` MemUsage ("12.5MiB / 1GiB"); None if unreadable."""
    if not isinstance(value, str):
        return None
    match = _MEMORY.fullmatch(value.split("/", 1)[0])
    if match is None or match.group(2) not in _MEMORY_UNITS:
        return None
    return int(float(match.group(1)) * _MEMORY_UNITS[match.group(2)])


class DockerInspectProbe:
    """Observe one container through an injected Docker command runner.

    Calls only `inspect --type container <name>` and, when `stats` is set and the container is
    running, `stats --no-stream --format {{json .}} <name>`, both through `run` (which supplies
    the `docker` executable, as `tessera_worker.cpu._docker` does). `expected_labels`, when set,
    must all match the container's labels; a container that does not carry them is not ours
    and reads as "unknown". `output_size` is an optional read-only measure of the attempt's
    declared output bytes (Docker reports no cumulative CPU time or output size).
    """

    def __init__(
        self,
        run: RunCommand,
        container_name: str,
        *,
        expected_labels: Mapping[str, str] | None = None,
        stats: bool = False,
        output_size: Callable[[], int | None] | None = None,
    ):
        if not isinstance(container_name, str) or not _CONTAINER_NAME.fullmatch(container_name):
            raise ValueError("container_name must be a valid Docker container name.")
        self.run = run
        self.container_name = container_name
        self.expected_labels = dict(expected_labels or {})
        self.stats = stats
        self.output_size = output_size

    def sample(self, now: datetime) -> ProbeSample:
        try:
            reply = self.run(["inspect", "--type", "container", self.container_name])
        except Exception:
            return ProbeSample("unknown", now)
        try:
            if reply.returncode:
                stderr = reply.stderr if isinstance(reply.stderr, str) else ""
                # A stopped daemon or an authorization failure is not absence.
                if "No such container" in stderr or "No such object" in stderr:
                    return ProbeSample("absent", now)
                return ProbeSample("unknown", now)
            return self._parse(reply.stdout, now)
        except (AttributeError, KeyError, TypeError, ValueError):
            return ProbeSample("unknown", now)

    def _parse(self, stdout: str, now: datetime) -> ProbeSample:
        items = json.loads(stdout)
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("unexpected inspect shape")
        info = items[0]
        labels = (info.get("Config") or {}).get("Labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("labels")
        for key, value in self.expected_labels.items():
            if labels.get(key) != value:
                return ProbeSample("unknown", now)
        state = info["State"]
        if not isinstance(state, dict):
            raise ValueError("state")
        status = state["Status"]
        oom_killed = state.get("OOMKilled", False)
        restarts = info.get("RestartCount", 0)
        if not isinstance(oom_killed, bool):
            raise ValueError("oom")
        if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
            raise ValueError("restarts")
        if status in _RUNNING_STATUSES:
            return ProbeSample(
                "running",
                now,
                oom_killed=oom_killed,
                output_bytes=self._output_bytes(),
                mem_bytes=self._mem_bytes(),
                restarts=restarts,
            )
        if status in _EXITED_STATUSES and state.get("Running") is False:
            exit_code = state.get("ExitCode")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise ValueError("exit code")
            return ProbeSample(
                "exited",
                now,
                exit_code=exit_code,
                oom_killed=oom_killed,
                output_bytes=self._output_bytes(),
                restarts=restarts,
            )
        return ProbeSample("unknown", now)

    def _output_bytes(self) -> int | None:
        if self.output_size is None:
            return None
        try:
            value = self.output_size()
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _mem_bytes(self) -> int | None:
        if not self.stats:
            return None
        try:
            reply = self.run(
                ["stats", "--no-stream", "--format", "{{json .}}", self.container_name]
            )
            if reply.returncode:
                return None
            payload = json.loads(reply.stdout)
            if not isinstance(payload, dict):
                return None
            return parse_memory_usage(payload.get("MemUsage"))
        except Exception:
            return None
