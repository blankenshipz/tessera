"""ADR 0111 wiring of the stage watcher into the Salad executor path: fakes only, no live Salad.

Covers the in-process watch channel (same refusal mapping as the HTTP route), the live-check
per-poll snapshot observer (off when its config key is absent) and the executor's optional
``stage_watcher`` block (on by default, ADR 0125; descriptive reports only, never a route effect).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_campaign_salad_executor import RECEIPT, Harness, item
from test_salad_campaign_profiles import OPERATION
from test_salad_live_check import LocalSession, drive

from tessera_control import campaign_salad_executor as executor_module
from tessera_control import salad_live_check as live
from tessera_control.campaign_salad_executor import ExecutorConfig, StageWatcherBlock
from tessera_control.salad_watch_signals import signals_from_poll
from tessera_control.stage_watch_channel import (
    InProcessWatchChannel,
    SaladRouteWatch,
    build_salad_route_watch,
)
from tessera_control.stage_watch_limits import MAX_REPORTS_PER_OPERATION
from tessera_worker.control_channel import ControlChannelError

OPERATION_HEX = OPERATION  # 32 lowercase hex, the executor suite's operation id
WATCH_MODULES = ("tessera_worker.stage_watch_runner", "tessera_worker.stage_watcher")


# -- in-process channel -----------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    from test_stage_watch_reports import Harness as WatchHarness

    from tessera_control.stage_watch import StageWatchStore
    from tessera_control.store import Store

    watch = WatchHarness(tmp_path)
    principal = Store(watch.state).authenticate_worker("worker-token")
    channel = InProcessWatchChannel(StageWatchStore(watch.state), principal)
    return watch, channel


def _path(op):
    return f"/worker/operations/{op}/watch-reports"


def _outcome(call):
    try:
        return ("ok", call())
    except ControlChannelError as error:
        return ("refused", error.status, error.refusal_reason)


def _http(watch, body, key, op=None):
    response = watch.post(body, key=key, op=op)
    if response.status_code < 300:
        return ("ok", response.json())
    reason = response.json().get("reason") if response.status_code != 422 else None
    return ("refused", response.status_code, reason)


def test_in_process_channel_records_like_the_route_and_replays_across_paths(world):
    watch, channel = world
    body = watch.body(kind="stuck")
    reply = channel.post(_path(watch.operation_id), body, "k1", timeout_seconds=2.0)
    assert reply == {
        "recorded": True,
        "seq": 1,
        "kind": "stuck",
        "operation_id": watch.operation_id,
    }
    assert len(watch.events()) == 1
    # Same digest as the HTTP route: an exact replay over HTTP returns the stored response.
    assert _http(watch, body, "k1") == ("ok", reply)
    assert channel.post(_path(watch.operation_id), body, "k1") == reply
    assert len(watch.events()) == 1


def test_in_process_refusals_match_the_route_status_and_reason(world):
    watch, channel = world
    assert channel.post(_path(watch.operation_id), watch.body(seq=2, kind="stuck"), "k0")
    cases = [
        (watch.body(seq=3, kind="stuck", lease_id="other-lease"), "k2", None),
        (watch.body(seq=3, kind="stuck", expected_epoch=watch.lease["epoch"] + 1), "k3", None),
        (watch.body(seq=1, kind="stuck", at=1), "k4", None),  # out of order
        (watch.body(seq=3, kind="stuck", at=-3600), "k5", None),  # before the lease
        (watch.body(seq=3, kind="stuck", at=86400), "k6", None),  # too early (transient)
        (watch.body(seq=3, kind="stuck", at=1), "k0", None),  # key reused: key_conflict
        (watch.body(seq=3, kind="stuck"), "k7", "f" * 32),  # not this worker's operation
        (watch.body(seq=3, kind="nope"), "k8", None),  # schema: 422, no reason
    ]
    reasons = []
    for body, key, op in cases:
        target = op or watch.operation_id
        in_process = _outcome(lambda b=body, k=key, t=target: channel.post(_path(t), b, k))
        assert in_process[0] == "refused"
        assert in_process == _http(watch, body, key, op), (key, in_process)
        reasons.append(in_process[2])
    assert reasons == [
        "lease_mismatch",
        "stale_epoch",
        "out_of_order",
        "before_lease",
        "too_early",
        "key_conflict",
        "unknown_operation",
        None,
    ]
    assert len(watch.events()) == 1


def test_in_process_refusals_settle_exactly_as_over_http(world):
    from tessera_worker.stage_watcher.report import classify_status

    watch, channel = world
    permanent = _outcome(
        lambda: channel.post(_path(watch.operation_id), watch.body(lease_id="x"), "k1")
    )
    transient = _outcome(
        lambda: channel.post(_path(watch.operation_id), watch.body(at=86400), "k2")
    )
    assert classify_status(permanent[1], "progress", permanent[2]).outcome == "refused"
    assert classify_status(transient[1], "failed", transient[2]).outcome == "transient"


def test_in_process_channel_accepts_only_the_watch_route_and_a_bounded_key(world):
    watch, channel = world
    body = watch.body()
    for path in (
        "/worker/operations/" + watch.operation_id + "/observations",
        "/worker/operations/../watch-reports",
        "/worker/operations/" + watch.operation_id.upper() + "/watch-reports",
        "https://control.example/worker/operations/x/watch-reports",
    ):
        with pytest.raises(ValueError):
            channel.post(path, body, "k1")
    for key in ("", "  ", "k" * 129):
        assert _outcome(lambda k=key: channel.post(_path(watch.operation_id), body, k)) == (
            "refused",
            400,
            None,
        )
    with pytest.raises(ValueError):
        channel.post(_path(watch.operation_id), {"x": float("nan")}, "k1")
    assert watch.events() == []
    with pytest.raises(ValueError):
        InProcessWatchChannel(object(), {"id": "w"})


def test_runner_reports_over_the_in_process_channel(world, tmp_path):
    from tessera_worker.stage_watch_config import StageWatchConfig
    from tessera_worker.stage_watch_runner import (
        RemoteOperationInfo,
        StageWatchRunner,
        register_probe_factory,
        snapshot_file_probe_factory,
        unregister_probe_factory,
    )
    from tessera_worker.stage_watcher.signals import write_signal_snapshot

    watch, channel = world
    register_probe_factory("salad", snapshot_file_probe_factory())
    try:
        (tmp_path / "exec-state").mkdir(mode=0o700)
        runner = StageWatchRunner(
            StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "kill")),
            state_dir=tmp_path / "exec-state",
            channel=channel,
            docker_run=lambda *a, **k: None,
        )
        path = runner.signals_path(watch.operation_id)
        runner.on_start_remote(
            RemoteOperationInfo(
                watch.operation_id,
                watch.lease["id"],
                watch.lease["epoch"],
                "gpu-stage",
                "salad",
                600,
            )
        )
        now = datetime.now(UTC)
        write_signal_snapshot(
            path,
            signals_from_poll(provider_status="running", poll_count=1, now=now),
            clock=lambda: now,
        )
        runner.tick()
    finally:
        unregister_probe_factory("salad")
    (event,) = watch.events()
    assert event["kind"] == "heartbeat" and event["operational_only"] is True
    assert runner.errors == 0


# -- live-check per-poll observer -------------------------------------------------------------


def _private_dir(tmp_path):
    directory = tmp_path.resolve() / "signals"
    directory.mkdir(mode=0o700)
    return directory


def _drive_with(session, path, *, max_polls=2):
    return live._drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=10,
        max_polls=max_polls,
        sleep=lambda _: None,
        clock=lambda: 0,
        watch_signals_file=path,
    )


def test_observer_writes_a_valid_private_snapshot_per_successful_poll(tmp_path, monkeypatch):
    from tessera_worker.stage_watcher.signals import validate_signals

    path = _private_dir(tmp_path) / f"{OPERATION_HEX}.signals.json"
    seen = []
    original = live._observe_watch

    def spy(target, *, state, evidence, polls):
        original(target, state=state, evidence=evidence, polls=polls)
        seen.append((polls, json.loads(Path(target).read_text())))

    monkeypatch.setattr(live, "_observe_watch", spy)
    session = LocalSession(prior=True)
    session.record["provider_status"] = "running"
    session.proof = dict(session.proof, output_retained=False, stop_confirmed=False)
    observed = {"n": 0}
    real_evidence = session.evidence

    def evidence(route):
        observed["n"] += 1
        if observed["n"] >= 3:
            return dict(real_evidence(route), output_retained=True, stop_confirmed=True)
        return real_evidence(route)

    session.evidence = evidence
    receipt = _drive_with(session, path, max_polls=5)
    assert receipt["status"] == "passed"
    markers = [polls for polls, _ in seen]
    assert markers == list(range(1, len(seen) + 1)) and len(seen) >= 3
    for polls, snapshot in seen:
        assert validate_signals(snapshot) == snapshot
        assert snapshot["progress_marker"] == polls
        assert set(snapshot) == {"schema", "observed_at", "state", "progress_marker"}
    assert seen[0][1]["state"] == "running"
    assert seen[-1][1]["state"] == "exited"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # Nothing identifying reaches the file.
    text = path.read_text()
    assert "exact-route" not in text and "synthetic-group" not in text


def test_absent_key_means_no_file_and_identical_behaviour(tmp_path, monkeypatch):
    directory = _private_dir(tmp_path)
    baseline_session = LocalSession()
    baseline = drive(baseline_session)
    unset_session = LocalSession()
    unset = _drive_with(unset_session, None)
    watched_session = LocalSession()
    watched = _drive_with(watched_session, directory / "x.signals.json")
    assert json.dumps(baseline, sort_keys=True) == json.dumps(unset, sort_keys=True)
    assert json.dumps(baseline, sort_keys=True) == json.dumps(watched, sort_keys=True)
    assert baseline_session.actions == unset_session.actions == watched_session.actions
    assert sorted(p.name for p in directory.iterdir()) == ["x.signals.json"]
    calls = []
    monkeypatch.setattr(
        "tessera_worker.stage_watcher.signals.write_signal_snapshot",
        lambda *a, **k: calls.append(a),
    )
    _drive_with(LocalSession(), None)
    drive(LocalSession())
    assert calls == []


@pytest.mark.parametrize("target", ["write", "map", "state"])
def test_observer_exceptions_are_swallowed(tmp_path, monkeypatch, target):
    baseline = drive(LocalSession())
    if target == "write":
        monkeypatch.setattr(
            "tessera_worker.stage_watcher.signals.write_signal_snapshot",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk SECRET")),
        )
        path = _private_dir(tmp_path) / "x.signals.json"
    elif target == "map":
        monkeypatch.setattr(
            "tessera_control.salad_watch_signals.signals_from_poll",
            lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        path = _private_dir(tmp_path) / "x.signals.json"
    else:
        path = tmp_path.resolve() / "missing-dir" / "x.signals.json"
    session = LocalSession()
    assert _drive_with(session, path) == baseline
    live._observe_watch(path, state=None, evidence=None, polls=1)  # never raises


def test_config_accepts_the_optional_key_and_checks_its_directory(tmp_path, monkeypatch):
    from test_salad_live_check import concrete_session

    session, _h, path = concrete_session(tmp_path, monkeypatch)
    session.close()
    document = json.loads(path.read_text())
    assert "watch_signals_file" not in document
    value, _policy, _digest = live._config(path)
    assert "watch_signals_file" not in value

    def rewrite(extra):
        path.write_text(json.dumps(document | extra))
        path.chmod(0o600)

    directory = _private_dir(tmp_path)
    rewrite({"watch_signals_file": str(directory / f"{OPERATION_HEX}.signals.json")})
    assert live._config(path)[0]["watch_signals_file"].endswith(".signals.json")
    loose = tmp_path.resolve() / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    for bad in (
        "relative/x.signals.json",
        str(loose / "x.signals.json"),
        str(directory / "missing" / "x.signals.json"),
        str(directory / "x.signals.txt"),
        str(directory / ".hidden.json"),
        str(directory / ".." / "signals" / "x.json"),
        7,
        None,
    ):
        rewrite({"watch_signals_file": bad})
        with pytest.raises((ValueError, OSError)):
            live._config(path)
    rewrite({"watch_signals_file": str(directory / "x.json"), "other": 1})
    with pytest.raises(ValueError, match="closed server config"):
        live._config(path)


def test_run_passes_the_configured_snapshot_path_to_the_driver(tmp_path, monkeypatch):
    from test_salad_live_check import concrete_session

    session, _h, path = concrete_session(tmp_path, monkeypatch)
    captured = {}

    def fake_drive(session, **kwargs):
        captured.update(kwargs)
        return {"schema": "tessera.salad.live-check.v1", "status": "inconclusive"}

    monkeypatch.setattr(live, "_drive", fake_drive)
    receipts = tmp_path.resolve() / "receipts"
    receipts.mkdir(mode=0o700)
    live.run(
        session,
        route_id=session.config["route_id"],
        source_revision=session.config["source_revision"],
        receipt_directory=receipts,
    )
    assert captured["watch_signals_file"] is None
    session.config = dict(session.config, watch_signals_file="/x/y.json")
    receipts2 = tmp_path.resolve() / "receipts2"
    receipts2.mkdir(mode=0o700)
    live.run(
        session,
        route_id=session.config["route_id"],
        source_revision=session.config["source_revision"],
        receipt_directory=receipts2,
    )
    assert captured["watch_signals_file"] == "/x/y.json"
    session.close()


# -- executor ---------------------------------------------------------------------------------


class RecordingChannel:
    def __init__(self):
        self.posts = []
        self.lock = threading.Lock()

    def post(self, path, body, key, *, timeout_seconds=None):
        with self.lock:
            self.posts.append((path, body["kind"], key, threading.current_thread().name))
        return {"recorded": True, "seq": body["seq"], "kind": body["kind"]}

    def kinds(self):
        with self.lock:
            return [kind for _path, kind, _key, _thread in self.posts]


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 11, 22, tzinfo=UTC)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += timedelta(seconds=seconds)


class GatedRunner:
    """The real runner; ticks from the ticker thread wait for the fake child's go-ahead."""

    def __init__(self, runner):
        self._runner = runner
        self.gate = threading.Semaphore(0)
        self.ticked = threading.Semaphore(0)
        self.ticker_ticks = 0

    def __getattr__(self, name):
        return getattr(self._runner, name)

    def tick(self):
        if threading.current_thread().name == "salad-stage-watch":
            if not self.gate.acquire(timeout=0.05):
                return
            self.ticker_ticks += 1
            self._runner.tick()
            self.ticked.release()
            return
        self._runner.tick()


def _watched_executor(tmp_path, *, kill_switch=True, silent=60, steps=None, runner_error=None):
    from tessera_worker.stage_watch_config import StageWatchConfig
    from tessera_worker.stage_watch_runner import (
        StageWatchRunner,
        register_probe_factory,
        snapshot_file_probe_factory,
    )
    from tessera_worker.stage_watcher.signals import write_signal_snapshot

    clock = FakeClock()
    channel = RecordingChannel()
    register_probe_factory("salad", snapshot_file_probe_factory())
    state = tmp_path / "exec-state"
    state.mkdir(mode=0o700)
    runner = GatedRunner(
        StageWatchRunner(
            StageWatchConfig(
                enabled=True,
                kill_switch_path=str(tmp_path / "kill") if kill_switch else None,
                silent_seconds=silent,
                heartbeat_seconds=30,
            ),
            state_dir=state,
            channel=channel,
            docker_run=lambda *a, **k: None,
            clock=clock,
        )
    )
    watch = SaladRouteWatch(runner, poll_seconds=0.01)
    seen = {}

    def child(config_path, receipt_dir, timeout):
        document = json.loads(Path(config_path).read_text())
        seen["document"] = document
        seen["ticker_alive"] = watch._thread is not None and watch._thread.is_alive()
        target = document.get("watch_signals_file")
        for advance, marker in steps or ():
            clock.advance(advance)
            now = clock()
            if target is not None:
                write_signal_snapshot(
                    target,
                    signals_from_poll(provider_status="running", poll_count=marker, now=now),
                    clock=lambda now=now: now,
                )
            runner.gate.release()
            assert runner.ticked.acquire(timeout=5)
        if runner_error is not None:
            raise runner_error
        return dict(RECEIPT)

    h = Harness(tmp_path / "h", runner=child)
    h.executor.stage_watch = watch
    h.control.items = [item("route_started", stage_key="gpu-stage", operation_id=OPERATION_HEX)]
    return h, watch, runner, channel, seen


@pytest.fixture
def salad_probe():
    from tessera_worker.stage_watch_runner import unregister_probe_factory

    yield
    unregister_probe_factory("salad")


def _plain_outcome(tmp_path):
    h = Harness(tmp_path / "plain")
    h.control.items = [item("route_started", operation_id=OPERATION_HEX)]
    actions = h.executor.run_once()
    return actions, h.names(), h.control.calls[-1]


def test_executor_watch_reports_heartbeat_progress_and_stuck_while_the_child_runs(
    tmp_path, salad_probe
):
    steps = [(0, 1), (40, 2), (40, 2), (50, 2)]
    h, watch, runner, channel, seen = _watched_executor(tmp_path, steps=steps)
    actions = h.executor.run_once()
    assert channel.kinds() == ["heartbeat", "progress", "heartbeat", "stuck"]
    assert {thread for *_, thread in channel.posts} == {"salad-stage-watch"}
    assert runner.ticker_ticks == len(steps)
    # The child saw a live ticker and a private snapshot path in its private config.
    assert seen["ticker_alive"] is True
    signals = seen["document"]["watch_signals_file"]
    assert signals == str(runner.signals_path(OPERATION_HEX))
    assert live._watch_signals_path(signals) == Path(signals)
    # Ticker stopped and joined, watch ended and forgotten.
    assert not watch._thread.is_alive()
    assert runner.active_operations == () and watch.errors == 0
    # The route result is exactly the unwatched one.
    plain_actions, plain_names, plain_outcome = _plain_outcome(tmp_path)
    assert [a["action"] for a in actions] == [a["action"] for a in plain_actions]
    assert h.names() == plain_names
    assert h.control.calls[-1] == plain_outcome
    assert not (tmp_path / "h" / "work" / "route_1" / "live-config.json").exists()


def test_a_route_timeout_still_stops_the_ticker_and_ends_the_watch(tmp_path, salad_probe):
    h, watch, runner, channel, _seen = _watched_executor(
        tmp_path, steps=[(0, 1)], runner_error=executor_module.RouteTimeout()
    )
    assert [a["action"] for a in h.executor.run_once()] == ["route_timeout"]
    assert not watch._thread.is_alive() and runner.active_operations == ()
    assert channel.kinds() == ["heartbeat"]


def test_no_kill_switch_means_no_watcher_and_no_snapshot_path(tmp_path, salad_probe):
    h, watch, runner, channel, seen = _watched_executor(tmp_path, kill_switch=False)
    h.executor.run_once()
    assert "watch_signals_file" not in seen["document"]
    assert watch._thread is None and channel.posts == []
    assert runner.refused_no_kill_switch == 1


class ExplodingRunner:
    active_operations = (OPERATION_HEX,)

    def __init__(self, tmp_path):
        self.path = tmp_path / "s.signals.json"

    def signals_path(self, operation_id):
        return self.path

    def on_start_remote(self, info):
        pass

    def tick(self):
        raise RuntimeError("tick")

    def on_end(self, operation_id):
        raise RuntimeError("end")

    def close(self):
        raise RuntimeError("close")


class ExplodingWatch:
    def begin(self, **kwargs):
        raise RuntimeError("begin")


def test_watcher_exceptions_never_change_the_route_result(tmp_path):
    plain_actions, plain_names, plain_outcome = _plain_outcome(tmp_path)
    for index, watch in enumerate(
        (SaladRouteWatch(ExplodingRunner(tmp_path), 0.001), ExplodingWatch())
    ):
        h = Harness(tmp_path / f"w{index}")
        h.executor.stage_watch = watch
        h.control.items = [item("route_started", stage_key="gpu-stage", operation_id=OPERATION_HEX)]
        actions = h.executor.run_once()
        assert [a["action"] for a in actions] == [a["action"] for a in plain_actions]
        assert h.names() == plain_names and h.control.calls[-1] == plain_outcome


def test_an_unjoinable_ticker_disables_the_watch_for_this_process(tmp_path, monkeypatch):
    from tessera_control import stage_watch_channel

    release = threading.Event()

    class SlowRunner(ExplodingRunner):
        def tick(self):
            release.wait(5)

        def on_end(self, operation_id):
            raise AssertionError("runner touched while the ticker may still run")

    monkeypatch.setattr(stage_watch_channel, "TICKER_JOIN_SECONDS", 0.05)
    watch = SaladRouteWatch(SlowRunner(tmp_path), 0.001)
    with watch.ticking():
        threading.Event().wait(0.05)
    watch.end(OPERATION_HEX)
    assert (
        watch.begin(
            operation_id=OPERATION_HEX, lease_id="l", epoch=1, stage_key="s", deadline_seconds=9
        )
        is None
    )
    release.set()
    watch._thread.join(5)


def test_disabled_executor_never_imports_the_watcher(tmp_path, monkeypatch):
    for name in list(sys.modules):
        if name.startswith(WATCH_MODULES) or name == "tessera_control.stage_watch_channel":
            monkeypatch.delitem(sys.modules, name)
    h = Harness(tmp_path)
    assert h.config.stage_watcher is None and h.executor.stage_watch is None
    h.control.items = [item("route_started")]
    assert "outcome_recorded" in [a["action"] for a in h.executor.run_once()]
    assert "watch_signals_file" not in h.runs[0]["document"]
    loaded = [name for name in sys.modules if name.startswith(WATCH_MODULES)]
    assert loaded == [] and "tessera_control.stage_watch_channel" not in sys.modules


def _build(tmp_path, monkeypatch, block):
    """build_executor with the same fakes as the executor suite's composition test."""
    h = Harness(tmp_path)

    class FakeProfiles:
        def get(self, name):
            return h.executor.profile

    class FakeStore:
        def __init__(self, state_dir):
            pass

        def authenticate_worker(self, token):
            return {"id": "principal_w", "org_id": "org_1"}

    import tessera_control.store as store

    monkeypatch.setattr(store, "Store", FakeStore)
    monkeypatch.setattr(live, "_private_bytes", lambda path, cap: b"token")
    monkeypatch.setattr(executor_module, "load_profiles", lambda path: FakeProfiles())
    monkeypatch.setattr(executor_module, "trusted_importer", lambda *a: None)
    monkeypatch.setattr(executor_module, "StoreControl", lambda *a: object())
    from tessera_control import stage_watch_channel

    class FakeWatchStore:
        def __init__(self, state_dir):
            self.state_dir = state_dir

    monkeypatch.setattr(stage_watch_channel, "StageWatchStore", FakeWatchStore)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    update = {"state_dir": str(state), "stage_watcher": block}
    return executor_module.build_executor(h.config.model_copy(update=update))


def test_build_executor_wires_the_watch_by_default_and_only_explicit_off_disables_it(
    tmp_path, monkeypatch, salad_probe
):
    for name in list(sys.modules):
        if name.startswith(WATCH_MODULES):
            monkeypatch.delitem(sys.modules, name)
    off = StageWatcherBlock(enabled=False, kill_switch_path="/run/kill")
    assert _build(tmp_path / "b", monkeypatch, off).stage_watch is None
    assert (
        _build(tmp_path / "b2", monkeypatch, StageWatcherBlock(enabled=False)).stage_watch is None
    )
    assert [name for name in sys.modules if name.startswith(WATCH_MODULES)] == []
    for name, block in (("a", None), ("c", StageWatcherBlock(enabled=True, max_reports=50))):
        built = _build(tmp_path / name, monkeypatch, block).stage_watch
        assert isinstance(built, SaladRouteWatch)
        marker = tmp_path / name / "state" / executor_module.DEFAULT_KILL_SWITCH_NAME
        assert built.runner._config.kill_switch_path == str(marker)
        assert not marker.exists()  # never auto-created
    on = StageWatcherBlock(enabled=True, kill_switch_path=str(tmp_path / "kill"), max_reports=50)
    built = _build(tmp_path / "d", monkeypatch, on).stage_watch
    assert isinstance(built, SaladRouteWatch)
    assert built.runner.directory == tmp_path / "d" / "state" / "stage-watch"
    assert built.poll_seconds == 2.0


def test_default_marker_is_in_the_state_dir_and_an_unusable_one_means_no_watcher(tmp_path, capsys):
    effective = executor_module.effective_stage_watcher
    block = effective(None, tmp_path)
    assert block.enabled and block.kill_switch_path == str(tmp_path / "stage-watch.kill")
    assert effective(StageWatcherBlock(enabled=False), tmp_path) is None
    explicit = StageWatcherBlock(enabled=True, kill_switch_path="/run/k")
    assert effective(explicit, tmp_path) is explicit
    assert effective(None, "relative/state") is None  # unusable: bounded log, never raises
    assert "stage watcher default unusable" in capsys.readouterr().err


def test_build_salad_route_watch_never_raises(tmp_path, salad_probe):
    block = StageWatcherBlock(enabled=True, kill_switch_path="/run/kill")
    assert build_salad_route_watch(None, state_dir=tmp_path, worker={}, poll_seconds=2) is None
    assert build_salad_route_watch(block, state_dir=tmp_path, worker={}, poll_seconds=2) is None


def test_the_stage_watcher_block_is_closed_and_bounded(tmp_path):
    base = Harness(tmp_path).config.model_dump(by_alias=True)
    good = dict(enabled=True, kill_switch_path="/run/tessera/kill", silent_seconds=600)
    config = ExecutorConfig.model_validate(dict(base, stage_watcher=good))
    assert config.stage_watcher.watch_config_fields() == dict(good)
    assert ExecutorConfig.model_validate(base).stage_watcher is None
    assert ExecutorConfig.model_validate(dict(base, stage_watcher={"enabled": True}))
    assert ExecutorConfig.model_validate(dict(base, stage_watcher={"enabled": False}))
    for bad in (
        dict(good, extra=1),
        dict(good, enabled="true"),
        dict(good, kill_switch_path="relative/kill"),
        dict(good, silent_seconds=0),
        dict(good, heartbeat_seconds=7 * 24 * 3600 + 1),
        dict(good, max_reports=1),
        dict(good, max_reports=MAX_REPORTS_PER_OPERATION + 1),
        dict(good, delivery_timeout_seconds=1.0),
    ):
        with pytest.raises(ValueError):
            ExecutorConfig.model_validate(dict(base, stage_watcher=bad))


def test_a_block_without_enabled_is_a_tuning_only_block_and_stays_enabled(tmp_path):
    base = Harness(tmp_path).config.model_dump(by_alias=True)
    config = ExecutorConfig.model_validate(dict(base, stage_watcher={"silent_seconds": 600}))
    assert config.stage_watcher.enabled is True
    assert config.stage_watcher.silent_seconds == 600
    effective = executor_module.effective_stage_watcher(config.stage_watcher, tmp_path)
    assert effective.enabled and effective.silent_seconds == 600
    assert effective.kill_switch_path == str(tmp_path / executor_module.DEFAULT_KILL_SWITCH_NAME)
    explicit = ExecutorConfig.model_validate(
        dict(base, stage_watcher={"kill_switch_path": "/run/k"})
    )
    assert explicit.stage_watcher.enabled is True
    off = ExecutorConfig.model_validate(
        dict(base, stage_watcher={"enabled": False, "silent_seconds": 600})
    )
    assert executor_module.effective_stage_watcher(off.stage_watcher, tmp_path) is None
    absent = ExecutorConfig.model_validate(base)
    assert absent.stage_watcher is None
    assert executor_module.effective_stage_watcher(None, tmp_path).enabled


def test_load_config_accepts_the_block_and_refuses_a_bad_one(tmp_path):
    tmp_path.chmod(0o700)
    for name in ("state", "cred", "work"):
        (tmp_path / name).mkdir(mode=0o700)
    for name in ("w", "c", "t", "ca", "profiles"):
        (tmp_path / name).write_text("x")
        (tmp_path / name).chmod(0o600)
    from test_campaign_salad_executor import custody

    document = {
        "schema": executor_module.CONFIG_SCHEMA,
        "state_dir": str(tmp_path / "state"),
        "profiles_file": str(tmp_path / "profiles"),
        "profile": "gpu-main",
        "worker_credential_file": str(tmp_path / "w"),
        "credential_directory": str(tmp_path / "cred"),
        "guardian_socket": "/run/guardian.sock",
        "guardian_uid": 1000,
        "custody": dict(custody(), token_file=str(tmp_path / "t"), ca_file=str(tmp_path / "ca")),
        "work_directory": str(tmp_path / "work"),
        "source_revision": "a" * 40,
        "poll_seconds": 5,
    }
    path = tmp_path / "config.json"

    def load(value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return executor_module.load_config(path)

    assert load(document).stage_watcher is None
    block = {"enabled": True, "kill_switch_path": "/run/tessera/kill"}
    assert load(dict(document, stage_watcher=block)).stage_watcher.enabled is True
    with pytest.raises(ValueError):
        load(dict(document, stage_watcher=dict(block, max_reports=0)))
