"""In-image guard logic with fake subprocess/signals/clocks; no real workload starts."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tessera_worker import ordinary_docker_guard as module


def test_guard_never_runs_as_host_helper_or_root(monkeypatch):
    monkeypatch.setattr(module.os, "getpid", lambda: 100)
    assert module.run({}) == 126
    monkeypatch.setattr(module.os, "getpid", lambda: 1)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    assert module.run({}) == 126


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(module.os, "getpid", lambda: 1)
    monkeypatch.setattr(module.os, "geteuid", lambda: 65532)
    start = datetime(2026, 10, 5, 12, tzinfo=UTC)
    now, monotonic, handlers, killed, calls = [start], [0.0], {}, [], []
    monkeypatch.setattr(
        module,
        "datetime",
        type("Clock", (), {"now": lambda *_: now[0], "fromisoformat": datetime.fromisoformat}),
    )
    monkeypatch.setattr(module.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(
        module.signal, "signal", lambda kind, handler: handlers.setdefault(kind, handler)
    )
    monkeypatch.setattr(module.os, "kill", lambda pid, kind: killed.append((pid, kind)))

    class Process:
        pid, returncode = 20, 0

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    process = Process()

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)

    def sleep(_):
        handlers[module.signal.SIGTERM](None, None)

    monkeypatch.setattr(module.time, "sleep", sleep)
    config = {
        "version": "ordinary-docker-guard-v1",
        "operation_id": "a" * 32,
        "argv": ["approved", "arg"],
        "hard_expires_at": (start + timedelta(seconds=10)).isoformat(),
    }
    return config, tmp_path / "result.json", process, now, monotonic, handlers, killed, calls


def test_guard_holds_completed_output_until_capture_then_exits_actual_code(setup):
    config, path, process, _, _, _, killed, calls = setup
    assert module.run(config, result_path=path) == 0
    assert json.loads(path.read_bytes()) == {"version": 1, "operation_id": "a" * 32, "exit_code": 0}
    assert killed[0][0] == -1  # namespace-wide descendant stop before ready marker
    assert calls[0][0] == ["approved", "arg"]
    assert calls[0][1]["close_fds"] is True
    assert calls[0][1]["cwd"] == "/outputs"
    assert "shell" not in calls[0][1]
    assert set(calls[0][1]["env"]) == {"PATH", "HOME", "TMPDIR"}


def test_nonzero_workload_exit_never_publishes_success_marker(setup):
    config, path, process, *_ = setup
    process.returncode = 7
    assert module.run(config, result_path=path) == 7
    assert not path.exists()


@pytest.mark.parametrize("boundary", ["wall", "monotonic", "cancel"])
def test_independent_guard_stops_on_hard_deadline_or_stop(setup, monkeypatch, boundary):
    config, path, process, now, monotonic, handlers, killed, calls = setup
    process.returncode = None

    def sleep(_):
        if boundary == "wall":
            now[0] += timedelta(seconds=11)
        elif boundary == "monotonic":
            monotonic[0] += 11
            now[0] -= timedelta(seconds=1)  # wall rollback cannot widen the timer
        else:
            handlers[module.signal.SIGTERM](None, None)

    monkeypatch.setattr(module.time, "sleep", sleep)
    assert module.run(config, result_path=path) == (143 if boundary == "cancel" else 124)
    assert killed and not path.exists()


def test_expired_guard_never_starts_child(setup):
    config, path, _, now, _, _, _, calls = setup
    config["hard_expires_at"] = now[0].isoformat()
    assert module.run(config, result_path=path) == 124
    assert calls == []
