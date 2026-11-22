"""Select and run only the tests for the code a branch changed.

Working loop: run the tests of the changed modules and their direct dependents, stopping at
the first failure (``pytest -x``). The full suite is run once per branch, right before
merge (``make test-parallel``).

Selection is by file names and import text; no code under test is imported. One level of
dependents is followed:

* a changed test file selects itself; a changed helper module under ``tests/`` selects the
  tests that mention it; a changed ``conftest.py`` selects every test below its directory
  (and is reported as broad when that is the whole suite);
* a changed ``src/<package>/.../<module>.py`` selects the tests named ``test_<module>.py``
  or ``test_<module>_*.py`` and the tests that import it, plus the same for every source
  module, in any package, that directly imports it. A changed ``__init__.py`` stands for its
  package or subpackage;
* changed documentation, ``deploy/`` files or ``pyproject.toml`` select the documentation
  link tests, and ``deploy/`` also selects tests that mention the changed path;
* changed ``scripts/platform_validation`` selects the harness unit tests; other
  ``scripts/`` and ``dev/`` files select the tests that mention the file's name.

A changed file that maps to no test (for example the Makefile or a data file under
``src/``) is reported on stderr. If nothing at all is selected while such files changed,
the run stops with status 2 and says to run the full suite; it never falls back silently
and never reports success for changes it could not map.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DOC_TESTS = ("tests/test_docs_links.py",)
HARNESS_TESTS = ("tests/test_platform_validation_harness.py",)


@dataclass
class Selection:
    tests: set[str] = field(default_factory=set)
    unmapped: list[str] = field(default_factory=list)
    broad: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return sorted(self.tests)


def source_module(path: str) -> tuple[str, str] | None:
    """('pkg', 'sub.mod') for src/pkg/sub/mod.py; the dotted package for an __init__.py
    ('pkg', 'sub'), or ('pkg', '') for the package root."""
    parts = Path(path).as_posix().split("/")
    if len(parts) < 3 or parts[0] != "src" or not parts[-1].endswith(".py"):
        return None
    package, rest = parts[1], parts[2:]
    stem = rest[-1][:-3]
    dotted = rest[:-1] if stem == "__init__" else [*rest[:-1], stem]
    return package, ".".join(dotted)


def module_key(path: str) -> tuple[str, str] | None:
    """Kept for callers of the earlier flat form: ('pkg', 'mod') for src/pkg/mod.py."""
    key = source_module(path)
    if key is None or "." in key[1] or not key[1]:
        return None
    return key


def imports(text: str, package: str, module: str) -> bool:
    """Whether text imports ``package.module`` (module may be dotted) or, for the package
    root (module ''), the package at all."""
    escaped = re.escape(package)
    if not module:
        return bool(re.search(rf"(?:^|\n)\s*(?:from|import)\s+{escaped}\b", text))
    dotted = r"\.".join(re.escape(part) for part in module.split("."))
    if re.search(rf"(?:from|import)\s+{escaped}\.{dotted}\b", text):
        return True
    head, _, leaf = module.rpartition(".")
    owner = ".".join(filter(None, [package, head]))
    owner_pattern = r"\.".join(re.escape(part) for part in owner.split("."))
    return bool(re.search(rf"from\s+{owner_pattern}\s+import\s+[^\n]*\b{re.escape(leaf)}\b", text))


def relative_imports(text: str, module: str) -> bool:
    """Whether a sibling module imports ``module`` (last component) with a relative import."""
    leaf = re.escape(module.rsplit(".", 1)[-1])
    return bool(re.search(rf"from\s+\.+{leaf}\b|from\s+\.+\s+import\s+[^\n]*\b{leaf}\b", text))


def named_for(stem: str, module: str) -> bool:
    """Whether a test file stem is the module's test file: ``test_<module>`` or a
    ``test_<module>_<suffix>`` sibling (not ``test_<module>s`` or an unrelated longer name)."""
    leaf = module.rsplit(".", 1)[-1]
    return bool(leaf) and (stem == f"test_{leaf}" or stem.startswith(f"test_{leaf}_"))


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def dependents(root: Path, package: str, module: str) -> set[tuple[str, str]]:
    """Source modules, in any package, that directly import ``package.module``."""
    found: set[tuple[str, str]] = set()
    for path in sorted((root / "src").glob("*/**/*.py")):
        key = source_module(path.relative_to(root).as_posix())
        if key is None or key == (package, module) or path.stem == "__init__":
            continue
        text = read(path)
        if imports(text, package, module) or (key[0] == package and relative_imports(text, module)):
            found.add(key)
    return found


def mentions(text: str, name: str) -> bool:
    if not name:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text))


def analyze(root: Path, changed: list[str]) -> Selection:
    test_files = sorted((root / "tests").rglob("test_*.py"))
    contents = {path: read(path) for path in test_files}
    selection = Selection()

    def add(path: Path) -> None:
        selection.tests.add(path.relative_to(root).as_posix())

    def add_existing(names: tuple[str, ...]) -> bool:
        present = [name for name in names if (root / name).exists()]
        selection.tests.update(present)
        return bool(present)

    def add_mentioning(name: str) -> bool:
        hit = False
        for path, text in contents.items():
            if mentions(text, name):
                add(path)
                hit = True
        return hit

    for changed_path in changed:
        posix = Path(changed_path).as_posix()
        name = Path(posix).name
        stem = Path(posix).stem
        if posix.startswith("tests/"):
            if name.startswith("test_") and name.endswith(".py"):
                if (root / posix).exists():  # a deleted test leaves nothing to run
                    selection.tests.add(posix)
            elif name == "conftest.py":
                scope = Path(posix).parent
                below = [p for p in test_files if scope in p.relative_to(root).parents]
                for path in below:
                    add(path)
                if scope.as_posix() == "tests":
                    selection.broad.append(posix)
                if not below:
                    selection.unmapped.append(posix)
            elif name.endswith(".py"):
                if not add_mentioning(stem):  # a shared helper module nobody mentions
                    selection.unmapped.append(posix)
            else:
                selection.unmapped.append(posix)
            continue
        if posix.startswith("scripts/platform_validation/"):
            if not add_existing(HARNESS_TESTS):
                selection.unmapped.append(posix)
            continue
        if (
            posix.startswith(("docs/", "deploy/"))
            or name.endswith(".md")
            or posix == "pyproject.toml"
        ):
            hit = add_existing(DOC_TESTS)
            if posix.startswith("deploy/"):
                hit = add_mentioning(posix.rsplit("/", 1)[0]) or hit
            if not hit:
                selection.unmapped.append(posix)
            continue
        if posix.startswith(("scripts/", "dev/")) and name.endswith(".py"):
            if not add_mentioning(stem):
                selection.unmapped.append(posix)
            continue
        key = source_module(posix)
        if key is None:
            selection.unmapped.append(posix)
            continue
        package, module = key
        hit = False
        for owner, dotted in {key} | dependents(root, package, module):
            for path, text in contents.items():
                if named_for(path.stem, dotted) or imports(text, owner, dotted):
                    add(path)
                    hit = True
        if not module:
            selection.broad.append(posix)  # a package root is imported by many tests
        if not hit:
            selection.unmapped.append(posix)
    return selection


def select_tests(root: Path, changed: list[str]) -> list[str]:
    """The selected test files (see :func:`analyze` for unmapped and broad changes)."""
    return analyze(root, changed).files


def git_changed(root: Path, base: str) -> list[str]:
    """Files changed since the merge base with ``base``, plus uncommitted and untracked."""

    def git(*args: str) -> list[str]:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
        return [line for line in done.stdout.splitlines() if line]

    merge_base = (git("merge-base", "HEAD", base) or ["HEAD"])[0]
    names = set(git("diff", "--name-only", merge_base))
    names.update(git("diff", "--name-only"))
    names.update(git("ls-files", "--others", "--exclude-standard"))
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main", help="branch to diff against (default main)")
    parser.add_argument("--list", action="store_true", help="print the selection and stop")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args, pytest_args = parser.parse_known_args(argv)
    root = args.root.resolve()
    selection = analyze(root, git_changed(root, args.base))
    if selection.unmapped:
        print(
            "focused tests: no test maps to: " + ", ".join(sorted(selection.unmapped)),
            file=sys.stderr,
        )
    if selection.broad:
        print(
            "focused tests: these changes affect many tests; consider the full suite "
            "(make test-parallel): " + ", ".join(sorted(selection.broad)),
            file=sys.stderr,
        )
    files = selection.files
    if not files:
        if selection.unmapped:
            print(
                "focused tests: nothing could be selected for these changes; "
                "run the full suite (make test-parallel)",
                file=sys.stderr,
            )
            return 2
        print("focused tests: no test is selected by the changes (nothing run)")
        return 0
    if args.list:
        print("\n".join(files))
        return 0
    print(f"focused tests: {len(files)} file(s), stopping at the first failure")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        ".pytest-tmp-focused",
        *pytest_args,
        *files,
    ]
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
