"""Select and run only the tests for the code a branch (or a commit range) changed.

Working loop: run the tests of the changed modules and their direct dependents, stopping at
the first failure (``pytest -x``). The full suite is run once per branch, right before
merge (``make test-parallel``), or explicitly with ``--full``; this module never falls back
to it on its own.

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

``dev/focused_tests_overrides.toml`` adds or replaces tests for matching changed paths,
caps hub modules (a cut keeps override > changed > named > direct import > dependent and is
reported, never widened to the full suite), sets the global file cap and job cap, and names
a small smoke set that is appended to every non-empty selection.

A changed file that maps to no test (for example the Makefile or a data file under
``src/``) is reported on stderr. If nothing at all is selected while such files changed,
the run stops with status 2 and says to run the full suite; it never falls back silently
and never reports success for changes it could not map.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

DOC_TESTS = ("tests/test_docs_links.py",)
HARNESS_TESTS = ("tests/test_platform_validation_harness.py",)
OVERRIDES_FILE = "dev/focused_tests_overrides.toml"
DEFAULT_MAX_FILES = 60
DEFAULT_MAX_JOBS = 4
DEFAULT_HUB_CAP = 40
BASETEMP = ".pytest-tmp-focused"

# Lower rank is kept first when a cap cuts a selection.
PRIORITY = {
    "override": 0,
    "changed": 1,
    "named": 2,
    "imports": 3,
    "docs": 3,
    "harness": 3,
    "mentions": 3,
    "dependent": 4,
    "conftest": 5,
}
_GLOB_CHARS = re.compile(r"[*?\[]")


@dataclass
class Selection:
    """Selected test files and why. ``tests`` excludes the smoke set; ``smoke`` is the
    configured smoke set (empty when not requested), included in ``all_files`` only when
    something else is selected."""

    tests: set[str] = field(default_factory=set)
    unmapped: list[str] = field(default_factory=list)
    broad: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    smoke: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    @property
    def files(self) -> list[str]:
        return sorted(self.tests)

    @property
    def all_files(self) -> list[str]:
        if not self.tests:
            return []
        return sorted(self.tests | set(self.smoke))

    def reason(self, path: str, why: str) -> None:
        found = self.reasons.setdefault(path, [])
        if why not in found:
            found.append(why)
            found.sort(key=lambda item: (PRIORITY.get(item, 9), item))


@dataclass(frozen=True)
class Limits:
    max_files: int = DEFAULT_MAX_FILES
    max_jobs: int = DEFAULT_MAX_JOBS
    hub_cap: int = DEFAULT_HUB_CAP


@dataclass(frozen=True)
class ModuleOverride:
    match: tuple[str, ...]
    mode: str = "add"
    tests: tuple[str, ...] = ()
    cap: int | None = None
    note: str = ""

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.match)


@dataclass(frozen=True)
class Overrides:
    limits: Limits = field(default_factory=Limits)
    smoke: tuple[str, ...] = ()
    modules: tuple[ModuleOverride, ...] = ()


# --- source and test discovery -----------------------------------------------------------


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


# --- overrides ---------------------------------------------------------------------------


def _repo_files(root: Path) -> list[str]:
    """Repository-relative files, skipping hidden and cache directories (for glob checks)."""
    found: list[str] = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(
            name for name in subdirs if not name.startswith(".") and name != "__pycache__"
        )
        base = Path(directory).relative_to(root)
        found.extend((base / name).as_posix() for name in sorted(files))
    return found


def _expect_keys(where: str, table: object, allowed: set[str]) -> dict:
    if not isinstance(table, dict):
        raise ValueError(f"{OVERRIDES_FILE}: {where} must be a table")
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ValueError(f"{OVERRIDES_FILE}: unknown key(s) in {where}: {', '.join(unknown)}")
    return table


def _positive_int(where: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{OVERRIDES_FILE}: {where} must be a positive integer, got {value!r}")
    return value


def _string_list(where: str, value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{OVERRIDES_FILE}: {where} must be a non-empty list of strings")
    return value


def _resolve_tests(root: Path, where: str, patterns: list[str], files: Callable) -> list[str]:
    """Existing test paths for literal paths or globs; a miss is an error (no silent rot)."""
    resolved: list[str] = []
    for pattern in patterns:
        if _GLOB_CHARS.search(pattern):
            hits = [path for path in files() if fnmatch.fnmatchcase(path, pattern)]
            if not hits:
                raise ValueError(f"{OVERRIDES_FILE}: {where}: glob matches nothing: {pattern}")
            resolved.extend(hits)
        elif (root / pattern).is_file():
            resolved.append(Path(pattern).as_posix())
        else:
            raise ValueError(f"{OVERRIDES_FILE}: {where}: no such file: {pattern}")
    return list(dict.fromkeys(resolved))


def load_overrides(root: Path) -> Overrides:
    """Read and validate ``dev/focused_tests_overrides.toml`` (defaults when absent).

    Unknown keys, missing test paths, globs that match nothing and bad caps raise
    ValueError so the file cannot silently rot."""
    root = Path(root)
    path = root / OVERRIDES_FILE
    if not path.is_file():
        return Overrides()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{OVERRIDES_FILE}: {error}") from error
    _expect_keys("the file", data, {"limits", "smoke", "module"})
    cache: list[list[str]] = []

    def files() -> list[str]:
        if not cache:
            cache.append(_repo_files(root))
        return cache[0]

    limits_table = _expect_keys(
        "[limits]", data.get("limits", {}), set(Limits.__dataclass_fields__)
    )
    limits = Limits(
        **{key: _positive_int(f"limits.{key}", value) for key, value in limits_table.items()}
    )
    smoke_table = _expect_keys("[smoke]", data.get("smoke", {}), {"files"})
    smoke: list[str] = []
    if "files" in smoke_table:
        smoke = _resolve_tests(
            root, "smoke.files", _string_list("smoke.files", smoke_table["files"]), files
        )
    entries = data.get("module", [])
    if not isinstance(entries, list):
        raise ValueError(f"{OVERRIDES_FILE}: module must be an array of tables ([[module]])")
    modules: list[ModuleOverride] = []
    for index, entry in enumerate(entries):
        where = f"module[{index}]"
        entry = _expect_keys(where, entry, {"match", "mode", "tests", "cap", "note"})
        if "match" not in entry:
            raise ValueError(f"{OVERRIDES_FILE}: {where}: 'match' is required")
        match = _string_list(f"{where}.match", entry["match"])
        for pattern in match:
            if _GLOB_CHARS.search(pattern):
                if not any(fnmatch.fnmatchcase(p, pattern) for p in files()):
                    raise ValueError(f"{OVERRIDES_FILE}: {where}.match matches nothing: {pattern}")
            elif not (root / pattern).exists():
                raise ValueError(f"{OVERRIDES_FILE}: {where}.match: no such file: {pattern}")
        mode = entry.get("mode", "add")
        if mode not in ("add", "replace"):
            raise ValueError(f"{OVERRIDES_FILE}: {where}.mode must be 'add' or 'replace'")
        tests: list[str] = []
        if "tests" in entry:
            tests = _resolve_tests(
                root, f"{where}.tests", _string_list(f"{where}.tests", entry["tests"]), files
            )
        elif mode == "replace":
            raise ValueError(f"{OVERRIDES_FILE}: {where}: mode 'replace' needs 'tests'")
        cap = _positive_int(f"{where}.cap", entry["cap"]) if "cap" in entry else None
        note = entry.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"{OVERRIDES_FILE}: {where}.note must be a string")
        modules.append(ModuleOverride(tuple(match), mode, tuple(tests), cap, note))
    return Overrides(limits, tuple(smoke), tuple(modules))


# --- selection ---------------------------------------------------------------------------


def _cut(candidates: dict[str, str], cap: int) -> tuple[list[str], int]:
    """Keep at most ``cap`` paths by reason priority, then path; return (kept, dropped)."""
    ranked = sorted(candidates, key=lambda path: (PRIORITY.get(candidates[path], 9), path))
    return ranked[:cap], max(0, len(ranked) - cap)


def select(
    root: Path,
    changed: list[str],
    *,
    overrides: Overrides | None = None,
    smoke: bool = True,
    max_files: int | None = None,
) -> Selection:
    """The tests for ``changed`` (repository-relative paths), with reasons, caps applied and
    the smoke set attached. Never widens to the full suite."""
    root = Path(root)
    if overrides is None:
        overrides = load_overrides(root)
    limits = overrides.limits
    if max_files is None:
        max_files = limits.max_files
    if max_files < 1:
        raise ValueError("max_files must be at least 1")
    test_files = sorted((root / "tests").rglob("test_*.py"))
    contents = {path.relative_to(root).as_posix(): read(path) for path in test_files}
    changed_set = {Path(path).as_posix() for path in changed}
    selection = Selection()
    best: dict[str, str] = {}

    def mentioning(name: str) -> list[str]:
        return [path for path, text in contents.items() if mentions(text, name)]

    for changed_path in changed:
        posix = Path(changed_path).as_posix()
        candidates: dict[str, str] = {}

        def offer(paths: Iterable[str], why: str, into: dict[str, str] = candidates) -> None:
            """Record ``why`` for each path, keeping its highest-priority reason; a test
            that is itself changed counts as 'changed'."""
            for path in paths:
                if path in changed_set and path in contents:
                    why_here = "changed" if PRIORITY[why] > PRIORITY["changed"] else why
                else:
                    why_here = why
                current = into.get(path)
                if current is None or PRIORITY[why_here] < PRIORITY[current]:
                    into[path] = why_here

        rules = [rule for rule in overrides.modules if rule.matches(posix)]
        replace = any(rule.mode == "replace" for rule in rules)
        status = _computed(root, posix, contents, offer, mentioning, replace=replace)
        if status == "deleted":
            continue  # a deleted test leaves nothing to run and is not unmapped
        broad = status == "broad"
        for rule in rules:
            offer(rule.tests, "override")
        if broad:
            selection.broad.append(posix)
        if not candidates:
            selection.unmapped.append(posix)
            continue
        caps = [rule.cap for rule in rules if rule.cap is not None]
        cap = min(caps) if caps else limits.hub_cap
        kept, dropped = _cut(candidates, cap)
        if dropped:
            selection.truncated.append(
                f"{posix}: kept {len(kept)} of {len(candidates)} test files "
                f"(cap {cap}), dropped {dropped}"
            )
        for path in kept:
            why = candidates[path]
            selection.reason(path, why)
            if path not in best or PRIORITY[why] < PRIORITY[best[path]]:
                best[path] = why

    kept, dropped = _cut(best, max_files)
    if dropped:
        selection.truncated.append(
            f"max_files: kept {len(kept)} of {len(best)} test files "
            f"(cap {max_files}), dropped {dropped}"
        )
    selection.tests = set(kept)
    selection.reasons = {path: selection.reasons[path] for path in kept}
    if smoke:
        selection.smoke = list(overrides.smoke)
        for path in selection.smoke:
            selection.reason(path, "smoke")
        if selection.tests and selection.smoke:
            selection.notes.append(f"smoke set appended ({len(selection.smoke)} file(s))")
    return selection


def _computed(root, posix, contents, offer, mentioning, *, replace: bool = False) -> str | None:
    """Offer the rule-based candidates for one changed path. With ``replace`` (a 'replace'
    override matched) only the changed test itself and the tests named for a source module
    are offered. Returns 'broad' when the change affects many tests, 'deleted' for a
    deleted test file."""
    name = Path(posix).name
    stem = Path(posix).stem
    if posix.startswith("tests/") and name.startswith("test_") and name.endswith(".py"):
        if posix not in contents:
            return "deleted"
        offer([posix], "changed")
        return None
    key = source_module(posix)
    if replace:
        if key is not None:
            offer([p for p in contents if named_for(Path(p).stem, key[1])], "named")
        return None
    if posix.startswith("tests/"):
        if name == "conftest.py":
            scope = Path(posix).parent
            offer([p for p in contents if scope in Path(p).parents], "conftest")
            if scope.as_posix() == "tests":
                return "broad"
        elif name.endswith(".py"):
            offer(mentioning(stem), "mentions")  # a shared helper module
        return None
    if posix.startswith("scripts/platform_validation/"):
        offer([t for t in HARNESS_TESTS if (root / t).exists()], "harness")
        return None
    if posix.startswith(("docs/", "deploy/")) or name.endswith(".md") or posix == "pyproject.toml":
        offer([t for t in DOC_TESTS if (root / t).exists()], "docs")
        if posix.startswith("deploy/"):
            offer(mentioning(posix.rsplit("/", 1)[0]), "mentions")
        return None
    if posix.startswith(("scripts/", "dev/")) and name.endswith(".py"):
        offer(mentioning(stem), "mentions")
        return None
    if key is None:
        return None
    package, module = key

    def importing(owner: str, dotted: str) -> list[str]:
        leaf = dotted.rsplit(".", 1)[-1] or owner  # cheap substring check before the regex
        return [p for p, text in contents.items() if leaf in text and imports(text, owner, dotted)]

    offer([p for p in contents if named_for(Path(p).stem, module)], "named")
    offer(importing(package, module), "imports")
    for owner, dotted in sorted(dependents(root, package, module)):
        offer([p for p in contents if named_for(Path(p).stem, dotted)], "dependent")
        offer(importing(owner, dotted), "dependent")
    return "broad" if not module else None  # a package root is imported by many tests


def analyze(root: Path, changed: list[str]) -> Selection:
    """The rule-based selection without overrides or smoke set (default caps apply)."""
    return select(root, changed, overrides=Overrides(), smoke=False)


def select_tests(root: Path, changed: list[str]) -> list[str]:
    """The selected test files (see :func:`analyze` for unmapped and broad changes)."""
    return analyze(root, changed).files


# --- changed files -----------------------------------------------------------------------

_REV = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./@^~{}:+-]*")


def parse_range(range_spec: str) -> tuple[str, str]:
    """('A', 'B') for 'A..B' or 'A...B'; ('A', 'HEAD') for a single revision 'A'."""
    spec = range_spec or ""
    separator = "..." if "..." in spec else ".."
    start, _, end = spec.partition(separator) if ".." in spec else (spec, "", "HEAD")
    if not all(_REV.fullmatch(rev) and ".." not in rev for rev in (start, end)):
        raise ValueError(f"malformed range {range_spec!r}; expected A..B or a single revision")
    return start, end


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def git_changed(root: Path, base: str) -> list[str]:
    """Files changed since the merge base with ``base``, plus uncommitted and untracked."""

    def git(*args: str) -> list[str]:
        return [line for line in _git(root, *args).stdout.splitlines() if line]

    merge_base = (git("merge-base", "HEAD", base) or ["HEAD"])[0]
    names = set(git("diff", "--name-only", merge_base))
    names.update(git("diff", "--name-only"))
    names.update(git("ls-files", "--others", "--exclude-standard"))
    return sorted(names)


def changed_files(root: Path, range_spec: str | None = None, base: str = "main") -> list[str]:
    """With ``range_spec`` ('A..B', or a revision meaning rev..HEAD): exactly
    ``git diff --name-only A...B`` (no working tree). Without it: the branch's changes since
    the merge base with ``base`` plus uncommitted and untracked files."""
    root = Path(root)
    if range_spec is None:
        return git_changed(root, base)
    start, end = parse_range(range_spec)
    done = _git(root, "diff", "--name-only", f"{start}...{end}", "--")
    if done.returncode != 0:
        raise ValueError(f"git diff {start}...{end} failed: {done.stderr.strip()}")
    return sorted({line for line in done.stdout.splitlines() if line})


# --- running -----------------------------------------------------------------------------


def shard_of(test_path: str, count: int) -> int:
    """The shard dev/pytest_shard.py gives a test: sha256 of its path relative to tests/."""
    relative = Path(test_path).as_posix().removeprefix("tests/")
    return int.from_bytes(hashlib.sha256(relative.encode()).digest()[:8], "big") % count


def clamp_jobs(jobs: int, max_jobs: int = DEFAULT_MAX_JOBS) -> int:
    return max(1, min(int(jobs), max(1, max_jobs)))


def plan_shards(
    files: list[str], jobs: int, *, max_jobs: int = DEFAULT_MAX_JOBS
) -> list[list[str]]:
    """Deterministic file shards (same rule as dev/pytest_shard.py); ``jobs`` is clamped to
    [1, max_jobs]. Returns one (possibly empty) list per job; no file appears twice."""
    count = clamp_jobs(jobs, max_jobs)
    shards: list[list[str]] = [[] for _ in range(count)]
    for path in sorted(dict.fromkeys(files)):
        shards[shard_of(path, count)].append(path)
    return shards


def pytest_command(
    root: Path, files: Sequence[str], pytest_args: Sequence[str], basetemp: Path, stop: bool
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *(["-x"] if stop else []),
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(basetemp),
        *pytest_args,
        *files,
    ]


def run(
    files: Sequence[str],
    root: Path,
    jobs: int = 1,
    pytest_args: Sequence[str] = (),
    *,
    popen: Callable = subprocess.Popen,
    log_dir: Path | None = None,
    stop_on_first_failure: bool = True,
    max_jobs: int | None = None,
) -> int:
    """Run the given test files (never the whole suite: an empty list runs nothing).

    One pytest process for ``jobs == 1``; otherwise one per non-empty shard, each with its
    own basetemp and log, at most ``jobs`` (clamped to the overrides' max_jobs) at once.
    Exit 1 if any shard fails."""
    root = Path(root)
    files = list(dict.fromkeys(files))
    if not files:
        return 0
    if max_jobs is None:
        try:
            max_jobs = load_overrides(root).limits.max_jobs
        except ValueError:
            max_jobs = DEFAULT_MAX_JOBS
    jobs = clamp_jobs(jobs, max_jobs)
    if jobs == 1:
        command = pytest_command(root, files, pytest_args, root / BASETEMP, stop_on_first_failure)
        return popen(command, cwd=root).wait()
    shards = [shard for shard in plan_shards(files, jobs, max_jobs=max_jobs) if shard]
    log_dir = Path(log_dir) if log_dir else Path(tempfile.mkdtemp(prefix="tessera-focused-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    running = []
    for index, shard in enumerate(shards):
        basetemp = root / f"{BASETEMP}-{index}"
        log_path = log_dir / f"shard{index}.log"
        log = log_path.open("w")
        command = pytest_command(root, shard, pytest_args, basetemp, stop_on_first_failure)
        try:
            process = popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
        except BaseException:
            log.close()
            raise
        running.append((index, len(shard), log, log_path, process))
    failed = []
    for index, count, log, log_path, process in running:
        code = process.wait()
        log.close()
        lines = read(log_path).strip().splitlines()
        tail = lines[-1] if lines else "(no output)"
        print(f"shard {index} ({count} file(s)): exit {code}: {tail}")
        if code != 0:
            failed.append(index)
    print(f"logs: {log_dir}")
    return 1 if failed else 0


def run_full(
    root: Path,
    jobs: int | None = None,
    pytest_args: Sequence[str] = (),
    *,
    call: Callable = subprocess.call,
) -> int:
    """The only full-suite path: dev/pytest_parallel.py. Callers use it only on an
    explicit full-suite request."""
    root = Path(root)
    command = [sys.executable, str(root / "dev" / "pytest_parallel.py")]
    if jobs is not None:
        command += ["--jobs", str(jobs)]
    return call([*command, *pytest_args], cwd=root)


# --- command line ------------------------------------------------------------------------


def _split_range(extra: list[str], root: Path) -> tuple[str | None, list[str]]:
    """The range is the first passthrough token when it looks like ``A..B``; everything
    else goes to pytest unchanged (so a range must come before pytest arguments)."""
    if extra and extra[0] == "--":
        extra = extra[1:]
    if extra and not extra[0].startswith("-") and ".." in extra[0]:
        if not (root / extra[0]).exists():
            rest = extra[1:]
            if rest and rest[0] == "--":  # `RANGE -- pytest args`: the separator is not a file
                rest = rest[1:]
            return extra[0], rest
    return None, extra


def main(argv: list[str] | None = None, *, changed_files_fn: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s [RANGE] [options] [pytest args]",
    )
    parser.add_argument("--base", default="main", help="branch to diff against (default main)")
    parser.add_argument("--list", action="store_true", help="print the selection and stop")
    parser.add_argument(
        "--explain", action="store_true", help="print the selection with reasons and stop"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--jobs", type=int, default=None, help="pytest processes (default 1)")
    smoke = parser.add_mutually_exclusive_group()
    smoke.add_argument("--smoke-only", action="store_true", help="only the smoke set")
    smoke.add_argument("--no-smoke", action="store_true", help="leave out the smoke set")
    parser.add_argument("--max-files", type=int, help="cap the number of selected files")
    parser.add_argument(
        "--full", action="store_true", help="run the whole suite (dev/pytest_parallel.py)"
    )
    args, extra = parser.parse_known_args(argv)
    root = args.root.resolve()
    range_spec, pytest_args = _split_range(extra, root)
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.max_files is not None and args.max_files < 1:
        parser.error("--max-files must be at least 1")
    if args.full:
        print("focused tests: running the full suite (--full)")
        return run_full(root, args.jobs, pytest_args)
    finder = changed_files_fn or changed_files
    try:
        overrides = load_overrides(root)
        changed = finder(root, range_spec, args.base)
        selection = select(
            root,
            changed,
            overrides=overrides,
            smoke=not args.no_smoke,
            max_files=args.max_files,
        )
    except ValueError as error:
        print(f"focused tests: {error}", file=sys.stderr)
        return 2
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
    for line in selection.truncated:
        print(f"focused tests: selection cut (not widened): {line}", file=sys.stderr)
    files = sorted(selection.smoke) if args.smoke_only else selection.all_files
    if not files:
        if selection.unmapped and not args.smoke_only:
            print(
                "focused tests: nothing could be selected for these changes; "
                "run the full suite explicitly (--full or make test-parallel)",
                file=sys.stderr,
            )
            return 2
        print("focused tests: no test is selected by the changes (nothing run)")
        return 0
    if args.explain:
        for path in files:
            print(f"{path}  <- {', '.join(selection.reasons.get(path, []))}")
        return 0
    if args.list:
        print("\n".join(files))
        return 0
    jobs = clamp_jobs(args.jobs or 1, overrides.limits.max_jobs)
    print(f"focused tests: {len(files)} file(s), {jobs} job(s), stopping at the first failure")
    return run(files, root, jobs, pytest_args, max_jobs=overrides.limits.max_jobs)


if __name__ == "__main__":
    raise SystemExit(main())
