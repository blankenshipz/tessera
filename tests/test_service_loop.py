"""Shared service loop: stop, once, error counting, health file and bounded logs."""

from __future__ import annotations

import io
import json
import os
import signal
import stat

from tessera_worker.service_loop import StopFlag, log_line, run_service, write_health


def test_once_runs_one_step_and_returns_zero(tmp_path):
    calls = []
    health = tmp_path / "health.json"
    code = run_service(
        step=lambda: calls.append(1) or [],
        interval=5,
        once=True,
        health_file=health,
        name="svc",
    )
    assert code == 0 and calls == [1]
    document = json.loads(health.read_text())
    assert document["loops"] == 1 and document["state"] == "stopped"


def test_a_signal_handler_call_stops_between_steps():
    stop = StopFlag()
    calls = []

    def step():
        calls.append(1)
        stop.request(signal.SIGTERM, None)
        return []

    assert (
        run_service(step=step, interval=30, once=False, health_file=None, name="s", stop=stop) == 0
    )
    assert calls == [1]


def test_install_is_idempotent_and_routes_signals():
    stop = StopFlag()
    previous = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    try:
        stop.install()
        stop.install()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert stop.requested
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def test_an_exception_is_counted_by_class_only_and_the_loop_continues(tmp_path, capsys):
    stop = StopFlag()
    health = tmp_path / "h.json"
    seen = []

    def step():
        seen.append(1)
        if len(seen) == 3:
            stop.request()
        raise ValueError("secret token abc")

    run_service(
        step=step,
        interval=0.01,
        once=False,
        health_file=health,
        name="svc",
        stop=stop,
        wait=lambda seconds: False,
    )
    document = json.loads(health.read_text())
    assert document["consecutive_errors"] == 3 and document["last_error_class"] == "ValueError"
    assert "secret" not in capsys.readouterr().out + health.read_text()


def test_health_is_private_atomic_and_has_exact_keys(tmp_path):
    health = tmp_path / "health.json"
    run_service(step=lambda: [], interval=1, once=True, health_file=health, name="svc")
    assert stat.S_IMODE(os.stat(health).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["health.json"]
    assert set(json.loads(health.read_text())) == {
        "schema",
        "name",
        "pid",
        "state",
        "started_at",
        "last_loop_at",
        "loops",
        "consecutive_errors",
        "last_error_class",
    }
    write_health(health, {"a": 1})
    assert json.loads(health.read_text()) == {"a": 1}


def test_logs_are_bounded_json_lines():
    stream = io.StringIO()
    log_line({"detail": "x" * 10000}, stream=stream, cap=200)
    line = stream.getvalue()
    assert len(line.strip()) <= 200 and json.loads(line)["truncated"] is True
    stream = io.StringIO()
    log_line({"a": 1}, stream=stream)
    assert json.loads(stream.getvalue()) == {"a": 1}


def test_stop_during_the_wait_exits_promptly():
    stop = StopFlag()
    slices = []

    def wait(seconds):
        slices.append(seconds)
        stop.request()
        return True

    run_service(
        step=lambda: [], interval=60, once=False, health_file=None, name="s", stop=stop, wait=wait
    )
    assert slices == [0.5]


def test_health_is_written_before_the_first_step(tmp_path):
    health = tmp_path / "health.json"
    seen = {}

    def step():
        seen["during"] = json.loads(health.read_text())
        return []

    run_service(step=step, interval=5, once=True, health_file=health, name="svc")
    assert seen["during"]["loops"] == 0 and seen["during"]["state"] == "running"
    assert json.loads(health.read_text())["state"] == "stopped"


def test_max_cycles_stops_after_n_steps_even_when_steps_fail():
    calls = []

    def step():
        calls.append(1)
        raise RuntimeError("boom")

    code = run_service(
        step=step,
        interval=0.01,
        once=False,
        health_file=None,
        name="s",
        max_cycles=3,
        wait=lambda seconds: False,
    )
    assert code == 0 and len(calls) == 3


def test_max_cycles_must_be_positive():
    import pytest

    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            run_service(
                step=lambda: [], interval=1, once=False, health_file=None, name="s", max_cycles=bad
            )
