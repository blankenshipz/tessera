"""``tessera dev focused-tests``: lists by default, runs only with --run, never without a checkout.

Git and pytest are faked: the changed files and the runners are monkeypatched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tessera_control import focused_tests, focused_tests_cli
from tessera_control.cli import main, parser


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A fake checkout with two tests; git reports ``changed`` and runners only record."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_alpha.py").write_text("def test_a():\n    pass\n")
    (root / "tests" / "test_beta.py").write_text("def test_b():\n    pass\n")
    state = {"changed": ["tests/test_alpha.py"], "runs": [], "full": [], "ranges": []}

    def changed_files(where, range_spec=None, base="main"):
        state["ranges"].append((Path(where), range_spec, base))
        return list(state["changed"])

    def run(files, where, jobs=1, pytest_args=(), **options):
        state["runs"].append((list(files), Path(where), jobs, list(pytest_args)))
        return state.get("exit", 0)

    def run_full(where, jobs=None, pytest_args=()):
        state["full"].append((Path(where), jobs, list(pytest_args)))
        return 0

    monkeypatch.setattr(focused_tests, "changed_files", changed_files)
    monkeypatch.setattr(focused_tests, "run", run)
    monkeypatch.setattr(focused_tests, "run_full", run_full)
    monkeypatch.setattr(focused_tests_cli, "find_root", lambda start: root)
    state["root"] = root
    return state


def tessera(monkeypatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["tessera", "dev", "focused-tests", *args])
    return main()


def test_the_group_is_registered_and_help_parses(capsys):
    with pytest.raises(SystemExit) as stopped:
        parser().parse_args(["dev", "focused-tests", "--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "--run" in out and "--full" in out and "RANGE" in out
    arguments = parser().parse_args(["dev", "focused-tests", "a..b", "--jobs", "3"])
    assert arguments.command == "dev" and arguments.dev_action == "focused-tests"
    assert arguments.range == "a..b" and arguments.jobs == 3 and not arguments.run


def test_dev_needs_an_action():
    with pytest.raises(SystemExit) as stopped:
        parser().parse_args(["dev"])
    assert stopped.value.code == 2


def test_the_default_lists_with_reasons_and_runs_nothing(checkout, monkeypatch, capsys):
    assert tessera(monkeypatch, "main..topic") == 0
    out = capsys.readouterr().out
    assert "tests/test_alpha.py  <- changed" in out
    assert "test_beta" not in out
    assert checkout["runs"] == [] and checkout["full"] == []
    assert checkout["ranges"] == [(checkout["root"], "main..topic", "main")]


def test_list_prints_only_paths(checkout, monkeypatch, capsys):
    assert tessera(monkeypatch, "--list", "--base", "trunk") == 0
    assert capsys.readouterr().out.splitlines() == ["tests/test_alpha.py"]
    assert checkout["ranges"] == [(checkout["root"], None, "trunk")]
    assert checkout["runs"] == []


def test_run_uses_the_capped_jobs_and_passes_pytest_args_through(checkout, monkeypatch):
    code = tessera(
        monkeypatch, "a..b", "--run", "--jobs", "3", "--pytest-arg=-q", "--pytest-arg=--jobs"
    )
    assert code == 0
    # The pytest arguments reach the runner untouched (not parsed as the command's own).
    assert checkout["runs"] == [(["tests/test_alpha.py"], checkout["root"], 3, ["-q", "--jobs"])]
    assert checkout["ranges"][-1][1] == "a..b"
    checkout["runs"].clear()
    assert tessera(monkeypatch, "--run", "--jobs", "64") == 0
    assert checkout["runs"][0][2] == focused_tests.DEFAULT_MAX_JOBS  # capped
    assert checkout["full"] == []


def test_a_failing_run_is_its_exit_status(checkout, monkeypatch):
    checkout["exit"] = 1
    assert tessera(monkeypatch, "--run") == 1


def test_full_without_run_is_a_usage_error(checkout, monkeypatch, capsys):
    assert tessera(monkeypatch, "--full") == 2
    assert "requires --run" in capsys.readouterr().err
    assert checkout["full"] == [] and checkout["runs"] == []


def test_full_with_run_runs_the_whole_suite_only_then(checkout, monkeypatch):
    assert tessera(monkeypatch, "--run", "--full", "--jobs", "2") == 0
    assert checkout["full"] == [(checkout["root"], 2, [])]
    assert checkout["runs"] == []


def test_pytest_args_without_run_are_a_usage_error(checkout, monkeypatch):
    assert tessera(monkeypatch, "--pytest-arg=-x") == 2
    assert checkout["runs"] == []


@pytest.mark.parametrize("flag", ["--jobs", "--max-files"])
def test_counts_below_one_are_usage_errors(checkout, monkeypatch, flag):
    assert tessera(monkeypatch, "--run", flag, "0") == 2
    assert checkout["runs"] == []


def test_run_and_list_are_exclusive(checkout, monkeypatch):
    with pytest.raises(SystemExit) as stopped:
        tessera(monkeypatch, "--run", "--list")
    assert stopped.value.code == 2


def test_changes_no_test_maps_to_exit_2_and_run_nothing(checkout, monkeypatch, capsys):
    checkout["changed"] = ["Makefile"]
    assert tessera(monkeypatch, "--run") == 2
    assert "run the full suite explicitly" in capsys.readouterr().err
    assert checkout["runs"] == [] and checkout["full"] == []
    assert tessera(monkeypatch) == 2


def test_nothing_changed_is_success_and_runs_nothing(checkout, monkeypatch):
    checkout["changed"] = []
    assert tessera(monkeypatch, "--run") == 0
    assert checkout["runs"] == []


def test_bad_overrides_exit_2(checkout, monkeypatch, capsys):
    (checkout["root"] / "dev").mkdir()
    (checkout["root"] / focused_tests.OVERRIDES_FILE).write_text("[nonsense]\n")
    assert tessera(monkeypatch) == 2
    assert "unknown key" in capsys.readouterr().err


def test_no_checkout_is_exit_2_with_a_clear_message(monkeypatch, capsys):
    monkeypatch.setattr(focused_tests_cli, "find_root", lambda start: None)
    monkeypatch.setattr(focused_tests, "run", pytest.fail)
    assert tessera(monkeypatch, "--run") == 2
    assert "no repository checkout found" in capsys.readouterr().err


def _fake_git(monkeypatch, *, code: int, out: str):
    calls = []

    def fake_run(argv, **options):
        calls.append((argv, options.get("cwd")))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    monkeypatch.setattr(focused_tests_cli.subprocess, "run", fake_run)
    return calls


def test_find_root_needs_git_and_tests_and_src(tmp_path, monkeypatch):
    calls = _fake_git(monkeypatch, code=0, out=f"{tmp_path}\n")
    assert focused_tests_cli.find_root(tmp_path / "sub") is None  # no tests/ or src/
    assert calls == [(["git", "rev-parse", "--show-toplevel"], tmp_path / "sub")]
    (tmp_path / "tests").mkdir()
    assert focused_tests_cli.find_root(tmp_path) is None  # no src/
    (tmp_path / "src").mkdir()
    assert focused_tests_cli.find_root(tmp_path) == tmp_path
    _fake_git(monkeypatch, code=128, out="")
    assert focused_tests_cli.find_root(tmp_path) is None  # not a work tree


def test_the_mapped_arguments(tmp_path):
    arguments = parser().parse_args(
        ["dev", "focused-tests", "a..b", "--run", "--no-smoke", "--max-files", "5"]
    )
    assert focused_tests_cli.focused_argv(arguments, tmp_path) == [
        "--root", str(tmp_path), "--base", "main", "--no-smoke", "--max-files", "5", "--", "a..b",
    ]  # fmt: skip
    arguments = parser().parse_args(["dev", "focused-tests", "--smoke-only"])
    assert focused_tests_cli.focused_argv(arguments, tmp_path) == [
        "--root", str(tmp_path), "--base", "main", "--explain", "--smoke-only",
    ]  # fmt: skip
