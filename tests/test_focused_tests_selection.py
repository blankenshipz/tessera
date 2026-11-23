"""Tests for tessera_control.focused_tests: ranges, overrides, caps, smoke set, sharding and
running. Nothing here runs pytest on the repository: process creation is injected."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tessera_control import focused_tests as ft

ROOT = Path(__file__).parents[1]


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c", "user.name=t",
            "-c", "user.email=t@example.invalid",
            "-c", "commit.gpgsign=false",
            "-c", "tag.gpgsign=false",
            *args,
        ],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def repo(tmp_path):
    """A source tree with a hub module (many importers) and one dependent."""
    write(tmp_path / "src/pkg/__init__.py")
    write(tmp_path / "src/pkg/hub.py", "X = 1\n")
    write(tmp_path / "src/pkg/user.py", "from pkg.hub import X\n")
    write(tmp_path / "src/pkg/leaf.py", "Y = 1\n")
    write(tmp_path / "tests/test_hub.py")
    write(tmp_path / "tests/test_hub_extra.py")
    for index in range(6):
        write(tmp_path / f"tests/test_imp{index}.py", "from pkg.hub import X\n")
    write(tmp_path / "tests/test_user.py")
    write(tmp_path / "tests/test_leaf.py")
    write(tmp_path / "tests/test_core_a.py")
    write(tmp_path / "tests/test_core_b.py")
    write(tmp_path / "tests/test_smoke_one.py")
    write(tmp_path / "tests/test_smoke_two.py")
    write(tmp_path / "tests/test_docs_links.py")
    return tmp_path


def overrides_file(root: Path, text: str) -> None:
    write(root / ft.OVERRIDES_FILE, text)


SMOKE = '[smoke]\nfiles = ["tests/test_smoke_one.py", "tests/test_smoke_two.py"]\n'


# --- changed_files -----------------------------------------------------------------------


def test_a_range_is_the_commit_diff_only_without_working_tree_or_untracked_files(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path / "a.py", "1\n")
    write(tmp_path / "b.py", "1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    write(tmp_path / "a.py", "2\n")
    git(tmp_path, "commit", "-q", "-am", "change a")
    head = git(tmp_path, "rev-parse", "HEAD")
    write(tmp_path / "b.py", "2\n")  # uncommitted
    write(tmp_path / "new.py", "")  # untracked
    assert ft.changed_files(tmp_path, f"{base}..{head}") == ["a.py"]
    assert ft.changed_files(tmp_path, base) == ["a.py"]  # a single rev means rev..HEAD
    assert ft.changed_files(tmp_path, f"{base}...HEAD") == ["a.py"]
    # Without a range: branch diff plus uncommitted and untracked (today's behaviour).
    assert ft.changed_files(tmp_path, None, base=base) == ["a.py", "b.py", "new.py"]


@pytest.mark.parametrize(
    "bad", ["", "..", "a..", "..b", "a....b", "a..b..c", "-p..HEAD", "a b..c", "--output=x"]
)
def test_a_malformed_range_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="malformed range"):
        ft.changed_files(tmp_path, bad)


def test_an_unknown_revision_is_an_error_not_an_empty_diff(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path / "a.py")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    with pytest.raises(ValueError, match="failed"):
        ft.changed_files(tmp_path, "nope..HEAD")


# --- selection and overrides -------------------------------------------------------------


def test_selection_records_why_each_file_was_picked(repo):
    selection = ft.select(repo, ["src/pkg/hub.py", "tests/test_leaf.py"], smoke=False)
    reasons = selection.reasons
    assert reasons["tests/test_hub.py"] == ["named"]
    assert reasons["tests/test_hub_extra.py"] == ["named"]
    assert reasons["tests/test_imp0.py"] == ["imports"]
    assert reasons["tests/test_user.py"] == ["dependent"]
    assert reasons["tests/test_leaf.py"] == ["changed"]
    assert selection.files == sorted(reasons)
    assert selection.smoke == [] and selection.truncated == []


def test_add_override_adds_tests_and_globs(repo):
    overrides_file(
        repo,
        '[[module]]\nmatch = ["src/pkg/leaf.py"]\nmode = "add"\ntests = ["tests/test_core_*.py"]\n',
    )
    selection = ft.select(repo, ["src/pkg/leaf.py"], smoke=False)
    assert selection.files == ["tests/test_core_a.py", "tests/test_core_b.py", "tests/test_leaf.py"]
    assert selection.reasons["tests/test_core_a.py"] == ["override"]
    assert selection.reasons["tests/test_leaf.py"] == ["named"]


def test_replace_override_drops_importers_and_dependents_but_keeps_named_tests(repo):
    overrides_file(
        repo,
        '[[module]]\nmatch = ["src/pkg/hub.py"]\nmode = "replace"\n'
        'tests = ["tests/test_core_a.py"]\n',
    )
    selection = ft.select(repo, ["src/pkg/hub.py"], smoke=False)
    assert selection.files == [
        "tests/test_core_a.py",
        "tests/test_hub.py",
        "tests/test_hub_extra.py",
    ]


def test_a_hub_cap_keeps_by_priority_and_reports_the_cut_never_the_full_suite(repo, capsys):
    overrides_file(
        repo,
        '[[module]]\nmatch = ["src/pkg/*.py"]\nmode = "add"\n'
        'tests = ["tests/test_core_b.py"]\ncap = 4\n',
    )
    selection = ft.select(repo, ["src/pkg/hub.py", "tests/test_imp5.py"], smoke=False)
    # override, then the changed importer, then named; imports/dependents are cut.
    assert selection.files == [
        "tests/test_core_b.py",
        "tests/test_hub.py",
        "tests/test_hub_extra.py",
        "tests/test_imp5.py",
    ]
    assert selection.reasons["tests/test_imp5.py"] == ["changed"]
    assert selection.truncated == ["src/pkg/hub.py: kept 4 of 10 test files (cap 4), dropped 6"]
    all_tests = {p.relative_to(repo).as_posix() for p in (repo / "tests").glob("test_*.py")}
    assert selection.tests < all_tests


def test_the_default_hub_cap_and_max_files_cut_too(repo):
    overrides_file(repo, "[limits]\nhub_cap = 3\nmax_files = 2\n")
    selection = ft.select(repo, ["src/pkg/hub.py"], smoke=False)
    assert selection.files == ["tests/test_hub.py", "tests/test_hub_extra.py"]
    assert [line.split(":")[0] for line in selection.truncated] == ["src/pkg/hub.py", "max_files"]
    capped = ft.select(repo, ["src/pkg/hub.py"], smoke=False, max_files=1)
    assert capped.files == ["tests/test_hub.py"]


def test_the_global_cap_keeps_changed_tests_before_dependents(repo):
    selection = ft.select(repo, ["src/pkg/hub.py", "tests/test_leaf.py"], smoke=False, max_files=1)
    assert selection.files == ["tests/test_leaf.py"]
    assert selection.truncated[-1].startswith("max_files: kept 1 of")


def test_the_smoke_set_is_appended_to_a_non_empty_selection_only(repo):
    overrides_file(repo, SMOKE)
    selection = ft.select(repo, ["src/pkg/leaf.py"])
    assert selection.files == ["tests/test_leaf.py"]
    assert selection.smoke == ["tests/test_smoke_one.py", "tests/test_smoke_two.py"]
    assert selection.all_files == [
        "tests/test_leaf.py",
        "tests/test_smoke_one.py",
        "tests/test_smoke_two.py",
    ]
    assert selection.reasons["tests/test_smoke_one.py"] == ["smoke"]
    assert ft.select(repo, ["src/pkg/leaf.py"], smoke=False).all_files == ["tests/test_leaf.py"]
    assert ft.select(repo, ["tests/test_gone.py"]).all_files == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[smoke]\nfiles = ["tests/test_missing.py"]\n', "no such file"),
        ("[limits]\nmax_file = 3\n", "unknown key"),
        ("[surprise]\n", "unknown key"),
        ('[[module]]\nmatch = ["src/pkg/hub.py"]\nextra = 1\n', "unknown key"),
        ('[[module]]\nmatch = ["src/pkg/hub.py"]\ntests = ["tests/test_zz*.py"]\n', "nothing"),
        ('[[module]]\nmatch = ["src/nowhere/*.py"]\ntests = ["tests/test_hub.py"]\n', "nothing"),
        ('[[module]]\nmatch = ["src/pkg/gone.py"]\ntests = ["tests/test_hub.py"]\n', "no such"),
        ('[[module]]\nmatch = ["src/pkg/hub.py"]\nmode = "swap"\n', "mode"),
        ('[[module]]\nmatch = ["src/pkg/hub.py"]\nmode = "replace"\n', "needs 'tests'"),
        ('[[module]]\nmatch = ["src/pkg/hub.py"]\ncap = 0\n', "positive integer"),
        ("[limits]\nmax_jobs = true\n", "positive integer"),
        ('[[module]]\ntests = ["tests/test_hub.py"]\n', "'match' is required"),
        ("[limits\n", "focused_tests_overrides"),
    ],
)
def test_overrides_validation_is_loud(repo, text, message):
    overrides_file(repo, text)
    with pytest.raises(ValueError, match=message):
        ft.load_overrides(repo)


def test_no_overrides_file_means_defaults(repo):
    assert ft.load_overrides(repo) == ft.Overrides()
    assert ft.Overrides().limits == ft.Limits(max_files=60, max_jobs=4, hub_cap=40)


def test_the_checked_in_overrides_file_validates_against_this_repository():
    overrides = ft.load_overrides(ROOT)
    assert 1 <= len(overrides.smoke) <= 12
    assert overrides.modules
    for path in [*overrides.smoke, *(t for rule in overrides.modules for t in rule.tests)]:
        assert (ROOT / path).is_file(), path
    for rule in overrides.modules:
        for pattern in rule.match:
            assert (ROOT / pattern).is_file(), pattern


# --- main --------------------------------------------------------------------------------


def changed_as(paths):
    return lambda root, range_spec, base: list(paths)


def test_unmapped_changes_with_an_empty_selection_exit_2(repo, capsys):
    overrides_file(repo, SMOKE)
    assert ft.main(["--root", str(repo)], changed_files_fn=changed_as(["Makefile"])) == 2
    err = capsys.readouterr().err
    assert "no test maps to: Makefile" in err and "run the full suite explicitly" in err


def test_a_malformed_range_or_bad_overrides_exit_2(repo, capsys):
    assert ft.main(["--root", str(repo), "a..b..c"]) == 2
    assert "malformed range" in capsys.readouterr().err
    overrides_file(repo, "[bogus]\n")
    assert ft.main(["--root", str(repo)], changed_files_fn=changed_as(["src/pkg/leaf.py"])) == 2
    assert "unknown key" in capsys.readouterr().err


def test_main_passes_the_range_to_changed_files_and_the_rest_to_pytest(repo, monkeypatch):
    seen = {}

    def fake_run(files, root, jobs, pytest_args, **kwargs):
        seen.update(files=files, jobs=jobs, args=list(pytest_args))
        return 0

    def changed(root, range_spec, base):
        seen["range"] = range_spec
        return ["src/pkg/leaf.py"]

    monkeypatch.setattr(ft, "run", fake_run)
    argv = ["--root", str(repo), "main..HEAD", "--jobs", "9", "-k", "x", "--no-smoke"]
    assert ft.main(argv, changed_files_fn=changed) == 0
    assert seen == {
        "range": "main..HEAD",
        "files": ["tests/test_leaf.py"],
        "jobs": 4,  # clamped to max_jobs
        "args": ["-k", "x"],
    }


def test_list_explain_and_smoke_only(repo, capsys):
    overrides_file(repo, SMOKE)
    changed = changed_as(["src/pkg/leaf.py"])
    assert ft.main(["--root", str(repo), "--list"], changed_files_fn=changed) == 0
    assert capsys.readouterr().out.split() == [
        "tests/test_leaf.py",
        "tests/test_smoke_one.py",
        "tests/test_smoke_two.py",
    ]
    assert ft.main(["--root", str(repo), "--explain"], changed_files_fn=changed) == 0
    assert "tests/test_leaf.py  <- named" in capsys.readouterr().out
    smoke = ["--root", str(repo), "--list", "--smoke-only"]
    assert ft.main(smoke, changed_files_fn=changed_as(["Makefile"])) == 0
    assert capsys.readouterr().out.split() == ["tests/test_smoke_one.py", "tests/test_smoke_two.py"]


def test_full_is_the_only_path_to_the_whole_suite(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(ft, "run", lambda *a, **k: calls.append(("run", a)) or 0)
    monkeypatch.setattr(ft, "run_full", lambda *a, **k: calls.append(("full", a)) or 0)
    for changed in (["Makefile"], ["src/pkg/hub.py"], ["tests/conftest.py"], []):
        ft.main(["--root", str(repo)], changed_files_fn=changed_as(changed))
    assert [kind for kind, _ in calls] == ["run", "run"]
    assert ft.main(["--root", str(repo), "--full", "--jobs", "6", "-x"]) == 0
    assert calls[-1] == ("full", (repo.resolve(), 6, ["-x"]))


def test_run_full_delegates_to_pytest_parallel(repo):
    seen = []
    ft.run_full(repo, 3, ["-x"], call=lambda command, cwd: seen.append((command, cwd)) or 0)
    (command, cwd), *_ = seen
    assert command[1].endswith("dev/pytest_parallel.py") and command[2:] == ["--jobs", "3", "-x"]
    assert cwd == repo


# --- sharding and running ----------------------------------------------------------------


FILES = [f"tests/test_f{index}.py" for index in range(23)]


def test_jobs_are_clamped_and_shards_are_deterministic_and_disjoint():
    assert len(ft.plan_shards(FILES, 0)) == 1
    assert len(ft.plan_shards(FILES, 99)) == 4
    assert len(ft.plan_shards(FILES, 99, max_jobs=6)) == 6
    shards = ft.plan_shards(FILES, 3)
    assert shards == ft.plan_shards(list(reversed(FILES)), 3)
    flat = [path for shard in shards for path in shard]
    assert sorted(flat) == sorted(FILES) and len(flat) == len(set(flat))
    assert ft.plan_shards([*FILES, FILES[0]], 3) == shards  # a duplicate is run once


def test_shards_match_dev_pytest_shard():
    spec = importlib.util.spec_from_file_location("pytest_shard", ROOT / "dev/pytest_shard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tests = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("test_*.py"))
    shards = ft.plan_shards(tests, 4)
    for index in range(4):
        expected = module.shard_files(ROOT / "tests", index, 4)
        assert shards[index] == sorted(f"tests/{p.relative_to(ROOT / 'tests')}" for p in expected)


class FakePopen:
    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    def __call__(self, command, cwd=None, stdout=None, stderr=None):
        index = len(self.calls)
        self.calls.append(command)
        if stdout is not None:
            stdout.write(f"shard output {index}\n")
        code = self.codes.get(index, 0)
        return type("P", (), {"wait": lambda self: code})()


def basetemp(command):
    return command[command.index("--basetemp") + 1]


def test_one_job_is_one_pytest_process(tmp_path):
    popen = FakePopen()
    assert ft.run(FILES[:3], tmp_path, 1, ["-k", "x"], popen=popen) == 0
    (command,) = popen.calls
    assert command[:4] == [sys.executable, "-m", "pytest", "-x"]
    assert basetemp(command) == str(tmp_path / ".pytest-tmp-focused")
    assert command[-5:] == ["-k", "x", *FILES[:3]]


def test_parallel_runs_at_most_jobs_processes_with_separate_basetemps(tmp_path):
    popen = FakePopen()
    assert ft.run(FILES, tmp_path, 3, popen=popen, log_dir=tmp_path / "logs") == 0
    assert 1 <= len(popen.calls) <= 3
    assert len({basetemp(c) for c in popen.calls}) == len(popen.calls)
    run_files = [path for command in popen.calls for path in command if path in FILES]
    assert sorted(run_files) == sorted(FILES)
    assert sorted(p.name for p in (tmp_path / "logs").iterdir())[0] == "shard0.log"
    assert ft.run(FILES, tmp_path, 50, popen=(many := FakePopen()), log_dir=tmp_path) == 0
    assert len(many.calls) <= ft.DEFAULT_MAX_JOBS


def test_a_failing_shard_fails_the_run(tmp_path, capsys):
    assert ft.run(FILES, tmp_path, 3, popen=FakePopen({1: 1}), log_dir=tmp_path) == 1
    assert "exit 1" in capsys.readouterr().out


def test_run_never_starts_pytest_without_files(tmp_path):
    popen = FakePopen()
    assert ft.run([], tmp_path, 3, popen=popen) == 0
    assert popen.calls == []


def test_a_separator_after_the_range_is_not_passed_to_pytest(tmp_path):
    from tessera_control import focused_tests

    root = tmp_path
    assert focused_tests._split_range(["A..B", "--", "-q"], root) == ("A..B", ["-q"])
    assert focused_tests._split_range(["--", "A..B", "-q"], root) == ("A..B", ["-q"])
    assert focused_tests._split_range(["A..B", "-q"], root) == ("A..B", ["-q"])
    assert focused_tests._split_range(["-q"], root) == (None, ["-q"])


def test_a_replace_override_names_the_importers_it_dropped(repo):
    overrides_file(
        repo,
        '[[module]]\nmatch = ["src/pkg/hub.py"]\nmode = "replace"\n'
        'tests = ["tests/test_core_a.py"]\n',
    )
    selection = ft.select(repo, ["src/pkg/hub.py"], smoke=True)
    assert selection.narrowed and "src/pkg/hub.py" in selection.narrowed[0]
    assert "7 importing or dependent" in selection.narrowed[0]  # six importers + a dependent
    # An add override (the default mode) drops nothing, so nothing is reported.
    overrides_file(
        repo, '[[module]]\nmatch = ["src/pkg/hub.py"]\ntests = ["tests/test_core_a.py"]\n'
    )
    assert ft.select(repo, ["src/pkg/hub.py"], smoke=False).narrowed == []


def test_a_declared_safe_file_is_not_unmapped_and_a_code_pattern_is_refused(repo):
    write(repo / "src/pkg/data/blob.txt", "x")
    overrides_file(repo, '[unmapped]\nsafe = ["src/pkg/data/*.txt"]\n')
    selection = ft.select(repo, ["src/pkg/data/blob.txt", "src/pkg/odd.bin"], smoke=False)
    assert selection.safe_unmapped == ["src/pkg/data/blob.txt"]
    assert selection.unmapped == ["src/pkg/odd.bin"]
    for pattern in ("src/**/*.py", "src/pkg/hub.py", "*.py"):
        overrides_file(repo, f'[unmapped]\nsafe = ["{pattern}"]\n')
        with pytest.raises(ValueError, match="may not cover Python files"):
            ft.load_overrides(repo)


def test_run_blocks_on_an_unmapped_file_unless_allowed(repo, capsys, monkeypatch):
    ran = []
    monkeypatch.setattr(ft, "run", lambda files, *a, **k: ran.append(list(files)) or 0)
    overrides_file(repo, SMOKE)
    changed = changed_as(["src/pkg/leaf.py", "Makefile"])
    assert ft.main(["--root", str(repo)], changed_files_fn=changed) == 2
    assert ran == [] and "no test maps to: Makefile" in capsys.readouterr().err
    assert ft.main(["--root", str(repo), "--allow-unmapped"], changed_files_fn=changed) == 0
    assert ran and "tests/test_leaf.py" in ran[0]
    # Listing and explaining still work (they only report), with the warning.
    assert ft.main(["--root", str(repo), "--list"], changed_files_fn=changed) == 0


def test_the_exact_range_includes_a_non_ancestor_revisions_own_side(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path / "base.txt", "1")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "side")
    write(tmp_path / "side_only.py", "x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "side")
    side = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    write(tmp_path / "main_only.py", "y = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "main")
    main_head = git(tmp_path, "rev-parse", "HEAD")
    three = ft.changed_files(tmp_path, f"{side}..{main_head}")
    exact = ft.changed_files(tmp_path, f"{side}..{main_head}", exact=True)
    assert three == ["main_only.py"]  # the side branch's own change is not in A...B
    assert exact == ["main_only.py", "side_only.py"]  # an upgrade between the trees needs both
