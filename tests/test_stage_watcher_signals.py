from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tessera_worker.stage_watcher import (
    COUNTER_KEYS,
    MAX_SNAPSHOT_BYTES,
    SIGNALS_SCHEMA,
    KillSwitch,
    ProbeSample,
    SignalProbe,
    SnapshotFileSignals,
    StageContract,
    StageWatcher,
    WatchOutbox,
    validate_signals,
    write_signal_snapshot,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
OPERATION_ID = "0123456789abcdef0123456789abcdef"


class FakeClock:
    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def snapshot(**overrides) -> dict:
    value = {
        "schema": SIGNALS_SCHEMA,
        "observed_at": T0.isoformat(),
        "state": "running",
        "progress_marker": 3,
    }
    value.update(overrides)
    return value


def put(path: Path, value: object, mode: int = 0o600) -> Path:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def reader(path: Path, clock: FakeClock | None = None, max_age: float = 60):
    return SnapshotFileSignals(path, max_age_seconds=max_age, clock=clock or FakeClock())


# -- reader ----------------------------------------------------------------------------------


def test_reader_accepts_valid_snapshot(tmp_path):
    full = snapshot(
        exit_code=-9,
        output_bytes=1,
        cpu_ms=2,
        mem_bytes=3,
        restarts=0,
        oom_killed=False,
        observed_at="2026-01-01T13:00:00+01:00",
    )
    path = put(tmp_path / "signals.json", full)
    assert reader(path).read() == full


def test_reader_missing_file_is_none(tmp_path):
    assert reader(tmp_path / "absent.json").read() is None


def test_reader_rejects_symlink(tmp_path):
    target = put(tmp_path / "real.json", snapshot())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert reader(target).read() is not None
    assert reader(link).read() is None


def test_reader_rejects_directory_and_fifo(tmp_path):
    directory = tmp_path / "dir.json"
    directory.mkdir(mode=0o600)
    assert reader(directory).read() is None
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    assert reader(fifo).read() is None


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o400, 0o700])
def test_reader_rejects_mode_other_than_0600(tmp_path, mode):
    path = put(tmp_path / "signals.json", snapshot(), mode=mode)
    assert reader(path).read() is None


def test_reader_rejects_other_owner(tmp_path, monkeypatch):
    path = put(tmp_path / "signals.json", snapshot())
    assert reader(path).read() is not None
    real_geteuid = os.geteuid
    monkeypatch.setattr(os, "geteuid", lambda: real_geteuid() + 1)
    assert reader(path).read() is None


def test_reader_rejects_other_owner_via_fstat(tmp_path, monkeypatch):
    path = put(tmp_path / "signals.json", snapshot())
    real_fstat = os.fstat

    def fstat(fd):
        info = real_fstat(fd)
        fields = list(info)
        fields[stat.ST_UID] = info.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)
    assert reader(path).read() is None


def test_reader_rejects_hard_link(tmp_path):
    path = put(tmp_path / "signals.json", snapshot())
    os.link(path, tmp_path / "other.json")
    assert reader(path).read() is None


def test_reader_rejects_oversize_and_empty(tmp_path):
    big = put(tmp_path / "big.json", b" " * MAX_SNAPSHOT_BYTES + json.dumps(snapshot()).encode())
    assert reader(big).read() is None
    empty = put(tmp_path / "empty.json", b"")
    assert reader(empty).read() is None


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'"running"',
        b'{"schema": "tessera.stage-watch.signals.v1", "schema": "x"}',
        json.dumps(snapshot()).replace("3", "NaN").encode(),
    ],
)
def test_reader_rejects_bad_json(tmp_path, raw):
    path = put(tmp_path / "signals.json", raw)
    assert reader(path).read() is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"detail": "free text"},
        {"schema": "tessera.stage-watch.signals.v2"},
        {"state": "stuck"},
        {"state": 1},
        {"progress_marker": True},
        {"output_bytes": False},
        {"exit_code": True},
        {"cpu_ms": -1},
        {"mem_bytes": 1.5},
        {"restarts": "1"},
        {"progress_marker": None},
        {"exit_code": 256},
        {"exit_code": -256},
        {"oom_killed": 0},
        {"oom_killed": "true"},
        {"observed_at": "2026-01-01T12:00:00"},
        {"observed_at": 1767268800},
        {"observed_at": "yesterday"},
    ],
)
def test_reader_rejects_schema_violations(tmp_path, overrides):
    path = put(tmp_path / "signals.json", snapshot(**overrides))
    assert reader(path).read() is None


@pytest.mark.parametrize("missing", ["schema", "observed_at", "state"])
def test_reader_rejects_missing_required_key(tmp_path, missing):
    value = snapshot()
    del value[missing]
    assert reader(put(tmp_path / "signals.json", value)).read() is None


def test_reader_future_time(tmp_path):
    clock = FakeClock()
    path = tmp_path / "signals.json"
    put(path, snapshot(observed_at=(T0 + timedelta(seconds=5)).isoformat()))
    assert reader(path, clock).read() is not None
    put(path, snapshot(observed_at=(T0 + timedelta(seconds=6)).isoformat()))
    assert reader(path, clock).read() is None


def test_reader_stale_snapshot_is_none(tmp_path):
    clock = FakeClock()
    path = put(tmp_path / "signals.json", snapshot())
    signals = reader(path, clock, max_age=30)
    clock.advance(30)
    assert signals.read() is not None
    clock.advance(1)
    assert signals.read() is None


def test_reader_bad_clock_is_none(tmp_path):
    path = put(tmp_path / "signals.json", snapshot())

    def boom() -> datetime:
        raise RuntimeError("clock")

    assert SnapshotFileSignals(path, max_age_seconds=60, clock=boom).read() is None
    naive = SnapshotFileSignals(path, max_age_seconds=60, clock=lambda: datetime(2026, 1, 1))
    assert naive.read() is None


@pytest.mark.parametrize("bad", ["relative.json", "/tmp/../x.json"])
def test_reader_requires_absolute_path(bad):
    with pytest.raises(ValueError):
        SnapshotFileSignals(bad, max_age_seconds=60, clock=FakeClock())


@pytest.mark.parametrize("bad", [0, -1, True, "60"])
def test_reader_requires_positive_max_age(tmp_path, bad):
    with pytest.raises(ValueError):
        SnapshotFileSignals(tmp_path / "x", max_age_seconds=bad, clock=FakeClock())


# -- writer ----------------------------------------------------------------------------------


def test_writer_round_trip_is_private_and_leaves_no_temp(tmp_path):
    clock = FakeClock()
    path = tmp_path / "signals.json"
    written = write_signal_snapshot(path, {"state": "running", "progress_marker": 1}, clock=clock)
    assert written == {
        "schema": SIGNALS_SCHEMA,
        "observed_at": T0.isoformat(),
        "state": "running",
        "progress_marker": 1,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert reader(path, clock).read() == written
    # Overwrite in place: still private, still one file.
    clock.advance(10)
    second = write_signal_snapshot(
        path, {"state": "exited", "exit_code": 0, "progress_marker": 2}, clock=clock
    )
    assert reader(path, clock).read() == second
    assert sorted(p.name for p in tmp_path.iterdir()) == ["signals.json"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writer_keeps_given_schema_and_time(tmp_path):
    clock = FakeClock()
    value = snapshot(observed_at="2026-01-01T11:59:00+00:00")
    assert write_signal_snapshot(tmp_path / "s.json", value, clock=clock) == value


@pytest.mark.parametrize(
    "signals",
    [
        {},
        {"state": "stuck"},
        {"state": "running", "extra": 1},
        {"state": "running", "progress_marker": True},
        {"state": "running", "cpu_ms": -1},
        {"state": "running", "schema": "other"},
        {"state": "running", "observed_at": "2026-01-01T12:00:00"},
        {"state": "running", "observed_at": "2026-01-01T12:00:06+00:00"},
        "running",
    ],
)
def test_writer_refuses_invalid_payload(tmp_path, signals):
    path = tmp_path / "signals.json"
    with pytest.raises(ValueError):
        write_signal_snapshot(path, signals, clock=FakeClock())
    assert list(tmp_path.iterdir()) == []


def test_writer_refuses_oversize(tmp_path, monkeypatch):
    import tessera_worker.stage_watcher.signals as signals_module

    monkeypatch.setattr(signals_module, "MAX_SNAPSHOT_BYTES", 40)
    with pytest.raises(ValueError):
        write_signal_snapshot(tmp_path / "s.json", {"state": "running"}, clock=FakeClock())
    assert list(tmp_path.iterdir()) == []


def test_writer_refuses_missing_parent(tmp_path):
    with pytest.raises(ValueError):
        write_signal_snapshot(tmp_path / "nope" / "s.json", {"state": "running"}, clock=FakeClock())
    (tmp_path / "file").write_text("x")
    with pytest.raises(ValueError):
        write_signal_snapshot(tmp_path / "file" / "s.json", {"state": "running"}, clock=FakeClock())


def test_writer_requires_absolute_path():
    with pytest.raises(ValueError):
        write_signal_snapshot("s.json", {"state": "running"}, clock=FakeClock())


def test_validate_signals_returns_copy():
    value = snapshot()
    result = validate_signals(value)
    assert result == value and result is not value


# -- probe -----------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["running", "exited", "absent", "unknown"])
def test_signal_probe_maps_state(state):
    sample = SignalProbe(lambda: snapshot(state=state)).sample(T0)
    assert sample.state == state
    assert sample.observed_at == T0


def test_signal_probe_maps_counters():
    probe = SignalProbe(
        lambda: snapshot(
            state="exited",
            exit_code=137,
            oom_killed=True,
            output_bytes=1,
            cpu_ms=2,
            mem_bytes=3,
            restarts=4,
            progress_marker=5,
        )
    )
    assert probe.sample(T0) == ProbeSample(
        "exited",
        T0,
        exit_code=137,
        oom_killed=True,
        output_bytes=1,
        cpu_ms=2,
        mem_bytes=3,
        restarts=4,
        progress_marker=5,
    )


def raising() -> dict:
    raise OSError("unreadable")


@pytest.mark.parametrize(
    "read",
    [
        lambda: None,
        raising,
        lambda: {"state": "running"},
        lambda: snapshot(extra=1),
        lambda: snapshot(progress_marker=True),
        lambda: "running",
        lambda: [("state", "running")],
    ],
)
def test_signal_probe_unknown_on_none_malformed_or_raise(read):
    assert SignalProbe(read).sample(T0) == ProbeSample("unknown", T0)


def test_signal_probe_from_file(tmp_path):
    clock = FakeClock()
    path = tmp_path / "signals.json"
    probe = SignalProbe.from_file(path, 30, clock)
    assert probe.sample(T0).state == "unknown"  # no file yet
    write_signal_snapshot(path, {"state": "running", "progress_marker": 7}, clock=clock)
    sample = probe.sample(T0)
    assert (sample.state, sample.progress_marker) == ("running", 7)
    clock.advance(31)
    assert probe.sample(clock()).state == "unknown"  # stale


def test_signal_probe_holds_no_credential_or_network_surface():
    probe = SignalProbe(lambda: None)
    public = {name for name in dir(probe) if not name.startswith("_")}
    assert public == {"from_file", "sample"}


# -- progress marker -------------------------------------------------------------------------


def test_progress_marker_is_not_a_report_counter():
    assert "progress_marker" not in COUNTER_KEYS


def test_progress_marker_alone_is_a_progress_signal():
    assert ProbeSample("running", T0, progress_marker=0).has_progress_signal
    assert not ProbeSample("running", T0).has_progress_signal
    with pytest.raises(ValueError):
        ProbeSample("running", T0, progress_marker=-1)
    with pytest.raises(ValueError):
        ProbeSample("running", T0, progress_marker=True)


class ScriptedSignals:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.marker = 0

    def __call__(self) -> dict:
        return {
            "schema": SIGNALS_SCHEMA,
            "observed_at": self.clock().isoformat(),
            "state": "running",
            "progress_marker": self.marker,
        }


class Sink:
    def __init__(self):
        self.delivered = []

    def deliver(self, report) -> bool:
        self.delivered.append(report)
        return True


def make_watcher(tmp_path, clock, read, silent_seconds=120):
    contract = StageContract(
        operation_id=OPERATION_ID,
        lease_id="lease-1",
        epoch=1,
        stage_key="train",
        provider="salad",
        deadline_seconds=36000,
        silent_seconds=silent_seconds,
        heartbeat_seconds=60,
        max_reports=200,
        kill_switch_path=None,
    )
    sink = Sink()
    watcher = StageWatcher(
        contract,
        SignalProbe(read),
        sink,
        KillSwitch(None),
        clock,
        WatchOutbox(tmp_path / "watch.sqlite"),
        sleep=lambda _: None,
    )
    return watcher, sink


def run(watcher, clock, signals, ticks, step=30, advance_marker=False) -> list[str]:
    kinds = []
    for _ in range(ticks):
        if advance_marker:
            signals.marker += 1
        kinds += [report.kind for report in watcher.tick()]
        clock.advance(step)
    return kinds


def test_advancing_marker_never_reads_stuck(tmp_path):
    clock = FakeClock()
    signals = ScriptedSignals(clock)
    watcher, sink = make_watcher(tmp_path, clock, signals)
    kinds = run(watcher, clock, signals, 40, advance_marker=True)
    assert "stuck" not in kinds
    assert "progress" in kinds
    assert all("progress_marker" not in r.counters for r in sink.delivered)


def test_flat_marker_reports_exactly_one_stuck(tmp_path):
    clock = FakeClock()
    signals = ScriptedSignals(clock)
    watcher, sink = make_watcher(tmp_path, clock, signals, silent_seconds=120)
    kinds = run(watcher, clock, signals, 20)  # 0..570 s with the marker flat
    assert kinds.count("stuck") == 1
    assert kinds[:3] == ["heartbeat", "heartbeat", "stuck"]
    [stuck] = [r for r in sink.delivered if r.kind == "stuck"]
    assert stuck.counters["silent_s"] == 120
    assert not watcher.done


@pytest.mark.parametrize("change", [lambda m: m + 1, lambda m: 0])
def test_marker_change_or_reset_counts_as_progress_and_rearms(tmp_path, change):
    clock = FakeClock()
    signals = ScriptedSignals(clock)
    signals.marker = 50
    watcher, _ = make_watcher(tmp_path, clock, signals, silent_seconds=120)
    assert run(watcher, clock, signals, 5).count("stuck") == 1
    signals.marker = change(signals.marker)
    assert [r.kind for r in watcher.tick()] == ["progress"]
    clock.advance(30)
    kinds = run(watcher, clock, signals, 6)  # flat again: a new episode
    assert kinds.count("stuck") == 1


def test_stale_snapshot_reads_unknown_then_failed(tmp_path):
    clock = FakeClock()
    path = tmp_path / "signals.json"
    write_signal_snapshot(path, {"state": "running", "progress_marker": 1}, clock=clock)
    watcher, sink = make_watcher(
        tmp_path, clock, SnapshotFileSignals(path, max_age_seconds=45, clock=clock).read
    )
    kinds = []
    for _ in range(5):
        kinds += [report.kind for report in watcher.tick()]
        clock.advance(30)
    # Fresh at 0 and 30 s; stale from 60 s: three unknown ticks end the watch as failed.
    assert kinds == ["heartbeat", "failed"]
    assert watcher.outcome == "failed"


def test_aware_non_utc_offsets_compare_correctly(tmp_path):
    clock = FakeClock()
    path = put(
        tmp_path / "signals.json",
        snapshot(observed_at=T0.astimezone(timezone(timedelta(hours=-7))).isoformat()),
    )
    assert reader(path, clock).read() is not None


# -- remote providers: an allocating group and an exit with no code (Salad-shaped signals) ---


def test_unknown_with_a_live_marker_is_not_a_lost_probe(tmp_path):
    """While a remote group allocates the snapshot says unknown, but the driver keeps polling."""
    clock = FakeClock()
    marker = {"n": 0}

    def read():
        marker["n"] += 1
        return {
            "schema": SIGNALS_SCHEMA,
            "observed_at": clock().isoformat(),
            "state": "unknown",
            "progress_marker": marker["n"],
        }

    watcher, sink = make_watcher(tmp_path, clock, read)
    kinds = run(watcher, clock, None, 20)
    assert "failed" not in kinds and not watcher.done
    assert all(r.kind != "failed" for r in sink.delivered)


def test_unknown_with_a_flat_marker_still_goes_stuck_not_failed(tmp_path):
    clock = FakeClock()

    def read():
        return {
            "schema": SIGNALS_SCHEMA,
            "observed_at": clock().isoformat(),
            "state": "unknown",
            "progress_marker": 7,
        }

    watcher, _ = make_watcher(tmp_path, clock, read, silent_seconds=120)
    kinds = run(watcher, clock, None, 12)
    assert kinds.count("stuck") == 1 and "failed" not in kinds


def test_missing_snapshot_is_still_a_lost_probe(tmp_path):
    clock = FakeClock()
    watcher, _ = make_watcher(tmp_path, clock, lambda: None)
    kinds = run(watcher, clock, None, 5)
    assert kinds == ["failed"] and watcher.done


def test_exit_without_a_code_ends_the_watch_without_a_failed_report(tmp_path):
    clock = FakeClock()
    state = {"s": "running", "n": 0}

    def read():
        state["n"] += 1
        out = {
            "schema": SIGNALS_SCHEMA,
            "observed_at": clock().isoformat(),
            "state": state["s"],
            "progress_marker": state["n"],
        }
        return out

    watcher, sink = make_watcher(tmp_path, clock, read)
    run(watcher, clock, None, 3)
    state["s"] = "exited"  # a remote route reports no exit code
    kinds = [r.kind for r in watcher.tick()]
    assert kinds == [] and watcher.done and watcher.outcome == "exited_unknown"
    assert all(r.kind != "failed" for r in sink.delivered)


def test_exit_without_a_code_but_oom_killed_is_still_failed(tmp_path):
    clock = FakeClock()

    def read():
        return {
            "schema": SIGNALS_SCHEMA,
            "observed_at": clock().isoformat(),
            "state": "exited",
            "oom_killed": True,
        }

    watcher, _ = make_watcher(tmp_path, clock, read)
    assert [r.kind for r in watcher.tick()] == ["failed"]
