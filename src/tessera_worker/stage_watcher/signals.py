"""Provider-neutral signal snapshots for watching a stage on a remote provider.

A provider-side component (for example a driver that polls the provider) writes a small,
non-secret snapshot file with ``write_signal_snapshot``; the watcher reads it through
``SignalProbe`` (usually ``SignalProbe.from_file``). The snapshot is a closed JSON object of
schema ``tessera.stage-watch.signals.v1``, at most 4096 bytes:

- ``schema`` (required): ``"tessera.stage-watch.signals.v1"``
- ``observed_at`` (required): ISO 8601 time with a UTC offset
- ``state`` (required): ``running``, ``exited``, ``absent`` or ``unknown``
- optional integers: ``exit_code`` (-255..255), and ``output_bytes``, ``cpu_ms``,
  ``mem_bytes``, ``restarts``, ``progress_marker`` (all >= 0)
- optional bool: ``oom_killed``

No other key, and no string other than ``schema``, ``observed_at`` and ``state``. The snapshot
carries no credential, URL, log line or free text.

``progress_marker`` is a liveness counter: for a remote provider the driver advances it once
per successful poll, so a ``stuck`` report built on it means "the provider-side driver stopped
observing", not "the workload stalled". It is never a report counter.

Reading fails closed: anything that is not exactly a fresh, valid, owner-only (0600) regular
file owned by the current user reads as ``None``, which the probe reports as "unknown". Nothing
here opens a network connection or holds a credential.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from .probe import PROBE_STATES, ProbeSample

SIGNALS_SCHEMA = "tessera.stage-watch.signals.v1"
MAX_SNAPSHOT_BYTES = 4096
FUTURE_SKEW_SECONDS = 5

_REQUIRED_KEYS = frozenset({"schema", "observed_at", "state"})
_COUNT_KEYS = frozenset({"output_bytes", "cpu_ms", "mem_bytes", "restarts", "progress_marker"})
SIGNAL_KEYS: frozenset[str] = _REQUIRED_KEYS | _COUNT_KEYS | {"exit_code", "oom_killed"}
_MAX_COUNT = 2**63 - 1
_MAX_TIME_TEXT = 64
_FUTURE_SKEW = timedelta(seconds=FUTURE_SKEW_SECONDS)

Clock = Callable[[], datetime]
SignalsReader = Callable[[], "Mapping[str, object] | None"]


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_TIME_TEXT:
        raise ValueError("observed_at must be an ISO 8601 string.")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("observed_at must carry a UTC offset.")
    return parsed


def validate_signals(value: object) -> dict[str, object]:
    """Check the closed snapshot schema (no clock checks); return a plain copy or raise
    ValueError."""
    if not isinstance(value, Mapping):
        raise ValueError("signals must be a JSON object.")
    signals = dict(value)
    keys = set(signals)
    if not all(type(key) is str for key in keys):
        raise ValueError("signal keys must be strings.")
    if not _REQUIRED_KEYS <= keys:
        raise ValueError("signals need schema, observed_at and state.")
    if keys - SIGNAL_KEYS:
        raise ValueError("signals carry an unknown key.")
    if type(signals["schema"]) is not str or signals["schema"] != SIGNALS_SCHEMA:
        raise ValueError("unexpected signals schema.")
    if type(signals["state"]) is not str or signals["state"] not in PROBE_STATES:
        raise ValueError("state must be running, exited, absent or unknown.")
    _parse_time(signals["observed_at"])
    for key in _COUNT_KEYS & keys:
        count = signals[key]
        if type(count) is not int or not 0 <= count <= _MAX_COUNT:
            raise ValueError(f"{key} must be a non-negative integer.")
    if "exit_code" in signals:
        code = signals["exit_code"]
        if type(code) is not int or not -255 <= code <= 255:
            raise ValueError("exit_code must be an integer in -255..255.")
    if "oom_killed" in signals and type(signals["oom_killed"]) is not bool:
        raise ValueError("oom_killed must be a bool.")
    return signals


def _now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime.")
    return value


def _check_not_future(signals: Mapping[str, object], now: datetime) -> datetime:
    observed_at = _parse_time(signals["observed_at"])
    if observed_at - now > _FUTURE_SKEW:
        raise ValueError("observed_at is in the future.")
    return observed_at


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite number {name}")


def _decode(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicates,
        parse_constant=_reject_constant,
    )


def _absolute(path: str | os.PathLike[str]) -> Path:
    result = Path(path)
    if not result.is_absolute() or ".." in result.parts or result.name in ("", ".", ".."):
        raise ValueError("an absolute snapshot file path is required.")
    return result


def _positive_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < 2**31:
        raise ValueError("max_age_seconds must be a positive number.")
    return float(value)


def _read_private(path: Path) -> bytes:
    """Read an owner-only regular file without following a final symlink; raise on anything
    else."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_SNAPSHOT_BYTES
        ):
            raise ValueError("snapshot file is not a private regular file within bounds.")
        data = os.read(fd, MAX_SNAPSHOT_BYTES + 1)
        if not 0 < len(data) <= MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot file out of bounds.")
        return data
    finally:
        os.close(fd)


class SnapshotFileSignals:
    """Read one signal snapshot file. ``read()`` returns the validated mapping or ``None``;
    it never raises. A snapshot older than ``max_age_seconds`` (by ``clock``) is ``None``."""

    def __init__(self, path: str | os.PathLike[str], *, max_age_seconds: float, clock: Clock):
        self.path = _absolute(path)
        self.max_age = timedelta(seconds=_positive_seconds(max_age_seconds))
        if not callable(clock):
            raise TypeError("clock must be callable.")
        self.clock = clock

    def read(self) -> dict[str, object] | None:
        try:
            signals = validate_signals(_decode(_read_private(self.path)))
            now = _now(self.clock)
            observed_at = _check_not_future(signals, now)
            if now - observed_at > self.max_age:
                return None
            return signals
        except Exception:
            return None


def write_signal_snapshot(
    path: str | os.PathLike[str], signals: Mapping[str, object], *, clock: Clock
) -> dict[str, object]:
    """Atomically write a snapshot for ``SnapshotFileSignals`` (provider-side; file I/O only).

    ``schema`` defaults to the v1 schema and ``observed_at`` to ``clock()``; everything else
    must already satisfy the closed schema, and ``observed_at`` may not be more than 5 s ahead
    of ``clock``. The parent directory must exist. The file is written to a 0600 temporary file
    in the same directory (O_EXCL, O_NOFOLLOW), fsynced and renamed over ``path``. Raises
    ValueError for an invalid payload or a missing parent; returns what was written.
    """
    target = _absolute(path)
    if not isinstance(signals, Mapping):
        raise ValueError("signals must be a mapping.")
    now = _now(clock)
    payload = dict(signals)
    payload.setdefault("schema", SIGNALS_SCHEMA)
    payload.setdefault("observed_at", now.isoformat())
    payload = validate_signals(payload)
    _check_not_future(payload, now)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot exceeds 4096 bytes.")
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValueError("snapshot parent must be an existing directory.") from exc
    try:
        temp_name = f".{target.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp_name, target.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return payload


class SignalProbe:
    """A ``ComputeProbe`` over a zero-argument ``read`` returning a snapshot mapping or None.

    It holds no credential and does no network I/O. ``None``, a malformed mapping or a raising
    ``read`` gives state "unknown". The sample is dated by the watcher's ``now``; freshness is
    the reader's job (``SnapshotFileSignals`` returns ``None`` for a stale snapshot).
    """

    def __init__(self, read: SignalsReader):
        if not callable(read):
            raise TypeError("read must be callable.")
        self._read = read

    @classmethod
    def from_file(
        cls, path: str | os.PathLike[str], max_age_seconds: float, clock: Clock
    ) -> SignalProbe:
        return cls(SnapshotFileSignals(path, max_age_seconds=max_age_seconds, clock=clock).read)

    def sample(self, now: datetime) -> ProbeSample:
        try:
            raw = self._read()
            if raw is None:
                return ProbeSample("unknown", now)
            signals = validate_signals(raw)
            state = signals["state"]
            if state == "unknown":
                # A fresh snapshot that says "unknown" (for example while a remote group is
                # still allocating) still proves the provider-side driver is alive: keep its
                # liveness marker so the watcher does not read it as a lost probe.
                return ProbeSample(
                    "unknown",
                    now,
                    progress_marker=signals.get("progress_marker"),  # type: ignore[arg-type]
                )
            if state not in ("running", "exited"):
                return ProbeSample(state, now)  # type: ignore[arg-type]
            return ProbeSample(
                state,  # type: ignore[arg-type]
                now,
                exit_code=signals.get("exit_code"),  # type: ignore[arg-type]
                oom_killed=signals.get("oom_killed", False),  # type: ignore[arg-type]
                output_bytes=signals.get("output_bytes"),  # type: ignore[arg-type]
                cpu_ms=signals.get("cpu_ms"),  # type: ignore[arg-type]
                mem_bytes=signals.get("mem_bytes"),  # type: ignore[arg-type]
                restarts=signals.get("restarts", 0),  # type: ignore[arg-type]
                progress_marker=signals.get("progress_marker"),  # type: ignore[arg-type]
            )
        except Exception:
            return ProbeSample("unknown", now)
