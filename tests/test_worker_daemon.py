from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time

import pytest

from tessera_worker import daemon
from tessera_worker.daemon import DaemonConfig, DaemonError, Supervisor
from tessera_worker.exit_codes import EXIT_CREDENTIAL_REJECTED

SENTINEL = "SENTINEL-secret-/private/path/token-9f3"
ARGS = [
    "development-poll",
    "--credential-file",
    f"/private/{SENTINEL}",
    "--control-url",
    "https://x.invalid",
]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeChild:
    def __init__(
        self, clock: Clock, runtime: float, code: int, *, ignore_term: bool = False
    ) -> None:
        self.clock = clock
        self.pid = 4242
        self.exit_at = clock.now + runtime
        self.code = code
        self.ignore_term = ignore_term
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        if self.killed:
            return -signal.SIGKILL
        if self.clock.now >= self.exit_at:
            return self.code
        return None

    def signal_group(self, number: int) -> None:
        self.signals.append(number)
        if number == signal.SIGKILL:
            self.killed = True
        elif number == signal.SIGTERM and not self.ignore_term:
            self.killed = False
            self.exit_at = self.clock.now
            self.code = -signal.SIGTERM


class Harness:
    """Scripted children (runtime, code); stops after the script is exhausted."""

    def __init__(self, script, config=None, jitter=0.0, **child_kwargs) -> None:
        self.clock = Clock()
        self.script = list(script)
        self.children: list[FakeChild] = []
        self.start_times: list[float] = []
        self.statuses: list[dict] = []
        self.logs: list[str] = []
        self.child_kwargs = child_kwargs
        self.supervisor = Supervisor(
            ARGS,
            config or DaemonConfig(),
            spawn=self.spawn,
            clock=self.clock,
            wall=self.clock,
            sleep=self.sleep,
            jitter=lambda: jitter,
            install_signals=lambda handler: None,
            write_status=self.statuses.append,
            log=self.logs.append,
        )

    def spawn(self, argv):
        assert argv[1:3] == ["-m", "tessera_worker.cli"]
        if not self.script:
            self.supervisor.request_stop()
            raise OSError("script exhausted")
        runtime, code = self.script.pop(0)
        child = FakeChild(self.clock, runtime, code, **self.child_kwargs)
        self.children.append(child)
        self.start_times.append(self.clock.now)
        return child

    def sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)

    def gaps(self) -> list[float]:
        return [b - a for a, b in zip(self.start_times, self.start_times[1:], strict=False)]


def test_backoff_sequence_and_cap() -> None:
    config = DaemonConfig(backoff_min=2, backoff_max=120, stable_seconds=60)
    harness = Harness([(0, 1)] * 9, config)
    harness.supervisor.run()
    assert [round(gap) for gap in harness.gaps()] == [2, 4, 8, 16, 32, 64, 120, 120]
    assert [daemon.backoff_delay(n, 2, 120, 0.0) for n in (1, 2, 7, 50)] == [2, 4, 120, 120]
    assert daemon.backoff_delay(1, 2, 120, 1.0) == 2.5
    assert daemon.backoff_delay(10, 2, 120, 1.0) == 120


def test_backoff_resets_after_stable_run() -> None:
    config = DaemonConfig(backoff_min=2, backoff_max=120, stable_seconds=60)
    harness = Harness([(0, 1), (0, 1), (0, 1), (61, 1), (0, 1), (0, 1)], config)
    harness.supervisor.run()
    gaps = harness.gaps()
    assert [round(gap) for gap in gaps[:2]] == [2, 4]
    assert round(gaps[3] - 61) == 2  # stable run: delay back to the minimum
    assert round(gaps[4]) == 4  # the failure after the reset counts as the second


def test_credential_rejection_uses_auth_backoff() -> None:
    config = DaemonConfig(auth_backoff_min=60, auth_backoff_max=900, backoff_min=2)
    harness = Harness([(0, 3)] * 6, config)
    harness.supervisor.run()
    gaps = harness.gaps()
    assert min(gaps) >= 60
    assert [round(gap) for gap in gaps] == [60, 120, 240, 480, 900]
    states = {s["state"] for s in harness.statuses}
    assert "credential_backoff" in states
    credential = [s for s in harness.statuses if s["state"] == "credential_backoff"][0]
    assert credential["last_exit_kind"] == "credential_rejected"
    assert credential["next_start"] is not None


def test_exit_zero_restarts_after_restart_delay_only() -> None:
    config = DaemonConfig(restart_delay=1.0, backoff_min=2)
    harness = Harness([(5, 0), (5, 0), (5, 0)], config)
    harness.supervisor.run()
    assert [round(gap, 3) for gap in harness.gaps()] == [6.0, 6.0]
    assert all(s["consecutive_failures"] == 0 for s in harness.statuses)


def test_exit_kinds() -> None:
    assert daemon.classify_exit(0) == "ok"
    assert daemon.classify_exit(3) == "credential_rejected"
    assert daemon.classify_exit(2) == "terminal"
    assert daemon.classify_exit(1) == "failure"
    assert daemon.classify_exit(-9) == "signal"
    harness = Harness([(0, 2), (0, -9), (0, 1)])
    harness.supervisor.run()
    kinds = [s["last_exit_kind"] for s in harness.statuses if s["state"] == "backoff"]
    assert kinds == ["terminal", "signal", "failure"]


def test_hundred_failures_never_crash_loop() -> None:
    config = DaemonConfig(backoff_min=2, backoff_max=120)
    harness = Harness([(0, 1)] * 100, config, jitter=0.0)
    harness.supervisor.run()
    assert len(harness.start_times) == 100
    assert min(harness.gaps()) >= 2
    # also with immediate-exit children alternating failure kinds and full jitter
    mixed = Harness([(0.1, code) for code in [1, 3, 2, -9] * 25], config, jitter=1.0)
    mixed.supervisor.run()
    assert min(mixed.gaps()) >= 2


def test_sigterm_terms_then_kills_after_grace_and_stops_spawning() -> None:
    config = DaemonConfig(grace_seconds=30)
    harness = Harness([(1000, 0), (1000, 0)], config, ignore_term=True)
    original = harness.sleep
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            harness.supervisor.request_stop()
        original(seconds)

    harness.supervisor._sleep = sleep
    assert harness.supervisor.run() == 0
    child = harness.children[0]
    assert child.signals == [signal.SIGTERM, signal.SIGKILL]
    assert len(harness.children) == 1  # nothing respawned
    assert harness.statuses[-1]["state"] == "stopped"
    assert harness.statuses[-1]["child_pid"] is None
    assert harness.clock.now - 1000 >= 30  # waited the grace period


def test_sigterm_during_backoff_stops_without_signalling() -> None:
    harness = Harness([(0, 1), (0, 1)], DaemonConfig())
    original = harness.sleep
    harness.supervisor._sleep = lambda s: (harness.supervisor.request_stop(), original(s))[1]
    assert harness.supervisor.run() == 0
    assert len(harness.children) == 1
    assert harness.children[0].signals == []
    assert harness.statuses[-1]["state"] == "stopped"


def test_graceful_child_exits_on_term_before_grace() -> None:
    harness = Harness([(1000, 0)], DaemonConfig(grace_seconds=30))
    original = harness.sleep
    harness.supervisor._sleep = lambda s: (harness.supervisor.request_stop(), original(s))[1]
    harness.supervisor.run()
    assert harness.children[0].signals[0] == signal.SIGTERM
    assert harness.clock.now - 1000 < 5


def test_unknown_subcommand_refused_with_64(tmp_path, capsys) -> None:
    code = daemon.main(["run", "--state-dir", str(tmp_path / "s"), "--", "serve", "--x"])
    assert code == 64
    assert "unsupported worker subcommand" in capsys.readouterr().err
    assert daemon.main(["run", "--state-dir", str(tmp_path / "s")]) == 64
    with pytest.raises(DaemonError) as raised:
        Supervisor(["rm", "-rf"], DaemonConfig())
    assert raised.value.code == 64
    assert {"development-run", "development-poll"} <= daemon.ALLOWED_SUBCOMMANDS


def test_lock_refuses_second_instance(tmp_path) -> None:
    state = daemon.prepare_state_dir(tmp_path / "state")
    first = daemon.acquire_lock(state)
    try:
        with pytest.raises(DaemonError) as raised:
            daemon.acquire_lock(state)
        assert raised.value.code == 75
        assert daemon.main(["run", "--state-dir", str(state), "--", "development-poll"]) == 75
    finally:
        os.close(first)
    os.close(daemon.acquire_lock(state))  # released lock can be taken again


def test_state_dir_permissions_and_symlink(tmp_path) -> None:
    created = daemon.prepare_state_dir(tmp_path / "new")
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    with pytest.raises(DaemonError, match="0700"):
        daemon.prepare_state_dir(loose)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(DaemonError, match="symlink"):
        daemon.prepare_state_dir(link)
    plain = tmp_path / "file"
    plain.write_text("x")
    with pytest.raises(DaemonError, match="not a directory"):
        daemon.prepare_state_dir(plain)


def test_status_file_is_private_atomic_and_leaks_nothing(tmp_path) -> None:
    state = daemon.prepare_state_dir(tmp_path / "state")
    replaced: list[str] = []
    real_replace = os.replace

    def spy(source, target):
        replaced.append(os.path.basename(source))
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        return real_replace(source, target)

    clock = Clock()
    supervisor = Supervisor(
        ARGS,
        DaemonConfig(),
        spawn=lambda argv: FakeChild(clock, 0, 1),
        clock=clock,
        wall=clock,
        sleep=lambda s: (clock.sleep(s), supervisor.request_stop())[0],
        install_signals=lambda handler: None,
        write_status=lambda document: daemon.write_status_file(state, document),
        log=lambda line: logs.append(line),
    )
    logs: list[str] = []
    real = os.replace
    os.replace = spy
    try:
        supervisor.run()
    finally:
        os.replace = real
    assert replaced and set(replaced) == {"status.json.tmp"}
    path = state / "status.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (state / "status.json.tmp").exists()
    text = path.read_text() + "".join(logs)
    for forbidden in (SENTINEL, "/private", "x.invalid", "--credential-file", "-m"):
        assert forbidden not in text
    document = json.loads(path.read_text())
    assert document["subcommand"] == "development-poll"
    assert document["state"] == "stopped"
    assert {
        "pid",
        "state",
        "child_pid",
        "started_at",
        "restarts",
        "consecutive_failures",
        "last_exit_code",
        "last_exit_kind",
        "last_start",
        "next_start",
        "subcommand",
        "stable",
    } <= set(document)
    assert all(len(line) <= 512 for line in logs)


def test_status_heartbeat_while_waiting() -> None:
    config = DaemonConfig(backoff_min=100, backoff_max=100, heartbeat_seconds=10)
    harness = Harness([(0, 1), (0, 1)], config)
    harness.supervisor.run()
    first_backoff = [s for s in harness.statuses if s["state"] == "backoff"]
    assert len(first_backoff) >= 9  # initial write plus roughly one per 10 s of 100 s


def test_status_freshness_logic(tmp_path, capsys) -> None:
    now = 5000.0
    base = {"state": "running", "updated_at": now - 10}
    assert daemon.evaluate_status(base, now, 120)[0] is True
    assert daemon.evaluate_status({**base, "updated_at": now - 500}, now, 120)[0] is False
    for state in ("stopped", "failed"):
        assert daemon.evaluate_status({**base, "state": state}, now, 120)[0] is False
    assert daemon.evaluate_status({"state": "running"}, now, 120)[0] is False
    assert daemon.evaluate_status(None, now, 120)[0] is False
    assert daemon.evaluate_status({**base, "state": "credential_backoff"}, now, 120)[0] is True

    state = daemon.prepare_state_dir(tmp_path / "state")
    assert daemon.main(["status", "--state-dir", str(state)]) == 1
    daemon.write_status_file(state, {"state": "running", "updated_at": time.time()})
    assert daemon.main(["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["fresh"] is True
    daemon.write_status_file(state, {"state": "stopped", "updated_at": time.time()})
    assert daemon.main(["status", "--state-dir", str(state)]) == 1
    daemon.write_status_file(state, {"state": "running", "updated_at": time.time() - 1000})
    assert daemon.main(["status", "--state-dir", str(state), "--max-age", "5"]) == 1
    # Fresh but waiting out a rejected credential: alive, yet degraded (exit 3, not 0).
    daemon.write_status_file(state, {"state": "credential_backoff", "updated_at": time.time()})
    assert daemon.main(["status", "--state-dir", str(state)]) == EXIT_CREDENTIAL_REJECTED
    # A stale credential_backoff file is unhealthy like any other stale state.
    daemon.write_status_file(
        state, {"state": "credential_backoff", "updated_at": time.time() - 1000}
    )
    assert daemon.main(["status", "--state-dir", str(state), "--max-age", "5"]) == 1


def test_invalid_numbers_refused() -> None:
    assert (
        daemon.main(
            [
                "run",
                "--state-dir",
                "/nonexistent-x",
                "--backoff-min",
                "5",
                "--backoff-max",
                "1",
                "--",
                "development-run",
            ]
        )
        == 64
    )
    assert daemon.main(["run", "--bogus"]) == 64


def test_real_processes_exit_codes_and_process_groups() -> None:
    scripts = [
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        [sys.executable, "-c", "pass"],
        [sys.executable, "-c", "import sys; sys.exit(1)"],
    ]
    spawned: list[daemon.Child] = []
    groups: list[int] = []
    statuses: list[dict] = []

    def spawn(argv):
        assert argv[:3] == [sys.executable, "-m", "tessera_worker.cli"]
        if not scripts:
            supervisor.request_stop()
            raise OSError("done")
        child = daemon.ProcessChild(scripts.pop(0))
        groups.append(os.getpgid(child.pid) - child.pid)  # 0 when it leads its own group
        spawned.append(child)
        return child

    config = DaemonConfig(
        backoff_min=0.05,
        backoff_max=0.05,
        auth_backoff_min=0.05,
        auth_backoff_max=0.05,
        restart_delay=0.05,
        poll_interval=0.02,
    )
    supervisor = Supervisor(
        ARGS,
        config,
        spawn=spawn,
        install_signals=lambda h: None,
        write_status=statuses.append,
        log=lambda line: None,
    )
    assert supervisor.run() == 0
    assert groups == [0, 0, 0]
    assert os.getpgid(0) != spawned[0].pid
    exits = [
        (s["last_exit_code"], s["last_exit_kind"])
        for s in statuses
        if s["state"] in ("backoff", "credential_backoff")
    ]
    assert exits == [(3, "credential_rejected"), (0, "ok"), (1, "failure")]
    assert statuses[-1]["state"] == "stopped"


def test_real_process_group_is_killed_on_stop() -> None:
    code = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "subprocess.Popen([sys.executable, '-c', 'import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    holder: list[daemon.ProcessChild] = []
    statuses: list[dict] = []
    sleeps = {"n": 0}

    def spawn(argv):
        child = daemon.ProcessChild([sys.executable, "-c", code])
        holder.append(child)
        return child

    def sleep(seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 10:
            supervisor.request_stop()
        time.sleep(seconds)

    supervisor = Supervisor(
        ARGS,
        DaemonConfig(grace_seconds=0.5, poll_interval=0.05),
        spawn=spawn,
        sleep=sleep,
        install_signals=lambda h: None,
        write_status=statuses.append,
        log=lambda line: None,
    )
    assert supervisor.run() == 0
    child = holder[0]
    assert child.poll() == -signal.SIGKILL
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(child.pid, 0)  # grandchild in the group is gone too
    assert statuses[-1]["state"] == "stopped"
