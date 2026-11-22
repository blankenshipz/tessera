"""Scheduled runner for the campaign submit action (ADR 0093 item 10).

The runner calls ``CampaignSubmitAction.tick`` every interval so no session has to. It adds only
scheduling, a kill-switch wait, failure backoff, a halt after repeated failures and a private
receipt journal. It never approves, creates or widens anything and never touches running
campaigns. Nothing starts at import time; ``main`` is the console-script entry point.

A tick counts as failed when the action raises, its list call fails or is malformed, or it had
``submit_failed`` events and no successful submission. Refusals are not failures.

Exit codes: 0 stopped cleanly, 2 configuration error, 3 halted after repeated failed ticks.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tessera_agent.campaign_submit_action import CampaignSubmitAction, SubmitPolicy
from tessera_agent.standing_loop import _API_KEY_VAR, ApiKeyPresent, KillSwitch

EXIT_STOPPED, EXIT_CONFIG, EXIT_HALTED = 0, 2, 3
_SLICE_SECONDS = 1.0


class JournalInUse(Exception):
    """Another live runner already holds the exclusive lock on this journal."""


@dataclass(frozen=True)
class RunnerSettings:
    journal_path: Path
    kill_switch: Path
    interval_seconds: float = 30
    max_submissions_per_tick: int = 1
    max_attempts_per_tick: int = 5
    max_consecutive_failures: int = 5
    max_backoff_seconds: float = 300
    max_ticks: int | None = None

    def __post_init__(self):
        if not 5 <= self.interval_seconds <= 3600:
            raise ValueError("interval_seconds must be between 5 and 3600")
        if not 1 <= self.max_submissions_per_tick <= 10:
            raise ValueError("max_submissions_per_tick must be between 1 and 10")
        if self.max_attempts_per_tick < 1:
            raise ValueError("max_attempts_per_tick must be at least 1")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        if self.max_backoff_seconds < self.interval_seconds:
            raise ValueError("max_backoff_seconds must be at least interval_seconds")
        if self.max_ticks is not None and self.max_ticks < 1:
            raise ValueError("max_ticks must be at least 1 or None")


class RunnerJournal:
    """Private (0600) append-only JSONL receipt log: one fsynced line per runner action.

    Lines hold a timestamp, an event name, ids and stable reasons only; never tokens, prompts or
    response bodies. The control plane keeps its own decision rows; this is the runner's log.
    An existing file is opened with O_NOFOLLOW and checked with fstat on that descriptor; a symlink,
    non-regular file, foreign owner or group/other permission bits is refused (ValueError), never
    chmodded.
    The journal holds an exclusive non-blocking ``flock`` on ``<path>.lock`` until ``close()``;
    a second runner on the same path raises ``JournalInUse``. Local filesystem only: ``flock`` is
    advisory, unreliable on network filesystems, and keyed on the path string (hard links or
    alternate paths to the same file are not covered).
    """

    def __init__(self, path: str | os.PathLike, clock: Callable[[], float]):
        self.path = os.fspath(path)
        self._clock = clock
        self._lock_fd: int | None = None
        self._fd: int | None = None
        try:
            created = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND, 0o600
            )
        except FileExistsError:
            created = None
        # Validate (fstat on the opened, O_NOFOLLOW descriptor) before taking the lock.
        fd = created if created is not None else self._open_private()
        try:
            self._acquire_lock()
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def _open_private(self) -> int:
        """Open an existing journal and validate the opened descriptor (no lstat/open gap)."""
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("journal must be an owner-only (0600) regular file")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _acquire_lock(self) -> None:
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise JournalInUse("another runner already holds this journal") from exc
        self._lock_fd = fd

    def write_event(self, event: dict[str, Any]) -> None:
        if self._fd is None:
            raise ValueError("journal is closed")
        line = json.dumps({"ts": self._clock(), **event}, sort_keys=True, default=str)
        os.write(self._fd, (line + "\n").encode())
        os.fsync(self._fd)

    def close(self) -> None:
        fd, self._fd = getattr(self, "_fd", None), None
        if fd is not None:
            os.close(fd)
        lock, self._lock_fd = getattr(self, "_lock_fd", None), None
        if lock is not None:
            os.close(lock)  # closing the descriptor releases the flock

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class SubmitRunner:
    """Calls the submit action each interval. ``build_action(record)`` gets the journal writer.

    A tick fails when the action raises, reports its list call failed or malformed, or records one
    or more ``submit_failed`` events (non-refusal client errors) with no successful submission in
    that tick. One success alongside a ``submit_failed`` is progress, not a failure. A
    ``submit_refused`` (stable control refusal) is never a failure. While the kill switch is
    engaged the action and the control plane are not called at all.
    """

    def __init__(
        self,
        build_action: Callable[[Callable[[dict], None]], CampaignSubmitAction],
        kill_switch: KillSwitch,
        journal: RunnerJournal,
        settings: RunnerSettings,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        stop_requested: Callable[[], bool] = lambda: False,
    ):
        self._kill = kill_switch
        self._journal = journal
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._stop = stop_requested
        self._action = build_action(journal.write_event)

    def _wait(self, seconds: float) -> None:
        end = self._clock() + seconds
        while not self._stop():
            remaining = end - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(_SLICE_SECONDS, remaining))

    def run(self) -> int:
        s, journal = self._settings, self._journal
        journal.write_event(
            {
                "event": "runner_started",
                "interval_seconds": s.interval_seconds,
                "max_ticks": s.max_ticks,
            }
        )
        engaged = False
        failures = ticks = 0
        while True:
            if self._stop():
                return self._stopped("stop_requested")
            if s.max_ticks is not None and ticks >= s.max_ticks:
                return self._stopped("max_ticks")
            ticks += 1
            if self._kill.engaged():
                if not engaged:
                    engaged = True
                    journal.write_event({"event": "kill_switch_engaged"})
                self._wait_unless_last(ticks, s.interval_seconds)
                continue
            if engaged:
                engaged = False
                journal.write_event({"event": "kill_switch_cleared"})
            failed_reason = self._tick(ticks)
            delay = s.interval_seconds
            if failed_reason is None:
                failures = 0
            else:
                failures += 1
                if failures >= s.max_consecutive_failures:
                    journal.write_event(
                        {
                            "event": "halted",
                            "consecutive_failures": failures,
                            "reason": failed_reason,
                        }
                    )
                    return EXIT_HALTED
                delay = min(s.interval_seconds * 2**failures, s.max_backoff_seconds)
                journal.write_event(
                    {
                        "event": "backoff",
                        "until": self._clock() + delay,
                        "delay_seconds": delay,
                        "consecutive_failures": failures,
                        "reason": failed_reason,
                    }
                )
            self._wait_unless_last(ticks, delay)

    def _wait_unless_last(self, ticks: int, delay: float) -> None:
        if self._settings.max_ticks is None or ticks < self._settings.max_ticks:
            self._wait(delay)

    def _tick(self, number: int) -> str | None:
        try:
            events = self._action.tick()
        except Exception as error:  # noqa: BLE001 - recorded by type only
            self._journal.write_event(
                {"event": "tick", "tick": number, "failed": True, "reason": type(error).__name__}
            )
            return type(error).__name__
        names = [e.get("event") for e in events]
        reason = next((n for n in names if n in {"list_failed", "list_malformed"}), None)
        if reason is None and "submit_failed" in names and "submitted" not in names:
            reason = "submit_failed"
        self._journal.write_event(
            {
                "event": "tick",
                "tick": number,
                "failed": reason is not None,
                "started": names.count("submitted"),
                "refused": names.count("submit_refused"),
                "skipped": names.count("submit_skipped"),
            }
        )
        return reason

    def _stopped(self, reason: str) -> int:
        self._journal.write_event({"event": "runner_stopped", "reason": reason})
        return EXIT_STOPPED


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tessera-submit-runner",
        description="Runs the owner-approved campaign submit action on a schedule.",
        epilog=(
            "References: TESSERA_CONTROL_URL, TESSERA_AGENT_TOKEN_FILE, TESSERA_PROJECT_ID, "
            "TESSERA_AGENT_DESTINATION (optional TESSERA_CONTROL_CA_FILE). Refuses to start "
            f"when {_API_KEY_VAR} is set. Journal and kill-switch paths must be on a local disk."
        ),
    )
    p.add_argument("--journal", required=True, type=Path)
    p.add_argument("--kill-switch", required=True, type=Path)
    p.add_argument("--interval-seconds", type=float, default=30)
    p.add_argument("--max-submissions-per-tick", type=int, default=1)
    p.add_argument("--max-attempts-per-tick", type=int, default=5)
    p.add_argument("--max-ticks", type=int, default=None, help="testing")
    p.add_argument("--once", action="store_true", help="run one tick (max-ticks 1)")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if _API_KEY_VAR in os.environ:
        print(f"{_API_KEY_VAR} is set; refusing to start", file=sys.stderr)
        return EXIT_CONFIG
    import time

    from tessera_mcp.client import ControlClient, SubmissionActionClient
    from tessera_mcp.config import Config

    try:
        config = Config.from_env()
        config.token()  # validate the credential reference now; the value is never kept or shown
        settings = RunnerSettings(
            journal_path=args.journal,
            kill_switch=args.kill_switch,
            interval_seconds=args.interval_seconds,
            max_submissions_per_tick=args.max_submissions_per_tick,
            max_attempts_per_tick=args.max_attempts_per_tick,
            max_ticks=1 if args.once else args.max_ticks,
        )
    except ValueError as error:
        print(f"configuration invalid: {error}", file=sys.stderr)
        return EXIT_CONFIG
    client = SubmissionActionClient(ControlClient(config))
    kill = KillSwitch(settings.kill_switch)
    policy = SubmitPolicy(
        config.project_id, settings.max_submissions_per_tick, settings.max_attempts_per_tick
    )
    stop = {"flag": False}

    def request_stop(*_):
        stop["flag"] = True

    try:
        journal = RunnerJournal(settings.journal_path, time.time)
    except (ValueError, OSError, JournalInUse) as error:
        print(f"journal unavailable: {type(error).__name__}", file=sys.stderr)
        return EXIT_CONFIG
    with journal:
        try:
            runner = SubmitRunner(
                lambda record: CampaignSubmitAction(client, kill, record, policy),
                kill,
                journal,
                settings,
                time.time,
                time.sleep,
                lambda: stop["flag"],
            )
        except ApiKeyPresent:
            print(f"{_API_KEY_VAR} is set; refusing to start", file=sys.stderr)
            return EXIT_CONFIG
        previous = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            return runner.run()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
