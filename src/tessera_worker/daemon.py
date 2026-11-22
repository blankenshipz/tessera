"""Generic supervisor that keeps one worker subcommand running.

The daemon runs ``python -m tessera_worker.cli <subcommand> ...`` as a child in its
own process group, restarts it according to its exit status, and publishes a small
status file. It never starts, stops or removes Docker containers; an interrupted run
is recovered by the worker itself on its next start.

Diagnostics and the status file carry only the subcommand name and numbers, never the
worker arguments, paths, control URLs or credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import random
import signal
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .exit_codes import EXIT_CREDENTIAL_REJECTED, EXIT_OK, EXIT_TERMINAL

# Worker subcommands the daemon may supervise. Extend this set to supervise more.
ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset({"development-run", "development-poll"})

EX_USAGE = 64
EX_CANTCREAT = 73
EX_TEMPFAIL = 75

STATUS_FILE = "status.json"
LOCK_FILE = "daemon.lock"
JITTER_FRACTION = 0.25
HEARTBEAT_SECONDS = 10.0
TICK_SECONDS = 0.5
BAD_STATES = frozenset({"stopped", "failed"})


class DaemonError(Exception):
    """A refusal with a process exit code and a message free of arguments."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DaemonConfig:
    backoff_min: float = 2.0
    backoff_max: float = 120.0
    auth_backoff_min: float = 60.0
    auth_backoff_max: float = 900.0
    stable_seconds: float = 60.0
    grace_seconds: float = 30.0
    restart_delay: float = 1.0
    poll_interval: float = TICK_SECONDS
    heartbeat_seconds: float = HEARTBEAT_SECONDS

    def validate(self) -> None:
        if min(self.backoff_min, self.auth_backoff_min, self.poll_interval) <= 0:
            raise DaemonError("backoff minimums must be positive", EX_USAGE)
        if self.backoff_max < self.backoff_min:
            raise DaemonError("--backoff-max must be at least --backoff-min", EX_USAGE)
        if self.auth_backoff_max < self.auth_backoff_min:
            raise DaemonError("--auth-backoff-max must be at least --auth-backoff-min", EX_USAGE)
        if min(self.stable_seconds, self.grace_seconds, self.restart_delay) < 0:
            raise DaemonError("durations must not be negative", EX_USAGE)


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def signal_group(self, number: int) -> None: ...


class ProcessChild:
    """A real child started in its own session and process group."""

    def __init__(self, argv: Sequence[str]) -> None:
        import subprocess

        self._process = subprocess.Popen(list(argv), start_new_session=True)
        self.pid = self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def signal_group(self, number: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, number)


def spawn_process(argv: Sequence[str]) -> Child:
    return ProcessChild(argv)


def classify_exit(code: int) -> str:
    if code == EXIT_OK:
        return "ok"
    if code == EXIT_CREDENTIAL_REJECTED:
        return "credential_rejected"
    if code < 0:
        return "signal"
    if code == EXIT_TERMINAL:
        return "terminal"
    return "failure"


def backoff_delay(step: int, minimum: float, maximum: float, jitter: float) -> float:
    """Exponential delay for failure number ``step`` (1-based).

    Jitter only lengthens the delay, so it is never below ``minimum``.
    """
    base = min(maximum, minimum * (2 ** max(0, step - 1)))
    return min(maximum, base * (1 + JITTER_FRACTION * min(1.0, max(0.0, jitter))))


def validate_worker_args(worker_args: Sequence[str]) -> str:
    if not worker_args:
        raise DaemonError("missing worker arguments after '--'", EX_USAGE)
    name = worker_args[0]
    if name not in ALLOWED_SUBCOMMANDS:
        allowed = ", ".join(sorted(ALLOWED_SUBCOMMANDS))
        raise DaemonError(f"unsupported worker subcommand (allowed: {allowed})", EX_USAGE)
    return name


def default_install_signals(handler: Callable[[], None]) -> None:
    def _handle(_number: int, _frame: Any) -> None:
        handler()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def write_status_file(state_dir: Path, document: dict[str, Any]) -> None:
    """Atomically replace the status file with a 0600 file."""
    temporary = state_dir / (STATUS_FILE + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    os.replace(temporary, state_dir / STATUS_FILE)


def prepare_state_dir(path: Path) -> Path:
    """Create or validate a private, user-owned, non-symlink state directory."""
    try:
        if not os.path.lexists(path):
            os.mkdir(path, 0o700)
        info = os.lstat(path)
    except OSError as error:
        raise DaemonError(
            f"cannot prepare state directory: {error.strerror}", EX_CANTCREAT
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise DaemonError("state directory must not be a symlink", EX_CANTCREAT)
    if not stat.S_ISDIR(info.st_mode):
        raise DaemonError("state directory is not a directory", EX_CANTCREAT)
    if info.st_uid != os.geteuid():
        raise DaemonError("state directory is not owned by the current user", EX_CANTCREAT)
    if info.st_mode & 0o077:
        raise DaemonError(
            "state directory must not be accessible by group or others (0700)", EX_CANTCREAT
        )
    return path


def acquire_lock(state_dir: Path) -> int:
    """Take the single-instance lock; the returned descriptor must stay open."""
    try:
        descriptor = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise DaemonError(f"cannot open lock file: {error.strerror}", EX_CANTCREAT) from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise DaemonError(
            "another worker daemon holds the lock for this state directory", EX_TEMPFAIL
        ) from None
    return descriptor


class Supervisor:
    """Restart policy and shutdown handling; all effects are injected."""

    def __init__(
        self,
        worker_args: Sequence[str],
        config: DaemonConfig,
        *,
        spawn: Callable[[Sequence[str]], Child] = spawn_process,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        install_signals: Callable[[Callable[[], None]], None] = default_install_signals,
        write_status: Callable[[dict[str, Any]], None] = lambda document: None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.subcommand = validate_worker_args(worker_args)
        config.validate()
        self._argv = [sys.executable, "-m", "tessera_worker.cli", *worker_args]
        self._config = config
        self._spawn = spawn
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._jitter = jitter
        self._install_signals = install_signals
        self._write_status = write_status
        self._log = log if log is not None else _stderr_log
        self._stop = False
        self._child: Child | None = None
        self._child_started = 0.0
        self._state = "starting"
        self._started_at = wall()
        self._restarts = 0
        self._starts = 0
        self._failures = 0
        self._last_exit_code: int | None = None
        self._last_exit_kind: str | None = None
        self._last_start: float | None = None
        self._next_start: float | None = None
        self._stable = False
        self._last_publish = clock()
        self._status_warned = False
        self._runtime = 0.0

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> int:
        self._install_signals(self.request_stop)
        self._publish("starting")
        try:
            while not self._stop:
                code = self._run_child()
                if code is None:
                    break
                self._wait(*self._after_exit(code))
            self._shutdown()
        except BaseException:
            self._child = None
            self._publish("failed")
            raise
        return 0

    # -- one child -----------------------------------------------------------------

    def _run_child(self) -> int | None:
        """Start a child and watch it; None when interrupted by a stop request."""
        began = self._clock()
        try:
            child = self._spawn(self._argv)
        except OSError:
            self._last_start = self._wall()
            self._runtime = 0.0
            return None if self._stop else 126  # cannot execute: an ordinary failure
        if self._starts:
            self._restarts += 1
        self._starts += 1
        self._child = child
        self._child_started = began
        self._stable = False
        self._last_start = self._wall()
        self._next_start = None
        self._publish("running")
        while not self._stop:
            code = child.poll()
            if code is not None:
                self._runtime = self._clock() - began
                self._child = None
                return code
            self._sleep(self._config.poll_interval)
            self._stable = self._clock() - began >= self._config.stable_seconds
            self._heartbeat("running")
        return None

    def _after_exit(self, code: int) -> tuple[float, str]:
        kind = classify_exit(code)
        self._last_exit_code = code
        self._last_exit_kind = kind
        if kind == "ok":
            self._failures = 0
            return self._config.restart_delay, "backoff"
        if self._runtime >= self._config.stable_seconds:
            self._failures = 0
        self._failures += 1
        jitter = self._jitter()
        if kind == "credential_rejected":
            delay = backoff_delay(
                self._failures, self._config.auth_backoff_min, self._config.auth_backoff_max, jitter
            )
            return delay, "credential_backoff"
        delay = backoff_delay(
            self._failures, self._config.backoff_min, self._config.backoff_max, jitter
        )
        return delay, "backoff"

    def _wait(self, delay: float, state: str) -> None:
        deadline = self._clock() + delay
        self._next_start = self._wall() + delay
        self._publish(state)
        while not self._stop:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(self._config.poll_interval, remaining))
            self._heartbeat(state)

    # -- shutdown ------------------------------------------------------------------

    def _shutdown(self) -> None:
        self._next_start = None
        self._publish("stopping")
        child = self._child
        if child is not None:
            child.signal_group(signal.SIGTERM)
            deadline = self._clock() + self._config.grace_seconds
            while child.poll() is None and self._clock() < deadline:
                self._sleep(min(self._config.poll_interval, max(0.0, deadline - self._clock())))
                self._heartbeat("stopping")
            if child.poll() is None:
                child.signal_group(signal.SIGKILL)
                for _ in range(20):
                    if child.poll() is not None:
                        break
                    self._sleep(self._config.poll_interval)
            else:
                # Remove stragglers the child left in its group.
                child.signal_group(signal.SIGKILL)
            self._child = None
        self._publish("stopped")

    # -- reporting -----------------------------------------------------------------

    def _heartbeat(self, state: str) -> None:
        if self._clock() - self._last_publish >= self._config.heartbeat_seconds:
            self._publish(state, log=False)

    def _publish(self, state: str, *, log: bool = True) -> None:
        self._state = state
        self._last_publish = self._clock()
        document = {
            "pid": os.getpid(),
            "state": state,
            "child_pid": self._child.pid if self._child is not None else None,
            "started_at": self._started_at,
            "updated_at": self._wall(),
            "restarts": self._restarts,
            "consecutive_failures": self._failures,
            "last_exit_code": self._last_exit_code,
            "last_exit_kind": self._last_exit_kind,
            "last_start": self._last_start,
            "next_start": self._next_start,
            "subcommand": self.subcommand,
            "stable": self._stable,
        }
        try:
            self._write_status(document)
        except OSError:
            if not self._status_warned:
                self._status_warned = True
                self._log(json.dumps({"event": "status_write_failed"}))
        if log:
            self._log(
                json.dumps(
                    {
                        "event": "transition",
                        "state": state,
                        "child_pid": document["child_pid"],
                        "restarts": self._restarts,
                        "consecutive_failures": self._failures,
                        "last_exit_code": self._last_exit_code,
                        "last_exit_kind": self._last_exit_kind,
                        "next_start": self._next_start,
                        "subcommand": self.subcommand,
                    },
                    sort_keys=True,
                )[:512]
            )


def _stderr_log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# -- status command ----------------------------------------------------------------


def read_status(state_dir: Path) -> dict[str, Any] | None:
    try:
        descriptor = os.open(state_dir / STATUS_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def evaluate_status(
    document: dict[str, Any] | None, now: float, max_age: float
) -> tuple[bool, dict[str, Any]]:
    """Return (healthy, report). Healthy means fresh and not stopped/failed."""
    if document is None:
        return False, {"state": "unknown", "fresh": False, "reason": "no status file"}
    updated = document.get("updated_at")
    age = now - updated if isinstance(updated, (int, float)) else None
    fresh = age is not None and -max_age <= age <= max_age
    report = dict(document)
    report["fresh"] = fresh
    report["age_seconds"] = round(age, 1) if age is not None else None
    healthy = fresh and document.get("state") not in BAD_STATES
    return healthy, report


# -- command line ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="tessera-worker-daemon",
        usage="%(prog)s {run,status} ... (run: options -- WORKER_SUBCOMMAND [worker args])",
    )
    commands = command.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="supervise a worker subcommand given after '--'")
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--backoff-min", type=float, default=2.0)
    run.add_argument("--backoff-max", type=float, default=120.0)
    run.add_argument("--auth-backoff-min", type=float, default=60.0)
    run.add_argument("--auth-backoff-max", type=float, default=900.0)
    run.add_argument("--stable-seconds", type=float, default=60.0)
    run.add_argument("--grace-seconds", type=float, default=30.0)
    run.add_argument("--restart-delay", type=float, default=1.0)
    status = commands.add_parser("status", help="print status; exit 0 only if fresh and running")
    status.add_argument("--state-dir", type=Path, required=True)
    status.add_argument("--max-age", type=float, default=120.0)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    worker_args: list[str] = []
    if "--" in arguments:
        index = arguments.index("--")
        worker_args = arguments[index + 1 :]
        arguments = arguments[:index]
    try:
        parsed = build_parser().parse_args(arguments)
    except SystemExit as error:
        return EX_USAGE if error.code not in (0, None) else 0
    try:
        if parsed.command == "status":
            return _status_command(parsed.state_dir, parsed.max_age)
        return _run_command(parsed, worker_args)
    except DaemonError as error:
        print(f"worker daemon: {error}", file=sys.stderr)
        return error.code


def _status_command(state_dir: Path, max_age: float) -> int:
    healthy, report = evaluate_status(read_status(state_dir), time.time(), max_age)
    print(json.dumps(report, sort_keys=True))
    if not healthy:
        return 1
    # Alive and supervising, but the worker is not doing work: control rejected its
    # credential. A distinct status lets a health check or script notice a degraded worker.
    if report.get("state") == "credential_backoff":
        return EXIT_CREDENTIAL_REJECTED
    return 0


def _run_command(parsed: argparse.Namespace, worker_args: list[str]) -> int:
    config = DaemonConfig(
        backoff_min=parsed.backoff_min,
        backoff_max=parsed.backoff_max,
        auth_backoff_min=parsed.auth_backoff_min,
        auth_backoff_max=parsed.auth_backoff_max,
        stable_seconds=parsed.stable_seconds,
        grace_seconds=parsed.grace_seconds,
        restart_delay=parsed.restart_delay,
    )
    validate_worker_args(worker_args)
    config.validate()
    state_dir = prepare_state_dir(parsed.state_dir)
    lock = acquire_lock(state_dir)
    try:
        supervisor = Supervisor(
            worker_args,
            config,
            write_status=lambda document: write_status_file(state_dir, document),
        )
        return supervisor.run()
    finally:
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
