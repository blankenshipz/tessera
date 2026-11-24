"""Standing-loop runner that offers stuck-campaign findings to the operator. Disabled by default.

Each tick the ``StandingLoop`` reads the project's open stuck findings (``StuckWorkSource``) and
hands at most one new finding to ``StuckOfferHandler``, which writes a ``stuck_offered`` receipt
and, only if the policy says ``ack_on_offer``, acknowledges the finding. Nothing here calls a model
(the loop's model and tier are fixed to ``none``), acts on a campaign or gains authority: the
control adapter (``StuckControl``) can reach only the stuck listing and acknowledge routes, and the
credential needs only ``campaign:read`` (plus ``campaign:cancel`` to acknowledge).

Policy: a strict, owner-only (0600) JSON file, unknown keys refused::

    {"enabled": true, "project_id": "...", "identity_ref": "...",
     "max_calls_per_hour": 20, "max_consecutive_failures": 3,
     "base_backoff_seconds": 30, "max_backoff_seconds": 900,
     "source_limit": 50, "ack_on_offer": false}

Every key except ``enabled`` has a default (``project_id`` defaults to TESSERA_PROJECT_ID and must
match it); ``enabled`` must be present and anything but JSON ``true`` means disabled: the runner
prints ``disabled`` and exits 0 without touching the network, ledger or journal. The policy digest
recorded on every receipt is the sha256 of the canonical JSON of the full resolved policy.

Safety: refuses to start (exit 2) when ``--kill-switch`` is missing (treated as engaged) or a
direct API key variable is set; the kill switch is re-checked by the loop and by the handler before
every offer; the call ledger (sqlite, 0600, single-instance lock) makes each finding occurrence an
at-most-once offer and persists the failure count and halt across restarts; after a refused or
uncertain offer the runner waits an exponential backoff (``base_backoff_seconds`` doubling, capped
at ``max_backoff_seconds``) before the next tick, and an uncertain offer is never retried; the
journal is a 0600, locked, fsynced JSONL file of ids, codes, digests and reasons, one timestamped
line per event.

Recovery: ``--reset`` (un-halt) and ``--resolve ITEM_ID --outcome succeeded|not_started`` (decide
an uncertain offer) open the same ledger under the same refusals and exclusive locks, never start
a call or touch the network, never clear the per-finding rows, append an ``operator_reset`` or
``uncertain_resolved`` receipt to the journal, print one line and exit 0. They cannot be combined
with ticking.

Exit codes: 0 stopped (or disabled, or recovered), 2 configuration error or refusal, 3 the loop is
halted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tessera_agent.standing_loop import (
    ApiKeyPresent,
    KillSwitch,
    LedgerInUse,
    LoopPolicy,
    SqliteCallLedger,
    StandingLoop,
)
from tessera_agent.stuck_loop_action import NO_MODEL, StuckOfferHandler
from tessera_agent.stuck_loop_control import StuckControl
from tessera_agent.stuck_work_source import MAX_ITEMS, StuckWorkSource

try:
    from tessera_agent.model_destinations import FORBIDDEN_ENVIRONMENT
except ImportError:  # pragma: no cover - the module is part of this package
    FORBIDDEN_ENVIRONMENT = ("ANTHROPIC_API_KEY",)

EXIT_STOPPED, EXIT_CONFIG, EXIT_HALTED = 0, 2, 3
_SLICE_SECONDS = 1.0
_MAX_POLICY_BYTES = 16384
_FORBIDDEN = tuple(dict.fromkeys(("ANTHROPIC_API_KEY", *FORBIDDEN_ENVIRONMENT)))


class PolicyError(ValueError):
    """The policy file is missing, not private, malformed or out of range."""


class JournalInUse(Exception):
    """Another live runner already holds the exclusive lock on this journal."""


@dataclass(frozen=True)
class StuckLoopPolicy:
    enabled: bool
    project_id: str
    identity_ref: str = "stuck-loop"
    max_calls_per_hour: int = 20
    max_consecutive_failures: int = 3
    base_backoff_seconds: float = 30
    max_backoff_seconds: float = 900
    source_limit: int = 50
    ack_on_offer: bool = False

    @property
    def digest(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def loop_policy(self) -> LoopPolicy:
        """The loop's fixed policy. Model and tier are ``none``: this loop never calls a model."""
        return LoopPolicy(
            policy_digest=self.digest,
            identity_ref=self.identity_ref,
            model=NO_MODEL,
            tier=NO_MODEL,
            max_calls_per_hour=self.max_calls_per_hour,
            max_consecutive_failures=self.max_consecutive_failures,
            base_backoff_seconds=self.base_backoff_seconds,
            max_backoff_seconds=self.max_backoff_seconds,
        )


_INT_FIELDS = {
    "max_calls_per_hour": 1000,
    "max_consecutive_failures": 100,
    "source_limit": MAX_ITEMS,
}
_NUM_FIELDS = {"base_backoff_seconds": 86400, "max_backoff_seconds": 86400}
_STR_FIELDS = ("project_id", "identity_ref")
_KEYS = {"enabled", "ack_on_offer", *_INT_FIELDS, *_NUM_FIELDS, *_STR_FIELDS}


def _read_private(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise PolicyError(f"policy file unavailable ({type(error).__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PolicyError("policy must be an owner-only (0600) regular file")
        if info.st_size > _MAX_POLICY_BYTES:
            raise PolicyError("policy file is too large")
        return os.read(fd, _MAX_POLICY_BYTES + 1)
    finally:
        os.close(fd)


def _reject_constants(token: str) -> Any:
    raise PolicyError(f"policy has a non-JSON number: {token}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PolicyError(f"policy repeats key: {key}")
        out[key] = value
    return out


def load_policy(path: Path, default_project: str | None = None) -> StuckLoopPolicy:
    """Strictly parse the policy file (see the module docstring); raises PolicyError."""
    raw = _read_private(path)
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constants,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PolicyError("policy is not valid JSON") from None
    if not isinstance(data, dict):
        raise PolicyError("policy must be a JSON object")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise PolicyError(f"policy has unknown keys: {', '.join(unknown)}")
    if "enabled" not in data:
        raise PolicyError("policy must say enabled")
    values: dict[str, Any] = {"enabled": data["enabled"] is True}
    if "ack_on_offer" in data:
        if not isinstance(data["ack_on_offer"], bool):
            raise PolicyError("ack_on_offer must be true or false")
        values["ack_on_offer"] = data["ack_on_offer"]
    for name in _STR_FIELDS:
        if name in data:
            value = data[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise PolicyError(f"{name} must be a non-empty string")
            values[name] = value
    for name, top in _INT_FIELDS.items():
        if name in data:
            value = data[name]
            if type(value) is not int or not 1 <= value <= top:
                raise PolicyError(f"{name} must be an integer 1-{top}")
            values[name] = value
    for name, top in _NUM_FIELDS.items():
        if name in data:
            value = data[name]
            if type(value) not in (int, float) or not 0 < value <= top:
                raise PolicyError(f"{name} must be a number in (0, {top}]")
            values[name] = value
    project = values.get("project_id", default_project)
    if not project:
        raise PolicyError("project_id is required")
    if default_project is not None and project != default_project:
        raise PolicyError("project_id does not match the configured project")
    values["project_id"] = project
    policy = StuckLoopPolicy(**values)
    try:
        StuckWorkSource(None, policy.project_id, policy.source_limit)
        policy.loop_policy()
    except ValueError as error:
        raise PolicyError(str(error)) from None
    return policy


class RunnerJournal:
    """Private (0600) append-only JSONL journal; one fsynced, timestamped line per event.

    Lines hold ids, codes, digests and reasons only; never tokens or response bodies. An existing
    file is opened with O_NOFOLLOW and checked by fstat on that descriptor: a symlink, a non-regular
    file, a foreign owner or group/other permission bits are refused (ValueError), never chmodded.
    An exclusive non-blocking ``flock`` on ``<path>.lock`` is held until ``close()``; a second
    runner on the same path raises ``JournalInUse``. Local filesystem only.
    """

    def __init__(self, path: str | os.PathLike, clock: Callable[[], float]):
        self.path = os.fspath(path)
        self._clock = clock
        self._fd: int | None = None
        self._lock_fd: int | None = None
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(self.path, flags)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError("journal must be an owner-only (0600) regular file")
            except BaseException:
                os.close(fd)
                raise
        try:
            lock = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                os.close(lock)
                raise JournalInUse("another runner already holds this journal") from error
        except BaseException:
            os.close(fd)
            raise
        self._fd, self._lock_fd = fd, lock

    def write_event(self, event: dict[str, Any]) -> None:
        if self._fd is None:
            raise ValueError("journal is closed")
        line = json.dumps({"ts": self._clock(), **event}, sort_keys=True, default=str)
        os.write(self._fd, (line + "\n").encode())
        os.fsync(self._fd)

    def close(self) -> None:
        for name in ("_fd", "_lock_fd"):
            fd = getattr(self, name, None)
            setattr(self, name, None)
            if fd is not None:
                os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class CallTally:
    """Passes loop events to the journal and counts this tick's call outcomes for the backoff."""

    def __init__(self, write: Callable[[dict], None]) -> None:
        self._write = write
        self.ok = self.failed = 0

    def record(self, event: dict) -> None:
        name = event.get("event")
        if name == "call_finished" and event.get("outcome") == "ok":
            self.ok += 1
        elif (name == "call_finished" and event.get("outcome") == "failed") or (
            name == "uncertain_outcome" and "error" in event
        ):
            self.failed += 1
        self._write(event)

    def take(self) -> tuple[int, int]:
        out, self.ok, self.failed = (self.ok, self.failed), 0, 0
        return out


def run_loop(
    loop: StandingLoop,
    journal: RunnerJournal,
    tally: CallTally,
    *,
    interval_seconds: float,
    max_ticks: int | None,
    base_backoff_seconds: float,
    max_backoff_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    stop_requested: Callable[[], bool],
) -> int:
    """Tick the loop until stopped or ``max_ticks`` is reached (exit 0) or it halts (exit 3).

    After a tick whose offer failed (refused or uncertain) the next tick waits an exponential
    backoff (``base * 2**(n-1)``, capped) instead of the interval; a successful offer resets it.
    The loop itself persists the failure count and halts at ``max_consecutive_failures``.
    """
    ticks = failures = 0
    while True:
        if stop_requested():
            journal.write_event({"event": "runner_stopped", "reason": "stop_requested"})
            return EXIT_STOPPED
        if max_ticks is not None and ticks >= max_ticks:
            journal.write_event({"event": "runner_stopped", "reason": "max_ticks"})
            return EXIT_STOPPED
        ticks += 1
        tally.take()
        out = loop.tick()
        ok, failed = tally.take()
        journal.write_event(
            {
                "event": "tick",
                "tick": ticks,
                "state": out.get("state"),
                "started": out.get("started"),
                "skipped": out.get("skipped"),
                "uncertain": list(out.get("uncertain") or []),
            }
        )
        if out.get("state") == "halted":
            journal.write_event({"event": "halted", "tick": ticks})
            return EXIT_HALTED
        delay = interval_seconds
        if ok:
            failures = 0
        elif failed:
            failures += 1
            delay = max(
                interval_seconds,
                min(base_backoff_seconds * 2 ** (failures - 1), max_backoff_seconds),
            )
            journal.write_event(
                {
                    "event": "backoff",
                    "reason": "offer_failed",
                    "delay_seconds": delay,
                    "until": clock() + delay,
                    "consecutive_failures": failures,
                }
            )
        if max_ticks is None or ticks < max_ticks:
            end = clock() + delay
            while not stop_requested() and (remaining := end - clock()) > 0:
                sleep(min(_SLICE_SECONDS, remaining))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tessera-stuck-loop",
        description=(
            "Offers stuck-campaign findings to the operator under a fixed policy. Disabled unless "
            "the policy file says enabled; never calls a model and never acts on a campaign."
        ),
        epilog=(
            "References: TESSERA_CONTROL_URL, TESSERA_AGENT_TOKEN_FILE, TESSERA_PROJECT_ID, "
            "TESSERA_AGENT_DESTINATION (optional TESSERA_CONTROL_CA_FILE). Refuses to start when "
            "a direct API key variable is set. Keep every path on a local disk."
        ),
    )
    p.add_argument("--policy", required=True, type=Path)
    p.add_argument("--journal", type=Path)
    p.add_argument("--ledger", type=Path)
    p.add_argument("--kill-switch", type=Path)
    p.add_argument("--interval-seconds", type=float, default=60)
    p.add_argument("--max-ticks", type=int, default=None)
    p.add_argument("--once", action="store_true", help="run one tick (same as --max-ticks 1)")
    p.add_argument("--reset", action="store_true", help="un-halt the loop, then exit (no tick)")
    p.add_argument("--resolve", metavar="ITEM_ID", help="resolve an uncertain offer, then exit")
    p.add_argument("--outcome", choices=("succeeded", "not_started"), help="with --resolve")
    return p


def _no_call(*_: Any, **__: Any) -> Any:
    raise RuntimeError("operator recovery never starts a call")


def _recover(
    args: argparse.Namespace,
    ledger: SqliteCallLedger,
    journal: RunnerJournal,
    kill: Any,
    policy: StuckLoopPolicy,
    env: Mapping[str, str],
    clock: Callable[[], float],
) -> int:
    """Operator reset/resolve through the loop's own methods; no source, no call, no network."""
    try:
        loop = StandingLoop(
            policy.loop_policy(), kill, lambda: [], _no_call, journal.write_event, clock,
            ledger=ledger, environ=env,
        )  # fmt: skip
        if args.reset:
            was = loop.state
            loop.reset()
            journal.write_event(
                {"event": "operator_reset", "was": was, "policy_digest": policy.digest}
            )
            print(f"reset: loop was {was}, now {loop.state}")
        else:
            loop.resolve_uncertain(args.resolve, args.outcome)
            print(f"resolved: {args.resolve} as {args.outcome}")
    except ApiKeyPresent:
        return _refuse("a direct API key variable is set; refusing to start")
    except ValueError as error:
        return _refuse(f"recovery refused: {error}")
    return EXIT_STOPPED


def _refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return EXIT_CONFIG


def main(
    argv: list[str] | None = None,
    *,
    control: Any = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Console entry point. ``control`` (a ``StuckControl``-like object) is for tests only."""
    parser = _parser()
    args = parser.parse_args(argv)
    recovery = args.reset or args.resolve is not None
    if args.reset and args.resolve is not None:
        parser.error("--reset and --resolve are mutually exclusive")
    if (args.outcome is None) == (args.resolve is not None):
        if args.resolve is not None or args.outcome is not None:
            parser.error("--resolve and --outcome go together")
    if recovery and (args.once or args.max_ticks is not None):
        parser.error("--reset/--resolve cannot be combined with --once or --max-ticks")
    env = os.environ if environ is None else environ
    present = [name for name in _FORBIDDEN if name in env]
    if present:
        return _refuse(f"{present[0]} is set; refusing to start")
    default_project = env.get("TESSERA_PROJECT_ID") or None
    try:
        policy = load_policy(args.policy, default_project)
    except PolicyError as error:
        return _refuse(f"policy invalid: {error}")
    if not policy.enabled:
        print("disabled")
        return EXIT_STOPPED
    if args.kill_switch is None:
        return _refuse("no --kill-switch given; treating the kill switch as engaged")
    if args.journal is None or args.ledger is None:
        return _refuse("--journal and --ledger are required when enabled")
    if recovery:
        kill = KillSwitch(args.kill_switch)
        try:
            journal = RunnerJournal(args.journal, clock)
        except (ValueError, OSError, JournalInUse) as error:
            return _refuse(f"journal unavailable: {type(error).__name__}")
        with journal:
            try:
                ledger = SqliteCallLedger(args.ledger)
            except (ValueError, OSError, LedgerInUse) as error:
                return _refuse(f"ledger unavailable: {type(error).__name__}")
            with ledger:
                return _recover(args, ledger, journal, kill, policy, env, clock)
    if not 1 <= args.interval_seconds <= 3600:
        return _refuse("--interval-seconds must be between 1 and 3600")
    max_ticks = 1 if args.once else args.max_ticks
    if max_ticks is not None and max_ticks < 1:
        return _refuse("--max-ticks must be at least 1")
    if control is None:
        from tessera_mcp.client import ControlClient
        from tessera_mcp.config import Config

        try:
            config = Config.from_env()
            config.token()  # validates the reference now; the value is never kept or shown
        except ValueError as error:
            return _refuse(f"configuration invalid: {error}")
        control = StuckControl(ControlClient(config), policy.project_id)
    kill = KillSwitch(args.kill_switch)
    try:
        journal = RunnerJournal(args.journal, clock)
    except (ValueError, OSError, JournalInUse) as error:
        return _refuse(f"journal unavailable: {type(error).__name__}")
    with journal:
        try:
            ledger = SqliteCallLedger(args.ledger)
        except (ValueError, OSError, LedgerInUse) as error:
            return _refuse(f"ledger unavailable: {type(error).__name__}")
        with ledger:
            tally = CallTally(journal.write_event)
            handler = StuckOfferHandler(
                control, policy.project_id, journal.write_event, kill, policy.ack_on_offer
            )
            try:
                loop = StandingLoop(
                    policy.loop_policy(),
                    kill,
                    StuckWorkSource(control, policy.project_id, policy.source_limit),
                    handler,
                    tally.record,
                    clock,
                    ledger=ledger,
                    environ=env,
                )
            except ApiKeyPresent:
                return _refuse("a direct API key variable is set; refusing to start")
            stop = {"flag": False}

            def request_stop(*_: Any) -> None:
                stop["flag"] = True

            journal.write_event(
                {
                    "event": "runner_started",
                    "policy_digest": policy.digest,
                    "project_id": policy.project_id,
                    "ack_on_offer": policy.ack_on_offer,
                    "interval_seconds": args.interval_seconds,
                    "max_ticks": max_ticks,
                }
            )
            signals = (signal.SIGTERM, signal.SIGINT)
            previous = {s: signal.signal(s, request_stop) for s in signals}
            try:
                return run_loop(
                    loop,
                    journal,
                    tally,
                    interval_seconds=args.interval_seconds,
                    max_ticks=max_ticks,
                    base_backoff_seconds=policy.base_backoff_seconds,
                    max_backoff_seconds=policy.max_backoff_seconds,
                    clock=clock,
                    sleep=sleep,
                    stop_requested=lambda: stop["flag"],
                )
            finally:
                for sig, handler_ in previous.items():
                    signal.signal(sig, handler_)


if __name__ == "__main__":
    raise SystemExit(main())
