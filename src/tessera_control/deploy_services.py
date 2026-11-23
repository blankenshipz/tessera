"""Stop, start and health-wait steps for the deploy upgrade (no shell, no secrets)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

Runner = Callable[[list[str], int], tuple[int, str]]

HEALTH_SCHEMA = "tessera.service-health.v1"
MAX_OUTPUT_CHARS = 2000
MAX_HEALTH_FILE_BYTES = 64 * 1024
POLL_INTERVAL_SECONDS = 2.0


class HealthLike(Protocol):
    kind: str
    path: str
    max_age_seconds: float
    url: str
    timeout_seconds: float


class ServiceLike(Protocol):
    name: str
    restart_argv: Sequence[str]
    stop_argv: Sequence[str] | None
    health: HealthLike
    health_wait_seconds: float
    command_timeout_seconds: int


@dataclass(frozen=True)
class StepResult:
    step: str
    name: str
    ok: bool
    seconds: float
    detail: str


def default_runner(argv: list[str], timeout: int) -> tuple[int, str]:
    """Run a fixed argv without a shell and with only PATH in the environment."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    try:
        completed = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as error:
        return 127, type(error).__name__
    output = (completed.stdout or b"") + (completed.stderr or b"")
    return completed.returncode, output.decode("utf-8", "replace")[:MAX_OUTPUT_CHARS]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _default_opener() -> Any:
    # An explicit empty ProxyHandler overrides the environment-derived default one.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _read_health_file(path: str) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        data = handle.read(MAX_HEALTH_FILE_BYTES + 1)
    if len(data) > MAX_HEALTH_FILE_BYTES:
        raise ValueError("oversized")
    return json.loads(data)


def _probe_file(
    health: HealthLike, now: Callable[[], float], not_before: float | None = None
) -> tuple[bool, str]:
    try:
        doc = _read_health_file(health.path)
        schema, state = doc.get("schema"), doc.get("state")
        written = float(doc["last_loop_at"])
        age = abs(now() - written)
        limit = float(health.max_age_seconds)
    except Exception as error:
        return False, f"unreadable health file ({type(error).__name__})"
    if schema != HEALTH_SCHEMA:
        return False, "unexpected health schema"
    if state != "running":
        return False, "service not running"
    if age > limit:
        return False, f"stale health file (age {age:.0f}s)"
    # A fresh file written by the process this run just replaced proves nothing: the new
    # process must have written it at or after the restart.
    if not_before is not None and written < not_before:
        return False, "health file predates the restart"
    return True, "healthy"


def _probe_http(health: HealthLike, opener: Any) -> tuple[bool, str]:
    try:
        active = opener if opener is not None else _default_opener()
        request = urllib.request.Request(health.url, method="GET")  # noqa: S310
        with active.open(request, timeout=health.timeout_seconds) as response:
            status = int(response.status)
    except urllib.error.HTTPError as error:
        return False, f"http status {error.code}"
    except Exception as error:
        return False, f"http probe failed ({type(error).__name__})"
    if 200 <= status < 300:
        return True, "healthy"
    return False, f"http status {status}"


def probe_health(
    health: HealthLike,
    *,
    now: Callable[[], float] = time.time,
    opener: Any = None,
    not_before: float | None = None,
) -> tuple[bool, str]:
    """Return (healthy, short reason); never includes file or response content.

    ``not_before`` (wall-clock seconds) is the restart time: a health file whose
    ``last_loop_at`` is earlier was written by the old process and does not count."""
    if health.kind == "file":
        return _probe_file(health, now, not_before)
    if health.kind == "http":
        return _probe_http(health, opener)
    return False, "unknown health kind"


def _run(runner: Runner, argv: Sequence[str], timeout: int) -> tuple[int, str]:
    try:
        return runner(list(argv), timeout)
    except Exception as error:
        return 127, type(error).__name__


def stop_services(
    services: Sequence[ServiceLike],
    runner: Runner,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> list[StepResult]:
    """Stop services in reverse order; every stop is attempted even after a failure."""
    results: list[StepResult] = []
    for service in reversed(list(services)):
        if not service.stop_argv:
            continue
        started = clock()
        code, _ = _run(runner, service.stop_argv, service.command_timeout_seconds)
        results.append(
            StepResult(
                "stop",
                service.name,
                code == 0,
                round(clock() - started, 3),
                f"exit {code}",
            )
        )
    return results


def start_services(
    services: Sequence[ServiceLike],
    runner: Runner,
    *,
    probe: Callable[..., tuple[bool, str]] = probe_health,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> list[StepResult]:
    """Restart in order, waiting for health; stop at the first failure.

    The probe is called as ``probe(health, not_before=restart_wall_time)``."""
    results: list[StepResult] = []
    for service in services:
        started = clock()
        restarted_at = wall()
        code, _ = _run(runner, service.restart_argv, service.command_timeout_seconds)
        ok = code == 0
        results.append(
            StepResult("restart", service.name, ok, round(clock() - started, 3), f"exit {code}")
        )
        if not ok:
            return results
        waited_from = clock()
        while True:
            healthy, reason = probe(service.health, not_before=restarted_at)
            elapsed = clock() - waited_from
            if healthy:
                results.append(StepResult("health", service.name, True, round(elapsed, 3), reason))
                break
            if elapsed >= service.health_wait_seconds:
                detail = f"not healthy within {service.health_wait_seconds:g}s: {reason}"
                results.append(StepResult("health", service.name, False, round(elapsed, 3), detail))
                return results
            sleep(POLL_INTERVAL_SECONDS)
    return results
