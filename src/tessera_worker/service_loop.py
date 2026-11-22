"""Shared loop for always-on Tessera services (ADR 0038 amendment, unattended loops).

One contract a service supervisor can wrap, with no unit file shipped: run forever (or
``once``), SIGTERM/SIGINT stop new work after the current step and exit 0, an optional
health file is replaced atomically (0600) after each loop, and logs are bounded JSON
lines that carry error class names only. No port is opened.

The health file is written before the first step and after each loop, never during a
step. A long step (the Salad executor's route window) leaves ``last_loop_at`` stale for
its whole length, so a supervisor must treat health as stale only beyond the longest
step plus one poll interval; ``started_at`` and ``loops`` still show the process is the
one it started.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HEALTH_SCHEMA = "tessera.service-health.v1"
STOP_POLL_SECONDS = 0.5


class StopFlag:
    """Set by SIGTERM or SIGINT; the loop checks it between steps and while waiting."""

    def __init__(self) -> None:
        self.requested = False
        self._event = threading.Event()

    def request(self, *_: Any) -> None:
        self.requested = True
        self._event.set()

    def install(self) -> None:
        """Route SIGTERM and SIGINT here; idempotent."""
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, self.request)

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds`` (interruptible); True when stop was requested."""
        return self._event.wait(seconds)


def write_health(path: Path, payload: dict[str, Any]) -> None:
    """Replace ``path`` atomically with a 0600 file (temp file in the same directory)."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(payload, sort_keys=True).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def log_line(event: dict[str, Any], *, stream=None, cap: int = 2048) -> None:
    """One JSON line, truncated to ``cap`` characters."""
    line = json.dumps(event, sort_keys=True, default=str)
    if len(line) > cap:
        line = json.dumps({"truncated": True, "head": line[: max(cap - 40, 0)]})[:cap]
    print(line, file=stream or sys.stdout, flush=True)


def run_service(
    *,
    step: Callable[[], Any],
    interval: float,
    once: bool,
    health_file: Path | None,
    name: str,
    stop: StopFlag | None = None,
    clock: Callable[[], float] = time.time,
    wait: Callable[[float], bool] | None = None,
    max_cycles: int | None = None,
) -> int:
    """Call ``step`` until stopped, once, or ``max_cycles`` steps; return 0.

    Step errors never end the loop. ``max_cycles`` counts every step, failed or not.
    """
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    stop = stop or StopFlag()
    wait = wait or stop.wait
    started = clock()
    loops = errors = 0
    last_error: str | None = None
    state = "running"

    def health() -> None:
        if health_file is None:
            return
        try:
            write_health(
                health_file,
                {
                    "schema": HEALTH_SCHEMA,
                    "name": name,
                    "pid": os.getpid(),
                    "state": state,
                    "started_at": started,
                    "last_loop_at": clock(),
                    "loops": loops,
                    "consecutive_errors": errors,
                    "last_error_class": last_error,
                },
            )
        except OSError:
            log_line({"service": name, "action": "health_write_failed"})

    health()  # visible to a supervisor before the first, possibly long, step
    while not stop.requested:
        try:
            result = step()
            errors = 0
            for entry in result if isinstance(result, list) else []:
                log_line(entry if isinstance(entry, dict) else {"entry": str(entry)})
        except Exception as error:  # noqa: BLE001 - class name only, loop continues
            errors += 1
            last_error = type(error).__name__
            log_line({"service": name, "action": "step_error", "error_class": last_error})
        loops += 1
        if once or stop.requested or (max_cycles is not None and loops >= max_cycles):
            break
        health()
        remaining = interval
        while remaining > 0 and not stop.requested:
            slice_seconds = min(remaining, STOP_POLL_SECONDS)
            if wait(slice_seconds):
                break
            remaining -= slice_seconds
    state = "stopped"
    health()
    return 0
