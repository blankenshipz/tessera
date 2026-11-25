"""A run removes its own pytest basetemps when green, keeps them on failure, and nothing else."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

from tessera_control import focused_tests as ft
from tessera_control import scratch_cleanup as sc

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("pytest_parallel", ROOT / "dev/pytest_parallel.py")
parallel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parallel)
FILES = ["tests/test_a.py", "tests/test_b.py"]


def _populate(path: Path, *, read_only: bool = True) -> None:
    """What pytest leaves in a basetemp: nested directories, files, a symlink inside."""
    inner = path / "test_x0" / "deep"
    inner.mkdir(parents=True)
    (inner / "data.txt").write_text("x")
    (path / "test_x0" / "note").write_text("y")
    os.symlink("data.txt", inner / "link")
    if read_only:  # inputs a test made read-only: rmtree would fail half-way on these
        (inner / "data.txt").chmod(0o444)
        inner.chmod(0o555)
        (path / "test_x0").chmod(0o555)


class Popen:
    """Stands in for pytest: creates the basetemp it was given, then exits with a scripted code."""

    def __init__(self, codes=None):
        self.codes, self.calls, self.basetemps = codes or {}, [], []

    def __call__(self, command, cwd=None, stdout=None, stderr=None):
        index = len(self.calls)
        self.calls.append(command)
        basetemp = Path(command[command.index("--basetemp") + 1])
        basetemp = basetemp if basetemp.is_absolute() else Path(cwd or ".") / basetemp
        _populate(basetemp)
        self.basetemps.append(basetemp)
        if stdout is not None:
            stdout.write("1 passed in 0.1s\n")
        code = self.codes.get(index, 0)
        return type("P", (), {"wait": lambda self: code})()


@pytest.fixture(autouse=True)
def _no_keep_env(monkeypatch):
    monkeypatch.delenv(sc.KEEP_ENV, raising=False)


# -- the helper ------------------------------------------------------------------------------


def test_a_read_only_tree_is_removed_without_a_partial_failure(tmp_path):
    scratch = sc.Scratch(tmp_path)
    target = scratch.path("run")
    _populate(target)
    scratch.finish(green=True)
    assert not target.exists()
    assert not (tmp_path / ".pytest-tmp").exists()  # this run made it and it is empty


def test_only_this_runs_directories_go_and_a_sibling_run_is_untouched(tmp_path):
    mine, theirs = sc.Scratch(tmp_path, run_id="mine"), sc.Scratch(tmp_path, run_id="theirs")
    mine_dir, their_dir = mine.path("parallel0"), theirs.path("parallel0")
    _populate(mine_dir)
    _populate(their_dir, read_only=False)
    assert mine_dir != their_dir
    mine.finish(green=True)
    assert not mine_dir.exists() and their_dir.exists()
    assert (tmp_path / ".pytest-tmp").exists()  # not removed: the other run still lives there
    theirs.finish(green=True)
    assert not their_dir.exists()


def test_a_failing_run_keeps_its_scratch_and_prints_the_path(tmp_path):
    lines: list[str] = []
    scratch = sc.Scratch(tmp_path, out=lines.append)
    target = scratch.path("run")
    _populate(target)
    scratch.finish(green=False)
    assert target.exists()
    assert any(str(target) in line and "triage" in line for line in lines)


def test_keep_flag_and_env_keep_a_green_runs_scratch(tmp_path, monkeypatch):
    for keep in (sc.Scratch(tmp_path, keep=True), None):
        if keep is None:
            monkeypatch.setenv(sc.KEEP_ENV, "1")
            keep = sc.Scratch(tmp_path, keep=sc.keep_requested())
        target = keep.path("run")
        _populate(target, read_only=False)
        keep.finish(green=True)
        assert target.exists()
    assert sc.keep_requested(environ={sc.KEEP_ENV: "0"}) is False
    assert sc.keep_requested(environ={}) is False and sc.keep_requested(True, {}) is True


def test_a_symlink_to_outside_is_never_followed_or_removed_through(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    scratch = sc.Scratch(tmp_path)
    target = scratch.path("run")
    target.mkdir()
    os.symlink(outside, target / "escape")  # a link inside the tree pointing out
    scratch.finish(green=True)
    assert not target.exists()  # the link itself went
    assert (outside / "keep.txt").read_text() == "precious"  # what it pointed at did not
    # A basetemp that is itself a symlink is refused outright.
    (tmp_path / ".pytest-tmp").mkdir(exist_ok=True)  # the run above removed the one it made
    link = tmp_path / ".pytest-tmp" / "alias"
    os.symlink(outside, link)
    assert sc.remove_tree(link, tmp_path / ".pytest-tmp") is False
    assert link.is_symlink() and (outside / "keep.txt").exists()


def test_a_path_outside_the_scratch_directory_is_refused(tmp_path):
    scratch_root = tmp_path / ".pytest-tmp"
    scratch_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "f").write_text("x")
    assert sc.remove_tree(elsewhere, scratch_root) is False
    assert sc.remove_tree(scratch_root, scratch_root) is False  # not a strict descendant
    assert sc.remove_tree(scratch_root / ".." / "elsewhere", scratch_root) is False
    assert (elsewhere / "f").exists() and scratch_root.exists()
    assert sc.remove_listed([elsewhere], scratch_root) == [elsewhere]


def test_cleanup_never_raises_and_says_so(tmp_path, monkeypatch):
    lines: list[str] = []
    scratch = sc.Scratch(tmp_path, out=lines.append)
    scratch.path("run").mkdir()

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sc, "remove_tree", boom)
    scratch.finish(green=True)  # must not raise
    assert any("cleanup stopped early" in line for line in lines)


# -- the focused-test runner -----------------------------------------------------------------


def test_focused_run_removes_its_own_basetemp_when_green(tmp_path):
    popen = Popen()
    assert ft.run(FILES, tmp_path, 1, popen=popen) == 0
    assert not popen.basetemps[0].exists()
    assert not (tmp_path / ".pytest-tmp").exists()


def test_focused_run_keeps_it_on_failure_and_prints_the_path(tmp_path, capsys):
    popen = Popen(codes={0: 1})
    assert ft.run(FILES, tmp_path, 1, popen=popen) == 1
    assert popen.basetemps[0].exists()
    assert str(popen.basetemps[0]) in capsys.readouterr().out


def test_focused_parallel_run_removes_all_when_green_and_all_when_kept_on_failure(tmp_path, capsys):
    green = Popen()
    assert ft.run(FILES, tmp_path, 2, popen=green, log_dir=tmp_path / "logs") == 0
    assert green.basetemps and not any(path.exists() for path in green.basetemps)
    red = Popen(codes={0: 1})
    assert ft.run(FILES, tmp_path, 2, popen=red, log_dir=tmp_path / "logs2") == 1
    assert all(path.exists() for path in red.basetemps)  # one shard failed: keep for triage
    assert str(red.basetemps[0]) in capsys.readouterr().out


def test_focused_keep_basetemp_flag_and_env(tmp_path, monkeypatch):
    popen = Popen()
    assert ft.run(FILES, tmp_path, 1, popen=popen, keep_basetemp=True) == 0
    assert popen.basetemps[0].exists()
    monkeypatch.setenv(sc.KEEP_ENV, "1")
    again = Popen()
    assert ft.run(FILES, tmp_path, 1, popen=again) == 0
    assert again.basetemps[0].exists() and again.basetemps[0] != popen.basetemps[0]


def test_two_focused_runs_never_share_a_basetemp(tmp_path):
    first, second = Popen(), Popen()
    ft.run(FILES, tmp_path, 1, popen=first, keep_basetemp=True)
    ft.run(FILES, tmp_path, 1, popen=second)
    assert first.basetemps[0] != second.basetemps[0]
    assert first.basetemps[0].exists() and not second.basetemps[0].exists()


def test_focused_cli_accepts_keep_basetemp(tmp_path):
    parser_args = ["--root", str(tmp_path), "--keep-basetemp", "--list"]
    assert ft.main(parser_args, changed_files_fn=lambda *a: []) in {0, 2}


# -- the full-suite parallel runner ----------------------------------------------------------


@pytest.fixture
def parallel_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    popen = Popen()

    def fake_popen(command, stdout, stderr):
        return popen(command, stdout=stdout)

    monkeypatch.setattr(parallel.subprocess, "Popen", fake_popen)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["pytest_parallel.py", *argv])
        return parallel.main()

    return run, popen, tmp_path


def test_parallel_runner_removes_its_basetemps_when_every_shard_is_green(parallel_run):
    run, popen, tmp_path = parallel_run
    assert run("--jobs", "3", "--log-dir", str(tmp_path / "logs")) == 0
    assert len(popen.basetemps) == 3 and not any(p.exists() for p in popen.basetemps)
    assert not (tmp_path / ".pytest-tmp").exists()


def test_parallel_runner_keeps_everything_when_a_shard_fails(parallel_run, capsys):
    run, popen, tmp_path = parallel_run
    popen.codes = {1: 1}
    assert run("--jobs", "3", "--log-dir", str(tmp_path / "logs")) == 1
    assert all(p.exists() for p in popen.basetemps)
    assert str(popen.basetemps[1]) in capsys.readouterr().out


def test_parallel_runner_keep_flag_and_env(parallel_run, monkeypatch):
    run, popen, tmp_path = parallel_run
    assert run("--jobs", "2", "--keep-basetemp", "--log-dir", str(tmp_path / "l1")) == 0
    assert all(p.exists() for p in popen.basetemps)
    monkeypatch.setenv(sc.KEEP_ENV, "1")
    popen.basetemps.clear()
    assert run("--jobs", "2", "--log-dir", str(tmp_path / "l2")) == 0
    assert all(p.exists() for p in popen.basetemps)


def test_a_concurrent_runs_basetemp_survives_a_green_run(parallel_run):
    run, popen, tmp_path = parallel_run
    other = tmp_path / ".pytest-tmp" / "parallel0-otherrun"
    _populate(other, read_only=False)
    assert run("--jobs", "2", "--log-dir", str(tmp_path / "logs")) == 0
    assert other.exists()  # not this run's: never touched
    assert (tmp_path / ".pytest-tmp").exists()
    st = other.stat().st_mode
    assert stat.S_ISDIR(st)
