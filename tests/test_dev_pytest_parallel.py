"""Tests for dev/pytest_parallel.py (the concurrent-shard full-suite runner)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "pytest_parallel", ROOT / "dev" / "pytest_parallel.py"
)
parallel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parallel)


class FakeProcess:
    """A shard process that has already finished with a scripted exit code and output."""

    def __init__(self, command, stdout, code, script):
        self.command, self.code = command, code
        stdout.write(script)
        stdout.flush()

    def wait(self):
        return self.code


@pytest.fixture
def launched(monkeypatch, tmp_path):
    """Run main() with fake shard processes; scripted per shard index."""
    monkeypatch.chdir(tmp_path)
    started: list[list[str]] = []
    script: dict[int, tuple[int, str]] = {}

    def popen(command, stdout, stderr):
        index = int(command[command.index("--index") + 1])
        started.append(command)
        code, text = script.get(index, (0, f"{index + 1} passed in 0.1s\n"))
        return FakeProcess(command, stdout, code, text)

    monkeypatch.setattr(parallel.subprocess, "Popen", popen)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["pytest_parallel.py", *argv])
        return parallel.main()

    return run, started, script, tmp_path


def test_every_shard_gets_its_index_count_basetemp_and_the_forwarded_arguments(launched):
    run, started, _, _ = launched
    assert run("--jobs", "3", "-k", "something") == 0
    assert len(started) == 3
    for index, command in enumerate(started):
        assert command[1] == "dev/pytest_shard.py"
        assert command[command.index("--index") + 1] == str(index)
        assert command[command.index("--count") + 1] == "3"
        assert command[-2:] == ["-k", "something"]
        assert "no:cacheprovider" in command
    # Disjoint pytest base directories, so concurrent shards never share temporary files.
    basetemps = [c[c.index("--basetemp") + 1] for c in started]
    assert len(set(basetemps)) == 3 and all(b.startswith(".pytest-tmp") for b in basetemps)


def test_it_exits_zero_only_if_every_shard_passes_and_names_the_failing_one(launched, capsys):
    run, _, script, _ = launched
    script[1] = (1, "FAILED tests/test_x.py::test_y\n1 failed, 4 passed in 1.0s\n")
    assert run("--jobs", "3") == 1
    out = capsys.readouterr().out
    assert "shard 0: exit 0: 1 passed in 0.1s" in out
    assert "shard 1: exit 1: 1 failed, 4 passed in 1.0s" in out
    assert "shard 2: exit 0:" in out


def test_a_shard_with_no_output_is_reported_and_a_nonzero_exit_still_fails(launched, capsys):
    run, _, script, _ = launched
    script[0] = (2, "")
    assert run("--jobs", "2") == 1
    assert "shard 0: exit 2: (no output)" in capsys.readouterr().out


def test_logs_go_to_a_log_directory_one_file_per_shard(launched, capsys):
    run, _, _, tmp_path = launched
    log_dir = tmp_path / "logs"
    assert run("--jobs", "2", "--log-dir", str(log_dir)) == 0
    assert sorted(p.name for p in log_dir.iterdir()) == ["shard0.log", "shard1.log"]
    assert f"logs: {log_dir}" in capsys.readouterr().out


def test_zero_or_negative_jobs_is_refused_before_anything_starts(launched):
    run, started, _, _ = launched
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit) as stop:
            run("--jobs", bad)
        assert stop.value.code == 2
    assert started == []
