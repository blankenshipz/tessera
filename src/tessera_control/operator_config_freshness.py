"""Operator provider config freshness: a launch never uses a config the files no longer match.

``tessera serve`` reads the operator's provider files once, at start. A change applies at
the next start (ADR 0081). Until then the process holds the old values, so a launch could
send work, and the data it carries, to a destination the file no longer names (ADR 0047).
At start the served files are fingerprinted; a launch is refused, fail closed, when any of
them changed, was removed or can no longer be read. The operator also keeps a second copy of
the Salad profiles for the executor (the file its config names). The two can diverge, so
when ``--salad-executor-config`` names it, a launch also compares the profile it is about to
use with the executor file's current entry of the same name and refuses on any difference.
Nothing is re-read or re-registered mid-process, so a run already started is untouched.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

STALE_CODE = "operator_config_stale"
MISMATCH_CODE = "operator_profile_mismatch"
_UNREADABLE = "unreadable"
_RECORDED: dict[str, str] = {}
_EXECUTOR: dict[str, str] = {}


def _digest(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return _UNREADABLE


def record(
    paths: Iterable[Path | str | None], executor_config: Path | str | None = None
) -> None:
    """Fingerprint the operator files the process was started with (replaces any earlier set).

    ``executor_config`` is the executor's config file; the profiles file it names is the
    second copy of the Salad profiles that a launch is compared with."""
    clear()
    for path in paths:
        if path is not None:
            _RECORDED[str(path)] = _digest(str(path))
    if executor_config is not None:
        _RECORDED[str(executor_config)] = _digest(str(executor_config))
        _EXECUTOR["config"] = str(executor_config)


def clear() -> None:
    _RECORDED.clear()
    _EXECUTOR.clear()


def stale_files() -> list[str]:
    """Recorded files whose bytes differ now (file names only, never contents)."""
    return sorted(
        Path(path).name for path, digest in _RECORDED.items() if _digest(path) != digest
    )


def refuse_if_stale() -> None:
    """Raise ValueError(STALE_CODE) when the running config no longer matches its files."""
    if stale_files():
        raise ValueError(STALE_CODE)


def executor_profile_problem(name: str, profile: object) -> str | None:
    """MISMATCH_CODE unless the executor's profiles file holds the identical profile.

    Only active when an executor config was recorded. An unreadable config or profiles file,
    a missing entry or any differing field (country, destinations, custody host, limits)
    is a mismatch: the executor would place a different selection than control derived."""
    config = _EXECUTOR.get("config")
    if config is None:
        return None
    try:
        from .deploy_state import _config_names
        from .salad_campaign_profiles import load_profiles

        profiles_file, _names = _config_names(Path(config))
        theirs = load_profiles(profiles_file).get(name)
        same = theirs is not None and theirs.model_dump(mode="json") == profile.model_dump(  # type: ignore[attr-defined]
            mode="json"
        )
    except Exception:  # noqa: BLE001 - anything unreadable is a refusal
        return MISMATCH_CODE
    return None if same else MISMATCH_CODE


def launch_problem(name: str, profile: object | None) -> str | None:
    """The refusal code for a launch of profile ``name``, or None when config is fresh."""
    if stale_files():
        return STALE_CODE
    if profile is not None:
        return executor_profile_problem(name, profile)
    return None
