"""Test scratch (pytest basetemps) a run creates, and removing exactly that when it ends green.

Standard library only: ``dev/pytest_parallel.py`` and the focused-test runner load this file by
path, so it works without the package's dependencies.

A run asks :class:`Scratch` for each basetemp it needs; the name carries a per-run id, so a
second run (even in the same checkout, at the same time) never shares or reuses a directory.
When the run ends, :meth:`Scratch.finish` removes the directories this run asked for if every
process was green, and keeps them (printing their paths) otherwise or when the operator opted
out with ``--keep-basetemp`` or ``TESSERA_KEEP_BASETEMP=1``. It never globs the scratch
directory and never touches a path it did not hand out. Cleanup never raises and never changes
the run's exit code.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

SCRATCH_DIR = ".pytest-tmp"
KEEP_ENV = "TESSERA_KEEP_BASETEMP"


def keep_requested(flag: bool = False, environ: Mapping[str, str] | None = None) -> bool:
    """True when the operator asked to keep the scratch (``--keep-basetemp`` or the env)."""
    env = os.environ if environ is None else environ
    return bool(flag) or env.get(KEEP_ENV, "") not in {"", "0"}


def new_run_id() -> str:
    """A short id that is unique per run (pid plus random), safe in a directory name."""
    return f"{os.getpid():x}{secrets.token_hex(2)}"


def _writable(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode | stat.S_IRWXU)
    except OSError:
        pass


def _clear(path: Path) -> None:
    """Remove ``path`` and everything under it; never follow a symlink out of the tree."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(path)  # a file or a symlink itself (the parent is already writable)
        return
    _writable(path, info.st_mode)  # a read-only directory cannot be listed or emptied
    for name in os.listdir(path):
        _clear(path / name)
    os.rmdir(path)


def inside(path: Path, scratch_root: Path) -> bool:
    """True when ``path`` is a real (non-symlink) strict descendant of ``scratch_root``."""
    try:
        if path.is_symlink():
            return False
        root = scratch_root.resolve(strict=True)
        return root in path.resolve(strict=True).parents
    except OSError:
        return False


def remove_tree(path: Path, scratch_root: Path) -> bool:
    """Remove one directory tree under ``scratch_root`` (read-only parts made writable first).

    Refuses (returns False, removing nothing) a path that is a symlink or not a strict descendant
    of ``scratch_root``. Returns True when the path is gone."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return True
    if not inside(path, scratch_root):
        return False
    _clear(path)
    return True


class Scratch:
    """The basetemps one run creates under ``<root>/.pytest-tmp`` and their cleanup."""

    def __init__(
        self,
        root: Path | str,
        *,
        keep: bool = False,
        run_id: str | None = None,
        out: Callable[[str], None] = print,
    ) -> None:
        self.root = Path(root)
        self.scratch_root = self.root / SCRATCH_DIR
        self.keep = keep
        self.run_id = run_id or new_run_id()
        self.out = out
        self.owned: list[Path] = []
        self._created_parent = False

    def path(self, name: str) -> Path:
        """A fresh basetemp path ``.pytest-tmp/<name>-<run id>`` owned by this run."""
        if not self.scratch_root.exists():
            try:
                self.scratch_root.mkdir()
                self._created_parent = True
            except FileExistsError:
                pass
        target = self.scratch_root / f"{name}-{self.run_id}"
        self.owned.append(target)
        return target

    def finish(self, green: bool) -> None:
        """Remove this run's own basetemps when ``green`` and not kept; else say where they are."""
        try:
            self._finish(green)
        except Exception as error:  # noqa: BLE001 - cleanup must never change the exit code
            self.out(f"test scratch: cleanup stopped early ({type(error).__name__})")

    def _finish(self, green: bool) -> None:
        present = [path for path in self.owned if path.exists() or path.is_symlink()]
        if not green or self.keep:
            reason = "kept (--keep-basetemp)" if green else "kept for triage"
            for path in present:
                self.out(f"test scratch {reason}: {path}")
            return
        failed = []
        for path in present:
            try:
                if not remove_tree(path, self.scratch_root):
                    failed.append(path)
            except OSError:
                failed.append(path)
        for path in failed:
            self.out(f"test scratch not removed: {path}")
        self.owned = [path for path in self.owned if path in failed]
        if self._created_parent and not failed:
            try:
                self.scratch_root.rmdir()  # only if this run made it and it is now empty
            except OSError:
                pass


def remove_listed(paths: Iterable[Path], scratch_root: Path) -> list[Path]:
    """Remove each listed path under ``scratch_root``; returns the ones that were refused or
    could not be removed. Never raises."""
    left = []
    for path in paths:
        try:
            if not remove_tree(Path(path), scratch_root):
                left.append(Path(path))
        except OSError:
            left.append(Path(path))
    return left
