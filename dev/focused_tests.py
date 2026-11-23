"""Select and run only the tests for the code a branch (or a commit range) changed.

Thin wrapper kept for ``.venv/bin/python dev/focused_tests.py``; the logic lives in
:mod:`tessera_control.focused_tests` (also ``tessera dev focused-tests``). Run with
``--help`` for the options.

The module is loaded from this checkout's ``src/`` by path: it needs only the standard
library, so this works without the package's dependencies and never picks up another
installed copy of the package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "tessera_control" / "focused_tests.py"
_NAME = "_tessera_focused_tests"
if _NAME in sys.modules:
    _core = sys.modules[_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_NAME, _SOURCE)
    _core = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = _core  # dataclasses look the module up while it executes
    _spec.loader.exec_module(_core)

# Re-export the public API (select, analyze, select_tests, git_changed, imports, ...).
globals().update({name: value for name, value in vars(_core).items() if not name.startswith("_")})


def main(argv: list[str] | None = None) -> int:
    """The core CLI, with the branch diff looked up here so ``git_changed`` can be replaced
    on this module."""

    def changed(root: Path, range_spec: str | None, base: str) -> list[str]:
        if range_spec is None:
            return git_changed(root, base)  # noqa: F821 - re-exported from the core module
        return _core.changed_files(root, range_spec, base)

    return _core.main(argv, changed_files_fn=changed)


if __name__ == "__main__":
    raise SystemExit(main())
