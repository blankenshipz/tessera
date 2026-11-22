from __future__ import annotations

import json
import sys
import urllib.error
from dataclasses import dataclass
from types import SimpleNamespace

from tessera_control import deploy_services as ds


def file_health(path, max_age=60):
    return SimpleNamespace(kind="file", path=str(path), max_age_seconds=max_age)


def http_health(url="http://127.0.0.1:1/h"):
    return SimpleNamespace(kind="http", url=url, timeout_seconds=3)


def svc(name, stop=True, health=None, wait=10):
    return SimpleNamespace(
        name=name,
        restart_argv=["restart", name],
        stop_argv=["stop", name] if stop else None,
        health=health or file_health("/nonexistent"),
        health_wait_seconds=wait,
        command_timeout_seconds=7,
    )


class Recorder:
    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return self.codes.get(tuple(argv), 0), "out"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def write_health(path, **over):
    doc = {"schema": ds.HEALTH_SCHEMA, "state": "running", "last_loop_at": 1000, "loops": 3}
    doc.update(over)
    path.write_text(json.dumps(doc))
    return path


def test_stop_reverse_order_skips_none_and_continues_on_failure():
    runner = Recorder({("stop", "b"): 1})
    results = ds.stop_services([svc("a"), svc("b"), svc("c", stop=False)], runner)
    assert [c[0] for c in runner.calls] == [["stop", "b"], ["stop", "a"]]
    assert [(r.step, r.name, r.ok) for r in results] == [("stop", "b", False), ("stop", "a", True)]
    assert runner.calls[0][1] == 7


def test_start_forward_order_and_health():
    runner = Recorder()
    results = ds.start_services(
        [svc("a"), svc("b")], runner, probe=lambda h: (True, "healthy"), sleep=lambda s: None
    )
    assert [c[0] for c in runner.calls] == [["restart", "a"], ["restart", "b"]]
    assert [(r.step, r.name, r.ok) for r in results] == [
        ("restart", "a", True),
        ("health", "a", True),
        ("restart", "b", True),
        ("health", "b", True),
    ]


def test_start_stops_at_first_failed_restart():
    runner = Recorder({("restart", "a"): 3})
    results = ds.start_services([svc("a"), svc("b")], runner, probe=lambda h: (True, "x"))
    assert len(runner.calls) == 1
    assert [(r.step, r.ok, r.detail) for r in results] == [("restart", False, "exit 3")]


def test_health_polls_until_healthy():
    clock = Clock()
    polls = []

    def probe(health):
        polls.append(clock())
        return len(polls) >= 3, "starting"

    results = ds.start_services([svc("a")], Recorder(), probe=probe, sleep=clock.sleep, clock=clock)
    assert polls == [0.0, 2.0, 4.0]
    assert results[-1].ok and results[-1].step == "health"


def test_health_timeout_ends_loop():
    clock = Clock()
    runner = Recorder()
    results = ds.start_services(
        [svc("a", wait=5), svc("b")],
        runner,
        probe=lambda h: (False, "stale"),
        sleep=clock.sleep,
        clock=clock,
    )
    assert len(runner.calls) == 1
    assert results[-1].step == "health" and not results[-1].ok
    assert "stale" in results[-1].detail


def test_probe_file_healthy(tmp_path):
    p = write_health(tmp_path / "h")
    assert ds.probe_health(file_health(p), now=lambda: 1030) == (True, "healthy")


def test_probe_file_stale(tmp_path):
    p = write_health(tmp_path / "h")
    ok, reason = ds.probe_health(file_health(p, 60), now=lambda: 2000)
    assert not ok and "stale" in reason


def test_probe_file_bad_schema_and_state(tmp_path):
    p = write_health(tmp_path / "h", schema="other")
    assert not ds.probe_health(file_health(p), now=lambda: 1000)[0]
    p = write_health(tmp_path / "h2", state="stopped")
    assert not ds.probe_health(file_health(p), now=lambda: 1000)[0]


def test_probe_file_missing_and_garbage_no_content(tmp_path):
    ok, reason = ds.probe_health(file_health(tmp_path / "nope"))
    assert not ok and "FileNotFoundError" in reason
    bad = tmp_path / "bad"
    bad.write_text("SECRET-CONTENT")
    ok, reason = ds.probe_health(file_health(bad))
    assert not ok and "SECRET" not in reason


def test_probe_file_oversized(tmp_path):
    p = tmp_path / "big"
    p.write_text(" " * (ds.MAX_HEALTH_FILE_BYTES + 10))
    ok, reason = ds.probe_health(file_health(p))
    assert not ok and "ValueError" in reason


def test_probe_file_symlink_and_directory_rejected(tmp_path):
    real = write_health(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert not ds.probe_health(file_health(link), now=lambda: 1000)[0]
    assert not ds.probe_health(file_health(tmp_path), now=lambda: 1000)[0]


@dataclass
class Resp:
    status: int

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_http_2xx_and_request_shape():
    opener = FakeOpener(Resp(204))
    assert ds.probe_health(http_health(), opener=opener) == (True, "healthy")
    request, timeout = opener.requests[0]
    assert request.get_method() == "GET" and timeout == 3
    assert not request.header_items()


def test_http_500_and_redirect_and_connection_error():
    err = urllib.error.HTTPError("u", 500, "x", {}, None)
    ok, reason = ds.probe_health(http_health(), opener=FakeOpener(err))
    assert (ok, reason) == (False, "http status 500")
    assert ds.probe_health(http_health(), opener=FakeOpener(Resp(302))) == (
        False,
        "http status 302",
    )
    ok, reason = ds.probe_health(http_health(), opener=FakeOpener(ConnectionRefusedError("x")))
    assert not ok and "ConnectionRefusedError" in reason


def test_default_opener_no_proxy_and_no_redirect(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:1")
    opener = ds._default_opener()
    # An empty ProxyHandler defines no *_open methods, so it is never registered.
    assert not any(hasattr(h, "http_open") and hasattr(h, "proxies") for h in opener.handlers)
    assert "http" not in getattr(opener, "handle_open", {}) or all(
        not hasattr(h, "proxies") for h in opener.handle_open["http"]
    )
    redirect = [h for h in opener.handlers if isinstance(h, ds._NoRedirect)]
    assert redirect and redirect[0].redirect_request(None, None, 302, "", {}, "http://x") is None
    assert not any(type(h).__name__ == "HTTPRedirectHandler" for h in opener.handlers)


def test_default_runner(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TESSERA_SECRET_PROBE", "leak")
    code, out = ds.default_runner(
        [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"], 20
    )
    assert code == 0 and "TESSERA_SECRET_PROBE" not in out and "PATH" in out
    code, out = ds.default_runner([sys.executable, "-c", "print('x'*5000)"], 20)
    assert code == 0 and len(out) == ds.MAX_OUTPUT_CHARS
    code, out = ds.default_runner([sys.executable, "-c", "import time;time.sleep(5)"], 1)
    assert (code, out) == (124, "timeout")
    code, out = ds.default_runner(["echo hi; id"], 5)  # no shell: a single missing program
    assert code == 127 and out == "FileNotFoundError"


def test_runner_exception_is_a_failed_step():
    def boom(argv, timeout):
        raise RuntimeError("secret detail")

    results = ds.stop_services([svc("a")], boom)
    assert not results[0].ok and "secret" not in results[0].detail
