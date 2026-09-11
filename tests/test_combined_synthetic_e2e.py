"""Unit coverage for bounded combined-fixture subprocess handling."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest


def _runner_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    import check_combined_synthetic_e2e

    return check_combined_synthetic_e2e


def test_combined_runner_starts_command_in_new_process_group(monkeypatch):
    runner = _runner_module(monkeypatch)
    calls = []

    class Process:
        pid = 123
        returncode = 0

        def communicate(self, *, timeout):
            calls.append(("communicate", timeout))

    process = Process()

    def popen(*args, **kwargs):
        calls.append(("popen", args, kwargs))
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    runner._run_command("fixture", ["fixture"], {"PATH": "test"})

    assert calls == [
        (
            "popen",
            (["fixture"],),
            {"cwd": runner.ROOT, "env": {"PATH": "test"}, "start_new_session": True},
        ),
        ("communicate", runner.COMMAND_TIMEOUT_SECONDS),
    ]


def test_combined_runner_timeout_attempts_process_group_cleanup(monkeypatch):
    runner = _runner_module(monkeypatch)
    signals = []

    class Process:
        pid = 123
        returncode = None
        calls = 0

        def communicate(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["fixture"], timeout)

    process = Process()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(RuntimeError, match="does not confirm child or container termination"):
        runner._run_command("fixture", ["fixture"], {"PATH": "test"})

    assert signals == [(process.pid, signal.SIGTERM)]
