"""``tessera dev``: developer commands that need a repository checkout.

``tessera dev focused-tests`` selects the tests for the code a commit range (or the working
branch) changed. By default it only prints the selection and why each file was picked; ``--run``
runs it with the concurrency cap; ``--full`` runs the whole suite and is honoured only together
with ``--run``. Selection and running live in :mod:`tessera_control.focused_tests`; this module
finds the checkout and maps the options onto it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

NO_CHECKOUT = (
    "no repository checkout found: needs a git work tree with tests/ and src/ (an installed "
    "package has none); run it from a checkout or pass --root"
)


def add_dev_commands(subcommands: argparse._SubParsersAction) -> None:
    from .dev_cli import dev_actions

    actions = dev_actions(subcommands)
    focused = actions.add_parser(
        "focused-tests",
        help="select (and with --run, run) the tests for the changed code",
        description=(
            "Selects the test files for the files changed in RANGE (A..B), or by default for "
            "the working branch against --base plus uncommitted changes, and prints them with "
            "the reasons. --run runs them with the concurrency cap. Changes that no test maps "
            "to are reported; an empty selection with such changes exits 2 (the full suite is "
            "only run when asked for: --run --full)."
        ),
    )
    focused.add_argument("range", nargs="?", metavar="RANGE", help="commit range A..B")
    mode = focused.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print only the selected file paths")
    mode.add_argument(
        "--explain", action="store_true", help="print the selection with reasons (the default)"
    )
    mode.add_argument("--run", action="store_true", help="run the selection")
    focused.add_argument("--jobs", type=int, help="pytest processes at once (default 1, capped)")
    smoke = focused.add_mutually_exclusive_group()
    smoke.add_argument("--smoke-only", action="store_true", help="only the smoke set")
    smoke.add_argument("--no-smoke", action="store_true", help="leave out the smoke set")
    focused.add_argument("--max-files", type=int, help="cap the number of selected files")
    focused.add_argument(
        "--full", action="store_true", help="run the whole suite instead (requires --run)"
    )
    focused.add_argument(
        "--allow-unmapped",
        action="store_true",
        help="with --run: go ahead although some changed files have no test (blocks by default)",
    )
    focused.add_argument("--root", type=Path, help="a directory inside the checkout (default cwd)")
    focused.add_argument("--base", default="main", help="branch to diff against (default main)")
    focused.add_argument(
        "--pytest-arg",
        dest="pytest_args",
        action="append",
        default=[],
        metavar="ARG",
        help="pass ARG to pytest with --run (repeatable)",
    )


def find_root(start: Path) -> Path | None:
    """The top level of the git checkout containing ``start`` when it has ``tests/`` and
    ``src/``; None otherwise."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    top = done.stdout.strip()
    if done.returncode != 0 or not top:
        return None
    root = Path(top)
    if not (root / "tests").is_dir() or not (root / "src").is_dir():
        return None
    return root


def focused_argv(arguments: argparse.Namespace, root: Path) -> list[str]:
    """The :func:`tessera_control.focused_tests.main` arguments for these options."""
    argv = ["--root", str(root), "--base", arguments.base]
    if not arguments.run:
        argv.append("--list" if arguments.list else "--explain")
    if arguments.jobs is not None:
        argv += ["--jobs", str(arguments.jobs)]
    if arguments.smoke_only:
        argv.append("--smoke-only")
    if arguments.no_smoke:
        argv.append("--no-smoke")
    if arguments.max_files is not None:
        argv += ["--max-files", str(arguments.max_files)]
    if arguments.full:
        argv.append("--full")
    if arguments.allow_unmapped:
        argv.append("--allow-unmapped")
    # After "--" so focused_tests.main never parses them as its own options; the range goes
    # first there because main takes the first bare A..B token as the range and drops it.
    rest = ([] if arguments.range is None else [arguments.range]) + arguments.pytest_args
    if rest:
        argv += ["--", *rest]
    return argv


def run_dev(arguments: argparse.Namespace) -> int:
    if arguments.dev_action == "focused-tests":
        return run_focused_tests(arguments)
    return 2  # pragma: no cover - argparse requires a known action


def run_focused_tests(arguments: argparse.Namespace) -> int:
    from . import focused_tests

    if arguments.full and not arguments.run:
        print("focused-tests: --full runs the whole suite and requires --run", file=sys.stderr)
        return 2
    if arguments.pytest_args and not arguments.run:
        print("focused-tests: --pytest-arg only applies with --run", file=sys.stderr)
        return 2
    for name in ("jobs", "max_files"):
        value = getattr(arguments, name)
        if value is not None and value < 1:
            print(f"focused-tests: --{name.replace('_', '-')} must be at least 1", file=sys.stderr)
            return 2
    root = find_root(arguments.root if arguments.root is not None else Path.cwd())
    if root is None:
        print(f"focused-tests: {NO_CHECKOUT}", file=sys.stderr)
        return 2
    return focused_tests.main(focused_argv(arguments, root))
