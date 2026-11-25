"""Test scratch in the storage retention policy: the repository's ``.pytest-tmp`` directory.

Focused and parallel test runs leave one basetemp per run under ``<repo>/.pytest-tmp``. This
module lists those directories (read-only, opt-in) and, on apply, removes the ones that have
been quiet for the operator's age. It touches nothing else:

* the only root it accepts is a real directory literally named ``.pytest-tmp``;
* each DIRECT child of that root is one candidate (a run's basetemp); the root itself, symlinks
  and anything whose resolved path is not a strict descendant of the resolved root are refused;
* a candidate whose tree changed within the last hour is kept (a run may still be using it).
  No process lock exists for a basetemp in this repository, so the recent-change rule is the
  guard, and it is checked again right before removal;
* read-only directories (pytest keeps some) are made writable before removal, never following a
  symlink, so removal does not stop half-way.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

from .storage_retention_models import Candidate, RetentionPolicy

SCRATCH_DIR_NAME = ".pytest-tmp"
RECENT_SECONDS = 3600  # a tree changed within the last hour is treated as in use
MAX_ENTRIES = 2_000_000  # bounded walk: a larger tree is kept and reported, never half-measured


class ScratchError(RuntimeError):
    """The scratch directory was not removed; the message is fixed text, never a path."""


def default_root() -> Path:
    """``<repository>/.pytest-tmp``: the source tree this module was loaded from."""
    return Path(__file__).resolve().parents[2] / SCRATCH_DIR_NAME


def resolve_root(policy: RetentionPolicy) -> Path:
    return Path(policy.test_scratch_root) if policy.test_scratch_root else default_root()


def _skip(ref: str, reason: str) -> dict[str, str]:
    return {"kind": "test_scratch", "ref": ref, "reason": reason}


def _measure(path: Path) -> tuple[int, int, int] | None:
    """``(bytes, newest mtime in ns, entries)`` of a tree, never following a symlink; ``None``
    when the walk would exceed ``MAX_ENTRIES``."""
    total = entries = 0
    newest = path.lstat().st_mtime_ns
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        return None
                    info = entry.stat(follow_symlinks=False)
                    newest = max(newest, info.st_mtime_ns)
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    else:
                        total += info.st_size
        except OSError:
            continue  # an unreadable subtree is not measured; removal re-checks everything
    return total, newest, entries


def _checked_root(root: Path) -> Path | None:
    """The resolved scratch root, or ``None`` when it is absent or not acceptable."""
    try:
        info = root.lstat()
    except OSError:
        return None
    if (
        root.name != SCRATCH_DIR_NAME
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
    ):
        return None
    return root.resolve()


def _child(root: Path, name: str) -> Path | None:
    """The candidate directory ``root/name`` when it is a plain directory strictly inside the
    root; ``None`` for a symlink, a non-directory, an odd name or an escape."""
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        return None
    path = root / name
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    resolved = path.resolve()
    if resolved.parent != root or resolved == root:
        return None
    return path


def plan_test_scratch(
    policy: RetentionPolicy, *, now: datetime
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """Read-only plan input; nothing unless the policy opts in and ``.pytest-tmp`` exists."""
    if not policy.reclaim_test_scratch:
        return [], []
    configured = resolve_root(policy)
    if not configured.exists() and not configured.is_symlink():
        return [], []  # no scratch directory: nothing to plan
    root = _checked_root(configured)
    if root is None:
        return [], [_skip("", "the test scratch root is not a plain .pytest-tmp directory")]
    now = now.astimezone(UTC)
    max_age = policy.test_scratch_max_age_hours * 3600
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    try:
        names = sorted(entry.name for entry in os.scandir(root))
    except OSError:
        return [], [_skip("", "the test scratch root could not be listed")]
    for name in names:
        path = _child(root, name)
        if path is None:
            skipped.append(_skip(name, "not a plain directory inside .pytest-tmp"))
            continue
        measured = _measure(path)
        if measured is None:
            skipped.append(_skip(name, "tree too large to measure"))
            continue
        size, newest_ns, entries = measured
        newest = datetime.fromtimestamp(newest_ns / 1e9, UTC)
        age = (now - newest).total_seconds()
        if age < RECENT_SECONDS:
            decision, reasons = "keep", ["recent_activity"]
        elif age >= max_age:
            decision, reasons = "delete", ["max_age"]
        else:
            decision, reasons = "keep", ["within_max_age"]
        digest = hashlib.sha256(
            json.dumps([name, size, newest_ns, entries], separators=(",", ":")).encode()
        ).hexdigest()
        candidates.append(
            Candidate(
                kind="test_scratch",
                ref=name,
                path=name,
                size_bytes=size,
                sha256=digest,
                created_at=newest.isoformat(),
                decision=decision,
                reasons=reasons,
            )
        )
    return candidates, skipped


def _make_writable(path: Path) -> None:
    """``chmod u+rwx`` on every real directory of the tree (never through a symlink), so removal
    cannot fail half-way on a read-only directory."""
    for directory, names, _files in os.walk(path, followlinks=False):
        names[:] = [n for n in names if not os.path.islink(os.path.join(directory, n))]
        try:
            os.chmod(directory, os.stat(directory).st_mode | 0o700)
        except OSError:
            pass


def delete_test_scratch(root: Path, candidate: Candidate) -> dict[str, object]:
    """Remove one planned basetemp. Re-checks everything right before removing."""
    checked = _checked_root(root)
    if checked is None:
        raise ScratchError("the test scratch root is not a plain .pytest-tmp directory")
    path = _child(checked, candidate.path)
    if path is None:
        raise ScratchError("the scratch directory is not a plain directory inside .pytest-tmp")
    measured = _measure(path)
    if measured is None:
        raise ScratchError("the scratch directory is too large to verify")
    newest = datetime.fromtimestamp(measured[1] / 1e9, UTC)
    if (datetime.now(UTC) - newest).total_seconds() < RECENT_SECONDS:
        raise ScratchError("the scratch directory changed recently; kept")
    _make_writable(path)
    try:
        shutil.rmtree(path)
    except OSError:
        raise ScratchError("the scratch directory could not be removed") from None
    return {"size_bytes": candidate.size_bytes, "sha256": candidate.sha256}


__all__ = [
    "SCRATCH_DIR_NAME",
    "ScratchError",
    "default_root",
    "delete_test_scratch",
    "plan_test_scratch",
    "resolve_root",
]
